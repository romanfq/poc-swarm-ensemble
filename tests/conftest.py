import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from dags import records as R  # noqa: E402

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = 15 * 60


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class Ledger:
    """Tiny helper for writing fixture records."""

    def __init__(self, root: Path):
        self.root = root

    def task(self, key, epic="EPIC-1", deps=(), **extra):
        d = self.root / "tasks" / R.slug(epic) / R.slug(key)
        meta = {"key": key, "epic": epic, "dependencies": list(deps), "logical_clock": 1, **extra}
        R.write_new(d / "meta.yaml", meta)
        return d

    def claim(self, task_dir, machine, clock, minute=0):
        return R.write_new(task_dir / "claims" / R.claim_name(machine, clock), {
            "claim_id": R.claim_id(machine, clock), "machine": machine, "human": "roman",
            "logical_clock": clock, "wall_utc": at(minute).isoformat()})

    def heartbeat(self, task_dir, machine, clock, minute):
        return R.write_replace(task_dir / "heartbeats" / R.heartbeat_name(machine), {
            "claim_id": R.claim_id(machine, clock), "machine": machine,
            "logical_clock": clock + 1000, "wall_utc": at(minute).isoformat()})

    def withdraw(self, task_dir, machine, clock, reason="lost-race", winner=None, wclock=None):
        wclock = wclock or clock + 1
        return R.write_new(task_dir / "withdrawals" / R.withdrawal_name(machine, wclock), {
            "claim_id": R.claim_id(machine, clock), "machine": machine, "reason": reason,
            "winner": winner, "logical_clock": wclock})

    def arbitrate(self, task_dir, human, winner, clock, action=None):
        data = {"human": human, "winner": winner, "reason": "test", "logical_clock": clock}
        if action:
            data["action"] = action
        return R.write_new(task_dir / "arbitration" / R.arbitration_name(human, clock), data)

    def complete(self, task_dir, machine, kind, clock, claim=None, **extra):
        return R.write_new(task_dir / "completions" / R.completion_name(machine, kind, clock), {
            "kind": kind, "machine": machine, "claim_id": claim, "logical_clock": clock, **extra})

    def checkpoint(self, task_dir, **data):
        return R.write_replace(task_dir / "checkpoint.yaml", data)


@pytest.fixture
def ledger(tmp_path):
    for d in R.RECORD_DIRS:
        (tmp_path / d).mkdir()
    return Ledger(tmp_path)


# --- multi-machine fixtures (Phase 2+) -------------------------------------------

import subprocess  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def sh(args, cwd):
    import os
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True, env=env).stdout


BACKEND_YAML = """\
backend: fake
fake:
  path: fake-backend.yaml
repos:
  OWNER/app:
    base: main
    test_command: "true"
default_repo: OWNER/app
swarm:
  default_quota: 3
  lease_minutes: 15
  heartbeat_minutes: 3
  max_retries: 3
  human_idle_hours: 8
  thrash_threshold: 2
"""

HUMANS_YAML = """\
humans:
  - name: roman
    github: romanfq
    emails: [t@example.com]
  - name: jane
    github: jane-gh
    emails: [jane@example.com]
"""


class Swarm:
    """A bare 'GitHub' remote plus any number of machine clones."""

    def __init__(self, base: Path):
        self.base = base
        self.remote = base / "remote.git"
        sh(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], base)
        seed = base / "seed"
        sh(["git", "init", "-q", "-b", "main", str(seed)], base)
        (seed / "backend.yaml").write_text(BACKEND_YAML)
        (seed / "humans.yaml").write_text(HUMANS_YAML)
        (seed / ".gitignore").write_text(".swarm/\n.worktrees/\n__pycache__/\n")
        (seed / "CONVENTIONS.md").write_text("# conventions\n")
        tpl = seed / "templates"
        tpl.mkdir()
        real = Path(__file__).resolve().parent.parent / "templates"
        for f in real.iterdir():
            (tpl / f.name).write_text(f.read_text())
        for d in ("tasks", "control", "priority", "quota"):
            (seed / d).mkdir()
            (seed / d / ".gitkeep").write_text("")
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-qm", "seed"], seed)
        sh(["git", "remote", "add", "origin", str(self.remote)], seed)
        sh(["git", "push", "-q", "origin", "main"], seed)

    def clone(self, machine: str, human: str = "roman"):
        from dags.config import Context
        path = self.base / machine
        sh(["git", "clone", "-q", str(self.remote), str(path)], self.base)
        sh(["git", "config", "user.name", machine], path)
        sh(["git", "config", "user.email", "t@example.com"], path)
        (path / ".swarm").mkdir()
        (path / ".swarm" / "local.yaml").write_text(f"human: {human}\nworker_token: none\n")
        return Context(path, identity=machine)


@pytest.fixture
def swarm(tmp_path):
    return Swarm(tmp_path)


class FakeGh:
    """Records gh invocations and answers from a handler: fn(args, env, input) ->
    (stdout, returncode) or str."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda args, env, input: "")

    def __call__(self, args, *, env, cwd=None, input=None):
        self.calls.append({"args": list(args), "token": env.get("GH_TOKEN"), "cwd": cwd, "input": input})
        out = self.handler(list(args), env, input)
        rc, err = 0, ""
        if isinstance(out, tuple):
            out, rc, *rest = out
            err = rest[0] if rest else ("boom" if rc else "")
        return subprocess.CompletedProcess(["gh", *args], rc, out, err)


@pytest.fixture
def fake_gh():
    from dags import gh
    fake = FakeGh()
    gh.set_runner(fake)
    yield fake
    gh.set_runner(None)
