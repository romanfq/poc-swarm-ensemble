"""Write operations on the coordination repo.

Every function here is one git transaction: pull, compute a fresh logical
clock, write new file(s), commit, push (Ch.6.2). Readers live in resolve.py.
"""
from __future__ import annotations

from pathlib import Path

import resolve
from dags import records as R
from dags import timeutil
from dags.config import ConfigError, Context


class LostClaim(RuntimeError):
    """Raised when this machine no longer owns a task it tried to act on."""


def _stamp(ctx: Context, clock: int, **data) -> dict:
    return {**data, "machine": data.get("machine", ctx.identity), "logical_clock": clock,
            "wall_utc": timeutil.iso()}


def _tx(ctx: Context, message: str, build, push: bool = True) -> list[Path]:
    return ctx.coord.transaction(build, f"[swarm] {ctx.identity}: {message}", push=push)


# -- claims (Ch.6.2, 6.4) -----------------------------------------------------------

def claim(ctx: Context, task_dir: Path, worker: str | None = None) -> str:
    result = {}

    def build():
        clock = resolve.next_clock(ctx.root)
        cid = R.claim_id(ctx.identity, clock)
        result["id"] = cid
        return [R.write_new(task_dir / "claims" / R.claim_name(ctx.identity, clock),
                            _stamp(ctx, clock, claim_id=cid, human=ctx.operator, worker=worker,
                                   task=task_dir.name))]

    _tx(ctx, f"claim {task_dir.name}", build)
    return result["id"]


def withdraw(ctx: Context, task_dir: Path, claim_id: str, reason: str, winner: str | None = None,
             push: bool = True) -> None:
    def build():
        existing = resolve.read_withdrawals(task_dir)
        if claim_id in existing:
            return []
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(task_dir / "withdrawals" / R.withdrawal_name(ctx.identity, clock),
                            _stamp(ctx, clock, claim_id=claim_id, reason=reason, winner=winner))]

    _tx(ctx, f"withdraw {claim_id} ({reason})", build, push=push)


def heartbeat(ctx: Context, items: list[tuple[Path, str]], push: bool = True) -> list[tuple[Path, str]]:
    """One commit for all of this machine's live claims (plan §2.4). Each
    claim is re-checked against resolve() after the pull (Ch.6.4); the
    ones this machine no longer owns are returned so the caller can withdraw."""
    lost: list[tuple[Path, str]] = []
    if not items:
        return lost

    def build():
        base = resolve.next_clock(ctx.root)
        paths = []
        for i, (task_dir, cid) in enumerate(items):
            if not still_mine(ctx, task_dir, cid):
                lost.append((task_dir, cid))
                continue
            paths.append(R.write_replace(task_dir / "heartbeats" / R.heartbeat_name(ctx.identity),
                                         _stamp(ctx, base + i, claim_id=cid)))
        return paths

    _tx(ctx, f"heartbeat x{len(items)}", build, push=push)
    return lost


def still_mine(ctx: Context, task_dir: Path, claim_id: str) -> bool:
    res = resolve.resolve(task_dir, timeutil.now(), ctx.settings.lease_s, ctx.human_names)
    return res.winner is not None and res.winner.id == claim_id


def require_mine(ctx: Context, task_dir: Path, claim_id: str) -> None:
    """Re-check resolve() before any expensive or irreversible step (Ch.6.4)."""
    ctx.coord.pull()
    if not still_mine(ctx, task_dir, claim_id):
        raise LostClaim(f"{claim_id} no longer owns {task_dir.name}")


# -- checkpoint (Ch.8) — single writer: the current winner ----------------------------

CHECKPOINT_LISTS = ("tried", "remaining", "open_questions", "risks")


def read_checkpoint(task_dir: Path) -> dict:
    return R.load_yaml(Path(task_dir) / "checkpoint.yaml")


def update_checkpoint(ctx: Context, task_dir: Path, claim_id: str, *, append: dict | None = None,
                      push: bool = True, check_owner: bool = True, event: dict | None = None,
                      **fields) -> dict:
    """``event`` ({"kind": ..., **fields}) also writes an append-only
    ``events/`` record in the same commit, with the checkpoint's clock, so
    the activity feed sees a change the checkpoint only overwrites."""
    out = {}

    def build():
        if check_owner and not still_mine(ctx, task_dir, claim_id):
            raise LostClaim(f"{claim_id} no longer owns {task_dir.name}")
        cp = read_checkpoint(task_dir)
        if cp.get("claim_id") not in (None, claim_id):
            # resuming someone else's work: keep their history
            cp.setdefault("previous_claims", []).append(cp.get("claim_id"))
        cp["claim_id"] = claim_id
        cp["machine"] = ctx.identity
        for k, v in fields.items():
            cp[k] = v
        for k, values in (append or {}).items():
            lst = list(cp.get(k) or [])
            for v in values if isinstance(values, (list, tuple)) else [values]:
                if v not in lst:
                    lst.append(v)
            cp[k] = lst
        cp["logical_clock"] = resolve.next_clock(ctx.root)
        cp["wall_utc"] = timeutil.iso()
        out.update(cp)
        paths = [R.write_replace(task_dir / "checkpoint.yaml", cp)]
        if event:
            paths.append(_write_event(ctx, task_dir, cp["logical_clock"], claim_id=claim_id, **event))
        return paths

    _tx(ctx, f"checkpoint {task_dir.name}", build, push=push)
    return out


