"""The scheduler loop and the heartbeat writer (whitepaper Ch.7, Ch.6.4, Ch.6.5, plan §2.9).

One cycle:  pull -> plan sync -> honour control records -> tidy own claims
(withdraw lost ones, resume won ones) -> claim ready work within quota ->
re-resolve -> prepare worktree -> dispatch the default worker or wait for a
human to pick one on the Board.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import resolve
import workers
from backends.base import TaskRef
from dags import ledger as L
from dags import records as R
from dags import plan, timeutil, work, worktree

log = logging.getLogger("dags.scheduler")


@dataclass
class CycleReport:
    paused: bool = False
    stop_requested: bool = False
    claimed: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    awaiting_worker: list[str] = field(default_factory=list)
    pausing: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    room: int = 0
    errors: list[str] = field(default_factory=list)


def _ref(task_dir: Path) -> TaskRef:
    return TaskRef(str(resolve.read_meta(task_dir)["key"]))


class Scheduler:
    def __init__(self, ctx, share: int, default_worker: str | None = None, notify=None,
                 launch=None, platform: str | None = None, run_plan_sync: bool = True,
                 started_clock: int = 0):
        self.ctx = ctx
        self.share = share
        self.default_worker = workers.resolve_name(default_worker) if default_worker else None
        self.notify = notify or (lambda text, kind="info": log.info(text))
        self.launch = launch
        self.platform = platform
        self.run_plan_sync = run_plan_sync
        self.started_clock = started_clock
        self._announced: set[str] = set()
        self._status_cache: dict[str, str] = {}

    # -- helpers ------------------------------------------------------------------
    @property
    def me(self) -> str:
        return self.ctx.identity

    def _set_status(self, task_dir: Path, status: str) -> None:
        key = str(resolve.read_meta(task_dir)["key"])
        if self._status_cache.get(key) == status:
            return
        try:
            self.ctx.backend.set_status(TaskRef(key), status)
            self._status_cache[key] = status
        except Exception as e:  # noqa: BLE001
            log.warning("set_status(%s, %s) failed: %s", key, status, e)

    def eligible(self, meta: dict) -> bool:
        if meta.get("is_epic") or not meta.get("repo"):
            return False
        if meta.get("autonomy") == "human-must-scope" and self.default_worker and \
                not workers.WORKERS[self.default_worker].human:
            return False            # never auto-dispatched to an AI worker (plan §2.8)
        return True

    def control_state(self) -> dict:
        return L.machine_control(self.ctx.root, self.me)

    # -- the cycle --------------------------------------------------------------------
    def cycle(self) -> CycleReport:
        ctx = self.ctx
        rep = CycleReport()
        ctx.coord.pull()
        if self.run_plan_sync:
            try:
                plan.sync(ctx)
            except Exception as e:  # noqa: BLE001 - tracker hiccups must not stop the loop
                rep.errors.append(f"plan sync: {e}")
                log.warning("plan sync failed: %s", e)

        ctl = self.control_state()
        last = ctl.get("last") or {}
        if ctl["stopped"] and _clock(last) > self.started_clock:
            rep.stop_requested = True
            return rep
        share = ctl["quota_share"] if ctl["quota_share"] is not None else self.share

        self.tidy_own_claims(rep)
        self.enforce_quota(share, rep)

        if ctl["paused"]:
            rep.paused = True
            return rep

        now = timeutil.now()
        lease = ctx.settings.lease_s
        humans = ctx.human_names
        rep.room = resolve.quota_room(ctx.root, self.me, share, ctx.settings.default_quota, now, lease, humans)
        if rep.room <= 0:
            return rep
        try:
            backend_ready = {r.key for r in ctx.backend.ready_tasks()}
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"backend: {e}")
            return rep
        idx = resolve.index(ctx.root)
        candidates = []
        for key, d in idx.items():
            if key not in backend_ready:
                continue
            meta = resolve.read_meta(d)
            if not self.eligible(meta):
                continue
            if resolve.ledger_ready(ctx.root, d, now, lease, humans, idx):
                candidates.append((key, meta))
        ordered = resolve.order_candidates(candidates, self.me, resolve.active_takeovers(ctx.root))

        wins = 0
        for key, _meta in ordered:
            if wins >= rep.room:
                break
            d = idx[key]
            # somebody may have claimed it since our pull (Ch.6.2)
            ctx.coord.pull()
            if not resolve.ledger_ready(ctx.root, d, timeutil.now(), lease, humans):
                continue
            cid = L.claim(ctx, d, worker=self.default_worker)
            ctx.coord.pull()
            res = resolve.resolve(d, timeutil.now(), lease, humans)
            if res.winner is None or res.winner.id != cid:
                L.withdraw(ctx, d, cid, "lost-race", winner=res.winner.id if res.winner else None)
                rep.lost.append(key)
                continue
            wins += 1
            rep.claimed.append(key)
            self._set_status(d, "claimed")
            self.after_win(d, cid, rep)
        return rep

    def tidy_own_claims(self, rep: CycleReport) -> None:
        ctx = self.ctx
        now = timeutil.now()
        lease = ctx.settings.lease_s
        for d in resolve.task_dirs(ctx.root):
            res = resolve.resolve(d, now, lease, ctx.human_names)
            mine = [c for c in res.valid if c.machine == self.me]
            if not mine:
                continue
            state = resolve.task_state(d, now, lease, ctx.human_names, res)
            winner = res.winner
            for c in mine:
                if winner is not None and c.id == winner.id:
                    continue
                if res.outcome.kind == "pr-opened" and res.outcome.claim_id == c.id:
                    continue
                reason = "arbitration" if res.arbitration is not None else "lost-race"
                L.withdraw(ctx, d, c.id, reason, winner=winner.id if winner else None)
                rep.lost.append(resolve.label(d))
                self.notify(f"{self.me} lost {resolve.label(d)} "
                            f"to {winner.id if winner else 'a freeze'}; stopped working on it", "lost")
            if winner is not None and winner.machine == self.me and winner in res.valid and state == "claimed":
                self.after_win(d, winner.id, rep)

    def enforce_quota(self, share: int, rep: CycleReport) -> None:
        """Quota lowered below what is running (Ch.8): a claim with no worker
        yet is released at once; a running one is asked to checkpoint and stop
        (``pause_requested`` in its checkpoint, shown by the skill) and is
        released one lease later. The task then waits, ready, until a slot
        frees, and resumes from its checkpoint."""
        ctx = self.ctx
        now = timeutil.now()
        lease = ctx.settings.lease_s
        humans = ctx.human_names
        over = resolve.over_quota(ctx.root, self.me, share, ctx.settings.default_quota, now, lease, humans)
        over_ids = {c.id for _, c in over}
        for d, c in over:
            label = resolve.label(d)
            state = resolve.task_state(d, now, lease, humans)
            cp = L.read_checkpoint(d)
            req = cp.get("pause_requested") if cp.get("claim_id") == c.id else None
            if state == "claimed" or (req and (timeutil.age_seconds(req.get("at"), now) or 0) >= lease):
                L.withdraw(ctx, d, c.id, "quota")
                if req:
                    L.update_checkpoint(ctx, d, c.id, check_owner=False, pause_requested=None)
                self._status_cache.pop(str(resolve.read_meta(d)["key"]), None)
                self._set_status(d, "ready")
                rep.released.append(label)
                self.notify(f"{label} released: the quota was lowered. It will resume from its "
                            f"checkpoint when a slot frees.", "quota")
            elif not req:
                L.update_checkpoint(ctx, d, c.id, pause_requested={
                    "claim_id": c.id, "at": timeutil.iso(),
                    "reason": "the quota was lowered below the tasks running"})
                rep.pausing.append(label)
                self.notify(f"The quota was lowered: {label} should record its progress "
                            f"(swarm-task note) and stop. It will be released in "
                            f"{int(lease // 60)} minutes.", "quota")
            else:
                rep.pausing.append(label)
        # quota raised again before the grace period ended: lift the request
        for d in resolve.task_dirs(ctx.root):
            cp = L.read_checkpoint(d)
            req = cp.get("pause_requested")
            if not req or cp.get("machine") != self.me or req.get("claim_id") in over_ids:
                continue
            res = resolve.resolve(d, now, lease, humans)
            if res.winner is not None and res.winner.id == req.get("claim_id") and res.winner in res.valid:
                L.update_checkpoint(ctx, d, res.winner.id, pause_requested=None)
                self.notify(f"Quota raised again: {resolve.label(d)} can carry on.", "quota")

    def after_win(self, d: Path, cid: str, rep: CycleReport) -> None:
        label = resolve.label(d)
        if self.default_worker:
            try:
                work.choose_worker(self.ctx, d, self.default_worker, launch=self.launch, platform=self.platform)
                rep.dispatched.append(label)
                self.notify(f"Handed {label} to {workers.WORKERS[self.default_worker].label}", "dispatched")
                return
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{label}: dispatch failed: {e}")
                log.warning("dispatch of %s failed: %s", label, e)
                return
        rep.awaiting_worker.append(label)
        if cid in self._announced:
            return
        self._announced.add(cid)
        # prepare the worktree now so the human's choice is instant
        try:
            work.prepare(self.ctx, d, cid)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"{label}: prepare failed: {e}")
        autonomy = str(resolve.read_meta(d).get("autonomy") or "")
        text = workers.prompt_text(label)
        if len(workers.allowed_for(autonomy)) < len(workers.WORKERS):
            text += f"\n  ({autonomy}: human workers only)"
        self.notify(text, "needs-worker")


def _clock(data: dict) -> int:
    return R.clock_of(data or {})


class Heartbeater:
    """Writes heartbeats for every live claim this machine won, on behalf of
    whatever worker is attached (Ch.7.3), with the idle limit of plan §2.9."""

    def __init__(self, ctx, notify=None):
        self.ctx = ctx
        self.notify = notify or (lambda text, kind="info": log.info(text))
        self._prompted: set[str] = set()
        self._dropped: set[str] = set()

    def last_progress(self, d: Path, claim) -> float:
        """Seconds since the last sign of life from the worker."""
        now = timeutil.now()
        cp = L.read_checkpoint(d)
        stamps = [claim.wall]
        if cp.get("claim_id") == claim.id:
            stamps += [cp.get("wall_utc"), cp.get("human_confirmed_utc"), cp.get("dispatched_utc")]
        wt = worktree.worktree_path(self.ctx, d)
        stamps.append(worktree.last_commit_time(wt))
        ages = [timeutil.age_seconds(s, now) for s in stamps if s]
        ages = [a for a in ages if a is not None]
        return min(ages) if ages else 0.0

    def items(self) -> list[tuple[Path, str]]:
        ctx = self.ctx
        now = timeutil.now()
        lease = ctx.settings.lease_s
        idle_limit = ctx.settings.human_idle_s
        out = []
        for d in resolve.task_dirs(ctx.root):
            res = resolve.resolve(d, now, lease, ctx.human_names)
            w = res.winner
            if w is None or w.machine != ctx.identity or w not in res.valid:
                continue
            if resolve.task_state(d, now, lease, ctx.human_names, res) not in resolve.ACTIVE_STATES:
                continue
            label = resolve.label(d)
            idle = self.last_progress(d, w)
            if idle > idle_limit + lease:
                if w.id not in self._dropped:
                    self._dropped.add(w.id)
                    self.notify(f"No answer about {label}; letting its lease expire so another "
                                f"machine can resume it", "idle-expire")
                continue
            if idle > idle_limit and w.id not in self._prompted:
                self._prompted.add(w.id)
                self.notify(f"Still working on {label}? No progress for {int(idle // 3600)}h. "
                            f"Answer with `swarm.py task still-working {label}` or on the Board.", "idle")
            elif idle <= idle_limit:
                self._prompted.discard(w.id)
            out.append((d, w.id))
        return out

    def cycle(self) -> int:
        self.ctx.coord.pull()
        items = self.items()
        lost = L.heartbeat(self.ctx, items)
        for d, cid in lost:
            self.notify(f"{self.ctx.identity} no longer owns {resolve.label(d)} ({cid})", "lost")
        return len(items) - len(lost)


class Loop(threading.Thread):
    """Runs ``fn`` every ``interval`` seconds until ``stop`` is set."""

    def __init__(self, name: str, fn, interval: float, stop: threading.Event):
        super().__init__(name=name, daemon=True)
        self.fn = fn
        self.interval = interval
        self.stop = stop
        self.last_error: str | None = None
        self.cycles = 0

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.fn()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("%s cycle failed", self.name)
            self.cycles += 1
            self.stop.wait(self.interval)
