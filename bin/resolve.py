"""Claim resolution — the deterministic, leaderless core (whitepaper Ch.6, Ch.7.1).

Given the same synced repository, every machine computes the same answers
here. Nothing in this module talks to the network or writes files; it only
reads the ledger. Time enters only through the ``now`` argument, which callers
take from ``dags.timeutil.now()`` (skew-corrected).

Task lifecycle, derived and never stored:

    ready -> claimed (awaiting worker) -> in-progress -> awaiting-review -> done
                     \\-> lost lease / withdrawn -> ready
    awaiting-review -> changes requested (reopened) -> ready (resume from checkpoint)
    awaiting-review -> rejected -> held until re-planned in the backend
    any             -> frozen (arbitration winner: none)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dags import records as R
from dags import timeutil

# ---------------------------------------------------------------------------
# Logical clock (Ch.6.1)
# ---------------------------------------------------------------------------

_clock_cache: dict[Path, tuple[int, int, int]] = {}


def _cached_clock(path: Path) -> int:
    try:
        st = path.stat()
    except FileNotFoundError:
        return 0
    key = (st.st_mtime_ns, st.st_size)
    hit = _clock_cache.get(path)
    if hit and hit[:2] == key:
        return hit[2]
    value = R.clock_of(R.load_yaml(path))
    _clock_cache[path] = (key[0], key[1], value)
    return value


def max_clock(root) -> int:
    root = Path(root)
    best = 0
    for top in R.RECORD_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*.yaml"):
            if path.name.startswith("."):
                continue
            best = max(best, _cached_clock(path))
    return best


def next_clock(root) -> int:
    """1 + max(logical_clock across every synced file). Never stored locally."""
    return max_clock(root) + 1


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    id: str
    machine: str
    clock: int
    human: str | None
    wall: str | None
    path: Path
    data: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def sort_key(self) -> tuple[int, str]:
        return (self.clock, self.machine)


def _claim_from(path: Path, data: dict) -> Claim | None:
    machine = data.get("machine")
    if not machine:
        return None
    clock = R.clock_of(data)
    return Claim(
        id=str(data.get("claim_id") or R.claim_id(machine, clock)),
        machine=str(machine),
        clock=clock,
        human=data.get("human"),
        wall=data.get("wall_utc"),
        path=path,
        data=data,
    )


def read_claims(task_dir) -> list[Claim]:
    out = []
    for path, data in R.read_dir(Path(task_dir) / "claims"):
        c = _claim_from(path, data)
        if c:
            out.append(c)
    return sorted(out, key=lambda c: c.sort_key)


def read_withdrawals(task_dir) -> dict[str, dict]:
    """claim_id -> withdrawal record."""
    return {
        str(d.get("claim_id")): d
        for _, d in R.read_dir(Path(task_dir) / "withdrawals")
        if d.get("claim_id")
    }


def read_heartbeats(task_dir) -> dict[str, dict]:
    """claim_id -> most recent heartbeat record for it."""
    out: dict[str, dict] = {}
    for _, d in R.read_dir(Path(task_dir) / "heartbeats"):
        cid = d.get("claim_id")
        if not cid:
            continue
        prev = out.get(cid)
        if prev is None or R.clock_of(d) >= R.clock_of(prev):
            out[str(cid)] = d
    return out


def _sorted_records(directory) -> list[tuple[Path, dict]]:
    return sorted(R.read_dir(directory), key=lambda pd: (R.clock_of(pd[1]), pd[0].name))


def active_arbitration(task_dir, humans: set[str] | None = None) -> dict | None:
    """The arbitration in force, or None. The latest record (by logical clock)
    wins; ``action: withdraw`` cancels whatever came before. Records from
    names not in humans.yaml are ignored when ``humans`` is given."""
    current = None
    for _, d in _sorted_records(Path(task_dir) / "arbitration"):
        if humans is not None and d.get("human") not in humans:
            continue
        if d.get("action") == "withdraw":
            current = None
        else:
            current = d
    return current


# ---------------------------------------------------------------------------
# Lease expiry (Ch.6.5) — a read-time computation, no watchdog
# ---------------------------------------------------------------------------

def last_seen(claim: Claim, heartbeats: dict[str, dict]) -> datetime | None:
    hb = heartbeats.get(claim.id)
    candidates = [timeutil.parse(claim.wall)]
    if hb:
        candidates.append(timeutil.parse(hb.get("wall_utc")))
    candidates = [c for c in candidates if c is not None]
    return max(candidates) if candidates else None


def is_expired(claim: Claim, heartbeats: dict[str, dict], now: datetime, lease_s: float) -> bool:
    seen = last_seen(claim, heartbeats)
    if seen is None:
        return True
    return (now - seen).total_seconds() > lease_s


# ---------------------------------------------------------------------------
# Completions — what happened after the work
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    kind: str | None = None          # pr-opened | done | reopened | rejected | replanned | None
    pr_url: str | None = None
    claim_id: str | None = None
    record: dict | None = None
    completed_claims: frozenset = frozenset()


TERMINAL_HOLD = {"pr-opened", "done", "rejected"}


def read_outcome(task_dir) -> Outcome:
    out = Outcome()
    seen_reviews: set[str] = set()
    completed: set[str] = set()
    for _, d in _sorted_records(Path(task_dir) / "completions"):
        kind = d.get("kind")
        if kind == "reopened":
            rid = str(d.get("review_id") or "")
            if rid and rid in seen_reviews:
                continue  # duplicate reopen for a review already acted on
            if rid:
                seen_reviews.add(rid)
            if out.kind != "pr-opened":
                continue  # reopen only makes sense after a PR was opened
        elif kind == "rejected" and out.kind not in ("pr-opened", "reopened"):
            continue
        elif kind == "replanned" and out.kind != "rejected":
            continue
        elif kind not in ("pr-opened", "done", "reopened", "rejected", "replanned"):
            continue
        if kind == "pr-opened" and d.get("claim_id"):
            completed.add(str(d["claim_id"]))
        if out.kind == "done":
            continue  # done is final
        out.kind = kind
        out.record = d
        out.claim_id = d.get("claim_id") or out.claim_id
        out.pr_url = d.get("pr_url") or out.pr_url
    out.completed_claims = frozenset(completed)
    return out


def is_done(task_dir) -> bool:
    return read_outcome(task_dir).kind == "done"


# ---------------------------------------------------------------------------
# Resolution (Ch.6.3, 6.6)
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    winner: Claim | None
    reason: str                      # arbitration | frozen | clock | none
    claims: list[Claim]
    valid: list[Claim]
    arbitration: dict | None
    outcome: Outcome

    @property
    def frozen(self) -> bool:
        return self.reason == "frozen"

    @property
    def conflict(self) -> bool:
        """More than one live claimant (Ch.9.4)."""
        return len({c.machine for c in self.valid}) > 1


def resolve(task_dir, now: datetime, lease_s: float, humans: set[str] | None = None) -> Resolution:
    task_dir = Path(task_dir)
    claims = read_claims(task_dir)
    withdrawn = read_withdrawals(task_dir)
    heartbeats = read_heartbeats(task_dir)
    outcome = read_outcome(task_dir)
    arb = active_arbitration(task_dir, humans)

    # A claim whose work already produced a PR stays attributable, but it is
    # not "live" once the task was reopened or finished.
    def live(c: Claim) -> bool:
        if c.id in withdrawn:
            return False
        if c.id in outcome.completed_claims:
            return outcome.kind == "pr-opened" and outcome.claim_id == c.id
        return not is_expired(c, heartbeats, now, lease_s)

    valid = [c for c in claims if live(c)]

    if arb is not None:
        target = arb.get("winner")
        if target in (None, "none", ""):
            return Resolution(None, "frozen", claims, valid, arb, outcome)
        chosen = next((c for c in claims if c.id == str(target)), None)
        return Resolution(chosen, "arbitration", claims, valid, arb, outcome)

    if not valid:
        return Resolution(None, "none", claims, valid, None, outcome)
    winner = min(valid, key=lambda c: c.sort_key)
    return Resolution(winner, "clock", claims, valid, None, outcome)


# ---------------------------------------------------------------------------
# Task index and state
# ---------------------------------------------------------------------------

def task_dirs(root) -> list[Path]:
    base = Path(root) / "tasks"
    if not base.is_dir():
        return []
    return sorted(p.parent for p in base.glob("*/*/meta.yaml"))


def read_meta(task_dir) -> dict:
    """meta.yaml plus any later revisions in meta/ (latest clock wins per key)."""
    task_dir = Path(task_dir)
    meta = dict(R.load_yaml(task_dir / "meta.yaml"))
    for _, rev in _sorted_records(task_dir / "meta"):
        for k, v in rev.items():
            if k not in ("logical_clock", "machine", "wall_utc"):
                meta[k] = v
    return meta


def index(root) -> dict[str, Path]:
    """task key -> task directory."""
    out = {}
    for d in task_dirs(root):
        key = R.load_yaml(d / "meta.yaml").get("key")
        if key:
            out[str(key)] = d
    return out


def lookup(root, text: str) -> Path | None:
    """Find a task by backend key ('org/repo#7'), short key ('GH-7') or
    directory name, case-insensitively."""
    text = str(text).strip()
    idx = index(root)
    if text in idx:
        return idx[text]
    low = text.lower()
    for key, d in idx.items():
        meta = R.load_yaml(d / "meta.yaml")
        if low in (key.lower(), str(meta.get("short", "")).lower(), d.name.lower()):
            return d
    return None


def label(task_dir) -> str:
    """Human-facing task name: the short key if there is one."""
    meta = R.load_yaml(Path(task_dir) / "meta.yaml")
    return str(meta.get("short") or meta.get("key") or Path(task_dir).name)


ACTIVE_STATES = {"claimed", "in-progress"}


def task_state(task_dir, now: datetime, lease_s: float, humans: set[str] | None = None,
               res: Resolution | None = None) -> str:
    res = res or resolve(task_dir, now, lease_s, humans)
    kind = res.outcome.kind
    if kind == "done":
        return "done"
    if res.frozen:
        return "frozen"
    if kind == "pr-opened":
        return "awaiting-review"
    if kind == "rejected":
        return "rejected"
    if res.winner is not None:
        if res.reason == "arbitration" and res.winner not in res.valid:
            return "arbitrated-stale"
        cp = R.load_yaml(Path(task_dir) / "checkpoint.yaml")
        if cp.get("claim_id") == res.winner.id and cp.get("worker"):
            return "in-progress"
        return "claimed"
    return "open"


def deps_done(root, meta: dict, idx: dict[str, Path] | None = None) -> bool:
    idx = idx if idx is not None else index(root)
    for dep in meta.get("dependencies") or []:
        d = idx.get(str(dep))
        if d is None or not is_done(d):
            return False
    return True


def ledger_ready(root, task_dir, now: datetime, lease_s: float, humans: set[str] | None = None,
                 idx: dict[str, Path] | None = None) -> bool:
    """Ready from locally-read data only (Ch.7.2): dependencies done, no
    arbitration in effect, no live claim, not already waiting on a human."""
    task_dir = Path(task_dir)
    meta = read_meta(task_dir)
    if not meta or meta.get("is_epic"):
        return False
    res = resolve(task_dir, now, lease_s, humans)
    if res.arbitration is not None:
        return False
    if res.valid:
        return False
    if res.outcome.kind in TERMINAL_HOLD:
        return False
    return deps_done(root, meta, idx)


# ---------------------------------------------------------------------------
# Quota (Ch.7.1)
# ---------------------------------------------------------------------------

def global_quota(root, default: int, humans: set[str] | None = None) -> int:
    n = default
    for _, d in _sorted_records(Path(root) / "quota"):
        if humans is not None and d.get("human") not in humans:
            continue
        try:
            n = int(d["n"])
        except (KeyError, TypeError, ValueError):
            continue
    return max(0, n)


def active_claims(root, now: datetime, lease_s: float, humans: set[str] | None = None) -> list[tuple[Path, Claim]]:
    """Claims currently occupying a quota slot: live winners whose task is
    claimed or in progress (awaiting-review releases the slot, Ch.9.1)."""
    out = []
    for d in task_dirs(root):
        res = resolve(d, now, lease_s, humans)
        if res.winner is None or res.winner not in res.valid:
            continue
        if task_state(d, now, lease_s, humans, res) in ACTIVE_STATES:
            out.append((d, res.winner))
    return out


def over_quota(root, machine: str, share: int, default_n: int, now: datetime, lease_s: float,
               humans: set[str] | None = None) -> list[tuple[Path, Claim]]:
    """This machine's active claims that must yield because the global N or
    this machine's share was lowered below what is running (Ch.8 'quota
    exhausted mid-task'). Newest claims yield first; every machine computes
    the same global order, so together they release exactly the excess."""
    active = sorted(active_claims(root, now, lease_s, humans), key=lambda dc: dc[1].sort_key, reverse=True)
    n = global_quota(root, default_n, humans)
    chosen = {c.id for _, c in active[:max(0, len(active) - n)]}
    mine = [(d, c) for d, c in active if c.machine == machine]
    chosen |= {c.id for _, c in mine[:max(0, len(mine) - max(0, share))]}
    return [(d, c) for d, c in mine if c.id in chosen]


def quota_room(root, machine: str, share: int, default_n: int, now: datetime, lease_s: float,
               humans: set[str] | None = None) -> int:
    """How many more tasks this machine may claim right now."""
    active = active_claims(root, now, lease_s, humans)
    n = global_quota(root, default_n, humans)
    mine = sum(1 for _, c in active if c.machine == machine)
    return max(0, min(n - len(active), share - mine))


# ---------------------------------------------------------------------------
# Epic takeover (Ch.10.4) and selection
# ---------------------------------------------------------------------------

def active_takeovers(root) -> dict[str, set[str]]:
    """epic key -> machines holding an active takeover."""
    state: dict[tuple[str, str], bool] = {}
    for _, d in _sorted_records(Path(root) / "priority"):
        epic, machine = d.get("epic"), d.get("machine")
        if not epic or not machine:
            continue
        state[(str(epic), str(machine))] = d.get("action") == "takeover"
    out: dict[str, set[str]] = {}
    for (epic, machine), on in state.items():
        if on:
            out.setdefault(epic, set()).add(machine)
    return out


def order_candidates(candidates: Iterable[tuple[str, dict]], machine: str,
                     takeovers: dict[str, set[str]]) -> list[tuple[str, dict]]:
    """Sort (key, meta) pairs: tasks under another machine's takeover last,
    own takeovers first, then by import order and key (deterministic)."""
    def rank(item):
        key, meta = item
        holders = takeovers.get(str(meta.get("epic")), set())
        if machine in holders:
            tier = 0
        elif holders:
            tier = 2
        else:
            tier = 1
        return (tier, R.clock_of({"logical_clock": meta.get("logical_clock")}), key)
    return sorted(candidates, key=rank)


# ---------------------------------------------------------------------------
# Failure accounting (Ch.8) and thrash detection (Ch.9.4)
# ---------------------------------------------------------------------------

NON_FAILURE_WITHDRAWALS = {"lost-race", "released", "arbitration", "quota"}


def retry_count(task_dir, now: datetime, lease_s: float) -> int:
    task_dir = Path(task_dir)
    claims = read_claims(task_dir)
    withdrawn = read_withdrawals(task_dir)
    heartbeats = read_heartbeats(task_dir)
    outcome = read_outcome(task_dir)
    failures = 0
    for c in claims:
        if c.id in outcome.completed_claims:
            continue
        w = withdrawn.get(c.id)
        if w is not None:
            if w.get("reason") not in NON_FAILURE_WITHDRAWALS:
                failures += 1
        elif is_expired(c, heartbeats, now, lease_s):
            failures += 1
    return failures


def conflict_signature(res: Resolution) -> str | None:
    machines = sorted({c.machine for c in res.valid})
    if len(machines) < 2:
        return None
    return "|".join(machines)


def race_history(task_dir) -> dict[str, int]:
    """How many times each machine pair has raced for this task, from the
    ledger alone: pairs of claims whose withdrawal reason is lost-race."""
    task_dir = Path(task_dir)
    claims = {c.id: c for c in read_claims(task_dir)}
    counts: dict[str, int] = {}
    for w in read_withdrawals(task_dir).values():
        if w.get("reason") != "lost-race":
            continue
        loser = claims.get(str(w.get("claim_id")))
        winner = claims.get(str(w.get("winner")))
        if not loser or not winner:
            continue
        sig = "|".join(sorted({loser.machine, winner.machine}))
        counts[sig] = counts.get(sig, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Plan review gate (plan §2.8)
# ---------------------------------------------------------------------------

def plan_status(task_dir, humans: set[str] | None = None) -> str | None:
    """None (no plan yet) | pending-review | changes-requested | approved.
    The plan itself lives in checkpoint.yaml (plan_sha); approvals are
    append-only records from humans, or self-review for auto-pr tasks."""
    cp = R.load_yaml(Path(task_dir) / "checkpoint.yaml")
    sha = cp.get("plan_sha")
    if not sha:
        return None
    if cp.get("plan_self_approved") == sha:
        return "approved"
    status = "pending-review"
    for _, d in _sorted_records(Path(task_dir) / "plan-reviews"):
        if d.get("plan_sha") != sha:
            continue
        if humans is not None and d.get("human") not in humans:
            continue
        status = d.get("decision") or status
    return status
