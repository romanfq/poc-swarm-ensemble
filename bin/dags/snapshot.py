"""A read-only view of the whole ledger at one instant.

Shared by the status panel (Ch.5.4), the poller (Ch.9.4) and the Swarm Board
(Ch.10.5), so all three always agree. Pure computation over the local clone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import resolve
from dags import records as R
from dags import timeutil


@dataclass
class TaskView:
    key: str
    short: str
    dir: Path
    title: str
    epic: str | None
    repo: str | None
    autonomy: str
    state: str                   # open | claimed | in-progress | awaiting-review | done | frozen | rejected | arbitrated-stale
    ready: bool
    res: resolve.Resolution
    checkpoint: dict
    retries: int
    meta: dict
    plan_status: str | None = None     # None | pending-review | changes-requested | approved

    @property
    def winner(self):
        return self.res.winner

    @property
    def owner_machine(self) -> str | None:
        return self.res.winner.machine if self.res.winner else None

    @property
    def owner_human(self) -> str | None:
        return self.res.winner.human if self.res.winner else None

    @property
    def worker(self) -> str | None:
        cp = self.checkpoint
        if self.res.winner and cp.get("claim_id") == self.res.winner.id:
            return cp.get("worker")
        return None

    @property
    def pr_url(self) -> str | None:
        return self.res.outcome.pr_url

    @property
    def pausing(self) -> bool:
        """The swarm asked this task's worker to stop because the quota was lowered."""
        req = self.checkpoint.get("pause_requested")
        return bool(req and self.winner and req.get("claim_id") == self.winner.id)

    @property
    def dispatch_failed(self) -> dict | None:
        """The current claim's worker could not be started: {attempts, error, at} (GH-10)."""
        f = self.checkpoint.get("dispatch_failed")
        return f if f and self.winner and f.get("claim_id") == self.winner.id else None

    @property
    def needs_human(self) -> str | None:
        return self.checkpoint.get("needs_human") or None

    def claim_age_s(self, now: datetime) -> float | None:
        if not self.res.winner:
            return None
        return timeutil.age_seconds(self.res.winner.wall, now)


@dataclass
class Snapshot:
    root: Path
    now: datetime
    machine: str
    tasks: list[TaskView] = field(default_factory=list)
    quota_n: int = 0
    quota_used: int = 0
    mine_used: int = 0
    share: int | None = None
    machines: dict[str, dict] = field(default_factory=dict)
    takeovers: dict[str, set[str]] = field(default_factory=dict)

    def by_key(self, key: str) -> TaskView | None:
        low = key.lower()
        for t in self.tasks:
            if low in (t.key.lower(), t.short.lower(), t.dir.name.lower()):
                return t
        return None

    @property
    def work(self) -> list[TaskView]:
        return [t for t in self.tasks if not t.meta.get("is_epic")]

    @property
    def ready(self) -> list[TaskView]:
        return [t for t in self.work if t.ready]

    @property
    def live_claims(self) -> list[TaskView]:
        return [t for t in self.work if t.state in ("claimed", "in-progress")]

    @property
    def awaiting_review(self) -> list[TaskView]:
        return [t for t in self.work if t.state == "awaiting-review"]

    @property
    def conflicts(self) -> list[TaskView]:
        return [t for t in self.work if t.res.conflict and t.state not in ("done",)]

    def awaiting_worker(self, machine: str | None = None) -> list[TaskView]:
        machine = machine or self.machine
        return [t for t in self.work if t.state == "claimed" and t.owner_machine == machine
                and t.winner in t.res.valid]

    def plans_pending(self) -> list[TaskView]:
        return [t for t in self.live_claims if t.plan_status == "pending-review"]


def machine_states(root: Path) -> dict[str, dict]:
    from dags.ledger import machine_control
    machines = set()
    for _, d in R.read_dir(Path(root) / "control"):
        if d.get("machine"):
            machines.add(str(d["machine"]))
    return {m: machine_control(root, m) for m in sorted(machines)}


def take(ctx, now: datetime | None = None, share: int | None = None) -> Snapshot:
    root = ctx.root
    now = now or timeutil.now()
    lease = ctx.settings.lease_s
    humans = ctx.human_names
    idx = resolve.index(root)
    snap = Snapshot(root=root, now=now, machine=ctx.identity)
    for key, d in sorted(idx.items(), key=lambda kv: kv[1].as_posix()):
        meta = resolve.read_meta(d)
        res = resolve.resolve(d, now, lease, humans)
        state = resolve.task_state(d, now, lease, humans, res)
        ready = (not meta.get("is_epic")) and resolve.ledger_ready(root, d, now, lease, humans, idx)
        snap.tasks.append(TaskView(
            key=key, short=str(meta.get("short") or key), dir=d, title=str(meta.get("title") or ""),
            epic=meta.get("epic"), repo=meta.get("repo"), autonomy=str(meta.get("autonomy") or ""),
            state=state, ready=ready, res=res, checkpoint=R.load_yaml(d / "checkpoint.yaml"),
            retries=resolve.retry_count(d, now, lease), meta=meta,
            plan_status=resolve.plan_status(d, humans)))
    snap.quota_n = resolve.global_quota(root, ctx.settings.default_quota, humans)
    active = [t for t in snap.live_claims if t.winner in t.res.valid]
    snap.quota_used = len(active)
    snap.mine_used = sum(1 for t in active if t.owner_machine == ctx.identity)
    snap.machines = machine_states(root)
    mine = snap.machines.get(ctx.identity, {})
    snap.share = mine.get("quota_share") if mine.get("quota_share") is not None else share
    snap.takeovers = resolve.active_takeovers(root)
    return snap
