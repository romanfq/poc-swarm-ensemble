"""What the Swarm Board shows (whitepaper Ch.10.5), as plain data.

Kept free of Textual so it is tested everywhere; ``bin/board.py`` only lays
these rows out and wires keys to ``dags.actions`` / ``dags.work``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dags import feed, snapshot

CLAIM_COLUMNS = ("task", "title", "machine", "human", "clock", "age", "worker", "state")
REVIEW_COLUMNS = ("task", "title", "PR", "review", "checks")
ARBITRATION_COLUMNS = ("task", "claimants", "why")
PLAN_COLUMNS = ("task", "title", "machine", "plan")

LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) \[([^\]]+)\] (.*)$")
# notification kinds the Board renders itself instead of echoing from the log
SKIP_KINDS = {"feed", "needs-worker"}


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def claim_rows(snap: snapshot.Snapshot) -> list[tuple[str, tuple]]:
    rows = []
    for t in snap.live_claims:
        w = t.winner
        rows.append((t.key, (t.short, t.title, w.machine, w.human or "?", str(w.clock),
                             age_text(t.claim_age_s(snap.now)), t.worker or "awaiting worker",
                             t.state + (" · needs human" if t.needs_human else "")
                             + (" · pausing (quota)" if t.pausing else ""))))
    return rows


def review_rows(snap: snapshot.Snapshot, pr_status: dict[str, dict]) -> list[tuple[str, tuple]]:
    rows = []
    for t in snap.awaiting_review:
        st = pr_status.get(t.key) or {}
        review = (st.get("review") or "pending").replace("_", " ").lower()
        rows.append((t.key, (t.short, t.title, t.pr_url or "", review, st.get("checks") or "?")))
    return rows


def poller_flags(swarm_dir: Path) -> list[str]:
    try:
        data = json.loads((Path(swarm_dir) / "poller-state.json").read_text())
    except (FileNotFoundError, ValueError):
        return []
    return list(data.get("needs_arbitration") or [])


def arbitration_rows(snap: snapshot.Snapshot, flagged: list[str]) -> list[tuple[str, tuple]]:
    rows = []
    keys = set(flagged) | {t.key for t in snap.conflicts}
    for t in snap.work:
        if t.key not in keys or t.res.arbitration is not None or t.state in ("done", "awaiting-review"):
            continue
        claimants = ", ".join(sorted({c.machine for c in t.res.valid})) or "-"
        why = "keeps racing (thrash)" if t.key in flagged else "live conflict"
        rows.append((t.key, (t.short, claimants, why)))
    return rows


def plan_rows(snap: snapshot.Snapshot) -> list[tuple[str, tuple]]:
    return [(t.key, (t.short, t.title, t.owner_machine or "-", t.plan_status or "-"))
            for t in snap.plans_pending()]


@dataclass
class Quota:
    used: int
    total: int
    mine: int
    share: int | None

    @property
    def text(self) -> str:
        share = "?" if self.share is None else str(self.share)
        return f"global {self.used}/{self.total}   ·   this machine {self.mine}/{share}"


def quota(snap: snapshot.Snapshot) -> Quota:
    return Quota(snap.quota_used, snap.quota_n, snap.mine_used, snap.share)


def machine_line(snap: snapshot.Snapshot, daemon_pid: int | None) -> str:
    st = snap.machines.get(snap.machine, {})
    if st.get("stopped") or not daemon_pid:
        state = "stopped" if st.get("stopped") else "daemon not running"
    elif st.get("paused"):
        state = "paused"
    else:
        state = "running"
    others = [f"{m}: {'paused' if s.get('paused') else 'stopped' if s.get('stopped') else 'on'}"
              for m, s in snap.machines.items() if m != snap.machine]
    tail = f"   ·   others — {', '.join(others)}" if others else ""
    return f"{snap.machine} · {state}{tail}"


def initial_feed(root: Path, seen: set[str], limit: int = 20) -> list[str]:
    events = feed.new_events(root, seen)
    return [e.text for e in events[-limit:]]


def epic_of(snap: snapshot.Snapshot, key: str) -> str | None:
    t = snap.by_key(key)
    return str(t.epic) if t and t.epic else None


def holds_takeover(snap: snapshot.Snapshot, epic: str) -> bool:
    return snap.machine in snap.takeovers.get(epic, set())


class LogTail:
    """New entries appended to .swarm/notifications.log since the Board started."""

    def __init__(self, path: Path, from_start: bool = False):
        self.path = Path(path)
        self.offset = 0 if from_start or not self.path.exists() else self.path.stat().st_size
        self.partial = ""

    def read(self) -> list[tuple[str, str]]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0                       # rotated/truncated
        with open(self.path, encoding="utf-8") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        entries: list[list[str]] = []
        for line in chunk.splitlines():
            m = LOG_LINE.match(line)
            if m:
                entries.append([m.group(2), m.group(3)])
            elif entries:
                entries[-1][1] += "\n" + line
        return [(k, t) for k, t in entries if k not in SKIP_KINDS]


def announcement(kind: str, text: str) -> str:
    return f"[swarm-board] {text}"