# -- events (feed only) — append-only notes of what a checkpoint change meant ------------

def _write_event(ctx: Context, task_dir: Path, clock: int, kind: str, **fields) -> Path:
    return R.write_new(task_dir / "events" / R.event_name(ctx.identity, kind, clock),
                       _stamp(ctx, clock, kind=kind, **fields))


def record_event(ctx: Context, task_dir: Path, kind: str, once_per_claim: bool = False,
                 push: bool = True, **fields) -> bool:
    """An event with no checkpoint change. ``once_per_claim`` skips it when
    this machine already recorded ``kind`` for the same ``claim_id``.
    Returns whether a record was written."""
    wrote = []

    def build():
        if once_per_claim:
            for _, d in R.read_dir(task_dir / "events"):
                if d.get("kind") == kind and d.get("claim_id") == fields.get("claim_id"):
                    return []
        wrote.append(_write_event(ctx, task_dir, resolve.next_clock(ctx.root), kind, **fields))
        return wrote

    _tx(ctx, f"{kind} {task_dir.name}", build, push=push)
    return bool(wrote)


# -- completions ------------------------------------------------------------------

def complete(ctx: Context, task_dir: Path, kind: str, push: bool = True, **fields) -> None:
    def build():
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(task_dir / "completions" / R.completion_name(ctx.identity, kind, clock),
                            _stamp(ctx, clock, kind=kind, **fields))]

    _tx(ctx, f"{kind} {task_dir.name}", build, push=push)


# -- plan review (plan §2.8) — append-only, so any human on any machine may write it --

def review_plan(ctx: Context, task_dir: Path, plan_sha: str, decision: str, note: str = "") -> None:
    assert decision in ("approved", "changes-requested")
    human = ctx.require_human()

    def build():
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(task_dir / "plan-reviews" / R.arbitration_name(human, clock),
                            _stamp(ctx, clock, human=human, plan_sha=plan_sha, decision=decision, note=note))]

    _tx(ctx, f"{human} {decision} plan for {task_dir.name}", build)


# -- human levers (Ch.6.6, 10.2) ----------------------------------------------------------

def arbitrate(ctx: Context, task_dir: Path, winner: str | None, reason: str,
              action: str | None = None) -> None:
    human = ctx.require_human()
    if not reason.strip():
        raise ConfigError("arbitration needs a reason (Ch.9.3)")

    def build():
        clock = resolve.next_clock(ctx.root)
        data = _stamp(ctx, clock, human=human, winner=winner or "none", reason=reason)
        if action:
            data["action"] = action
        return [R.write_new(task_dir / "arbitration" / R.arbitration_name(human, clock), data)]

    verb = action or ("freeze" if not winner else f"award to {winner}")
    _tx(ctx, f"{human} arbitrates {task_dir.name}: {verb}", build)


def control(ctx: Context, action: str, target_machine: str | None = None, **fields) -> bool:
    """Returns False (and writes nothing) for a pause or resume that wouldn't
    change the machine's state, checked after the pull."""
    human = ctx.operator
    machine = target_machine or ctx.identity
    wrote = []

    def build():
        if action in ("pause", "resume") and machine_control(ctx.root, machine)["paused"] == (action == "pause"):
            return []
        wrote.append(True)
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(ctx.root / "control" / R.control_name(machine, action, clock),
                            _stamp(ctx, clock, machine=machine, human=human, action=action, **fields))]

    _tx(ctx, f"{action} {machine}", build)
    return bool(wrote)


def priority(ctx: Context, epic: str, action: str) -> None:
    assert action in ("takeover", "release")
    human = ctx.operator

    def build():
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(ctx.root / "priority" / R.priority_name(ctx.identity, epic, action, clock),
                            _stamp(ctx, clock, epic=epic, human=human, action=action))]

    _tx(ctx, f"{action} epic {epic}", build)


def set_quota(ctx: Context, n: int, reason: str = "") -> None:
    human = ctx.require_human()

    def build():
        clock = resolve.next_clock(ctx.root)
        return [R.write_new(ctx.root / "quota" / R.quota_name(human, clock),
                            _stamp(ctx, clock, human=human, n=int(n), reason=reason))]

    _tx(ctx, f"{human} sets global quota to {n}", build)


# -- machine control state (derived from control/ records) ----------------------------------

def machine_control(root: Path, machine: str) -> dict:
    """Latest pause/resume/stop/throttle state for a machine."""
    state = {"paused": False, "quota_share": None, "stopped": False, "last": None}
    records = sorted(R.read_dir(Path(root) / "control"), key=lambda pd: (R.clock_of(pd[1]), pd[0].name))
    for _, d in records:
        if d.get("machine") != machine:
            continue
        action = d.get("action")
        if action == "pause":
            state["paused"] = True
        elif action == "resume":
            state["paused"] = False
        elif action == "start":
            state["stopped"] = False
            if d.get("quota_share") is not None:
                state["quota_share"] = int(d["quota_share"])
        elif action == "stop":
            state["stopped"] = True
        elif action == "throttle" and d.get("quota_share") is not None:
            state["quota_share"] = int(d["quota_share"])
        state["last"] = d
    return state
