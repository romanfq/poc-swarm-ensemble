"""The per-machine daemon (plan §2.2, whitepaper Ch.5.3 step 4).

``swarm.py start`` spawns ``swarm.py _daemon`` detached; the daemon runs the
scheduler, heartbeat and poller loops as threads of one process, writes
``.swarm/daemon.pid`` and logs to ``.swarm/swarm.log``. ``stop`` writes a
shared stop record (so every Board shows it) and sends SIGTERM; each loop
finishes its current cycle and exits, and leases lapse through the normal
heartbeat timeout rather than being yanked (Ch.5.4).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import resolve
from dags import timeutil

log = logging.getLogger("dags.daemon")

PIDFILE = "daemon.pid"
INFOFILE = "daemon.json"
LOGFILE = "swarm.log"


@dataclass
class Options:
    quota_share: int = 1
    poll_interval: float = 60.0
    cycle_interval: float = 30.0
    default_worker: str | None = None
    identity: str | None = None
    no_poller: bool = False


def parse_interval(text: str | float | int) -> float:
    """'60s', '5m', '1h' or plain seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    if t and t[-1] in mult:
        return float(t[:-1]) * mult[t[-1]]
    return float(t)


# -- pidfile ------------------------------------------------------------------------

def pid_path(ctx) -> Path:
    return ctx.swarm_dir / PIDFILE


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(ctx) -> int | None:
    try:
        pid = int(pid_path(ctx).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if alive(pid) else None


def info(ctx) -> dict:
    try:
        return json.loads((ctx.swarm_dir / INFOFILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


# -- control from the CLI -----------------------------------------------------------------

def spawn(ctx, opts: Options, script: Path, python: str | None = None, wait_s: float = 10.0) -> int:
    if running_pid(ctx):
        raise RuntimeError(f"a swarm daemon is already running (pid {running_pid(ctx)})")
    ctx.swarm_dir.mkdir(parents=True, exist_ok=True)
    args = [python or sys.executable, str(script), "_daemon",
            "--quota-share", str(opts.quota_share),
            "--poll-interval", str(opts.poll_interval),
            "--cycle-interval", str(opts.cycle_interval)]
    if opts.default_worker:
        args += ["--default-worker", opts.default_worker]
    if opts.identity:
        args += ["--identity", opts.identity]
    if opts.no_poller:
        args += ["--no-poller"]
    logf = open(ctx.swarm_dir / LOGFILE, "a")
    env = dict(os.environ, DAGS_ROOT=str(ctx.root))
    proc = subprocess.Popen(args, cwd=str(ctx.root), stdin=subprocess.DEVNULL, stdout=logf,
                            stderr=subprocess.STDOUT, start_new_session=True, env=env)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if running_pid(ctx) == proc.pid:
            return proc.pid
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited early (code {proc.returncode}); see .swarm/{LOGFILE}")
        time.sleep(0.1)
    return proc.pid


def terminate(ctx, wait_s: float = 60.0) -> bool:
    pid = running_pid(ctx)
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


# -- the daemon process itself --------------------------------------------------------------

class Daemon:
    def __init__(self, ctx, opts: Options, notify=None, launch=None, platform=None):
        from dags.notify import Notifier
        from dags.scheduler import Heartbeater, Scheduler
        from poll import Poller
        self.ctx = ctx
        self.opts = opts
        self.stop = threading.Event()
        self.notify = notify or Notifier(ctx.swarm_dir, ctx.local)
        self.started_clock = resolve.max_clock(ctx.root)
        self.scheduler = Scheduler(ctx, opts.quota_share, opts.default_worker, notify=self.notify,
                                   launch=launch, platform=platform, started_clock=self.started_clock)
        self.heartbeater = Heartbeater(ctx, notify=self.notify)
        self.poller = None if opts.no_poller else Poller(ctx, notify=self.notify)
        self.loops = []

    def _scheduler_cycle(self):
        rep = self.scheduler.cycle()
        if rep.stop_requested:
            log.info("stop record found for %s; shutting down", self.ctx.identity)
            self.stop.set()
        for e in rep.errors:
            log.warning("scheduler: %s", e)

    def start_threads(self) -> None:
        from dags.scheduler import Loop
        s = self.ctx.settings
        self.loops = [
            Loop("heartbeat", self.heartbeater.cycle, min(s.heartbeat_s, s.lease_s / 3), self.stop),
            Loop("scheduler", self._scheduler_cycle, self.opts.cycle_interval, self.stop),
        ]
        if self.poller:
            self.loops.append(Loop("poller", self.poller.cycle, self.opts.poll_interval, self.stop))
        for loop in self.loops:
            loop.start()

    def write_info(self) -> None:
        data = {"pid": os.getpid(), "identity": self.ctx.identity, "started_utc": timeutil.iso(),
                "options": asdict(self.opts),
                "threads": {lp.name: {"alive": lp.is_alive(), "cycles": lp.cycles, "error": lp.last_error}
                            for lp in self.loops}}
        tmp = self.ctx.swarm_dir / (INFOFILE + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.ctx.swarm_dir / INFOFILE)

    def run(self) -> int:
        ctx = self.ctx
        ctx.swarm_dir.mkdir(parents=True, exist_ok=True)
        pid_path(ctx).write_text(f"{os.getpid()}\n")

        def on_signal(signum, frame):
            log.info("signal %s: finishing current cycles", signum)
            self.stop.set()
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        log.info("daemon %s up (pid %s) %s", ctx.identity, os.getpid(), self.opts)
        try:
            self.start_threads()
            while not self.stop.is_set():
                self.write_info()
                self.stop.wait(5)
            for loop in self.loops:
                loop.join(timeout=120)
            return 0
        finally:
            try:
                if pid_path(ctx).read_text().strip() == str(os.getpid()):
                    pid_path(ctx).unlink()
            except FileNotFoundError:
                pass
            (ctx.swarm_dir / INFOFILE).unlink(missing_ok=True)
            log.info("daemon %s stopped", ctx.identity)


def setup_logging(ctx, to_stderr: bool = False) -> None:
    handlers = [logging.StreamHandler(sys.stdout if not to_stderr else sys.stderr)]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
