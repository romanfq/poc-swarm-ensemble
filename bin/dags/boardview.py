"""What the Swarm Board shows (whitepaper Ch.10.5), as plain data.

Kept free of Textual so it is tested everywhere; ``bin/board.py`` only lays
these rows out and wires keys to ``dags.actions`` / ``dags.work``.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from dags import daemon, feed, snapshot

CLAIM_COLUMNS = ("task", "title", "machine", "human", "clock", "age", "worker", "state")
REVIEW_COLUMNS = ("task", "title", "PR", "review", "checks")
ARBITRATION_COLUMNS = ("task", "claimants", "why")
PLAN_COLUMNS = ("task", "title", "machine", "plan")

# what a click (or Enter) on a table cell opens: the column's own link, else the table's default
LINK_COLUMNS = {"task": "ticket", "PR": "pr"}
DEFAULT_LINK = {"review": "pr"}

URL = re.compile(r"https?://[^\s<>\"']+")
URL_TRAILING = ".,;:!?)]}'\""

LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) \[([^\]]+)\] (.*)$")
# notification kinds the Board renders itself instead of echoing from the log
SKIP_KINDS = {"feed", "needs-worker", "dispatched"}


def find_urls(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) for each http(s) link in text, minus trailing punctuation."""
    found = []
    for m in URL.finditer(text):
        url = m.group(0).rstrip(URL_TRAILING)
        if "://" in url and not url.endswith("://"):
            found.append((m.start(), m.start() + len(url), url))
    return found


def link_kind(table_id: str, column: str | None) -> str:
    """Which link a selected cell opens: "pr" or "ticket"."""
    return LINK_COLUMNS.get(column or "") or DEFAULT_LINK.get(table_id, "ticket")


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def short_error(text: str, width: int = 80) -> str:
    """First meaningful line of an error, cut to fit a table cell."""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    line = next((ln for ln in lines if ln.startswith("fatal:")), lines[0] if lines else "")
    return line if len(line) <= width else line[:width - 1] + "…"


def dispatch_failed_text(t: snapshot.TaskView) -> str:
    f = t.dispatch_failed
    if not f:
        return ""
    return f"dispatch failed ×{f.get('attempts') or 1}: {short_error(f.get('error') or '')}"


def claim_rows(snap: snapshot.Snapshot) -> list[tuple[str, tuple]]:
    rows = []
    for t in snap.live_claims:
        w = t.winner
        failed = dispatch_failed_text(t)
        worker = t.worker or ("not started" if failed else "awaiting worker")
        rows.append((t.key, (t.short, t.title, w.machine, w.human or "?", str(w.clock),
                             age_text(t.claim_age_s(snap.now)), worker,
                             t.state + (" · needs human" if t.needs_human else "")
                             + (" · pausing (quota)" if t.pausing else "")
                             + (f" · {failed}" if failed else ""))))
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


def machine_line(snap: snapshot.Snapshot, daemon_pid: int | None, info: dict | None = None) -> str:
    st = snap.machines.get(snap.machine, {})
    if st.get("stopped") or not daemon_pid:
        state = "stopped" if st.get("stopped") else "daemon not running"
    else:
        state = "paused" if st.get("paused") else "running"
        up = daemon.uptime_text(info or {})
        if up:
            state += f" ({up})"
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


# -- the daemon's own log (GH-10) ------------------------------------------------------

DAEMON_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)? (DEBUG|INFO|WARNING|ERROR|CRITICAL) (\S+): (.*)$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# what the Board's `l` key steps through, starting from the default
LEVEL_CYCLE = ("WARNING", "INFO", "ERROR")


@dataclass
class LogEntry:
    time: str
    level: str
    logger: str
    text: str

    @property
    def line(self) -> str:
        head = f"{self.time} {self.level}"
        return f"{head} {self.logger}: {self.text}" if self.logger else f"{head} {self.text}"


def next_level(level: str) -> str:
    i = LEVEL_CYCLE.index(level) if level in LEVEL_CYCLE else -1
    return LEVEL_CYCLE[(i + 1) % len(LEVEL_CYCLE)]


class DaemonLogTail:
    """Entries of this machine's .swarm/swarm.log at or above ``level``.

    ``backlog()`` returns the last ``limit`` of them already in the file, so an
    error from before the Board opened is still shown; ``read()`` returns what
    was appended since. Lines that don't start a log record (tracebacks, a
    worker's stray output) belong to the record before them. ``generation``
    changes on every ``backlog()``, so a ``read()`` that raced a reload can be
    told apart and dropped.
    """

    def __init__(self, path: Path, level: str = "WARNING", limit: int = 50, max_bytes: int = 512_000):
        self.path = Path(path)
        self.level = level
        self.limit = limit
        self.max_bytes = max_bytes
        self.offset = 0
        self.partial = ""
        self._last_level = "INFO"
        self.generation = 0
        self._lock = threading.Lock()

    def keep(self, e: LogEntry) -> bool:
        return LEVELS.get(e.level, 0) >= LEVELS.get(self.level, 30)

    def _parse(self, chunk: str) -> list[LogEntry]:
        text = self.partial + chunk
        complete, _, self.partial = text.rpartition("\n")
        entries: list[LogEntry] = []
        for line in complete.splitlines():
            m = DAEMON_LOG_LINE.match(line)
            if m:
                entries.append(LogEntry(m.group(1), m.group(2), m.group(3), m.group(4)))
                self._last_level = m.group(2)
            elif entries:
                entries[-1].text += "\n" + line
            elif line.strip():
                entries.append(LogEntry("", self._last_level, "", line))
        return entries

    def _chunk(self, start: int) -> str:
        with open(self.path, encoding="utf-8", errors="replace") as f:
            f.seek(start)
            chunk = f.read()
            self.offset = f.tell()
        return chunk

    def backlog(self) -> list[LogEntry]:
        with self._lock:
            self.generation += 1
            return self._backlog()

    def _backlog(self) -> list[LogEntry]:
        self.offset, self.partial = 0, ""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        start = max(0, size - self.max_bytes)
        chunk = self._chunk(start)
        if start:
            chunk = chunk.split("\n", 1)[1] if "\n" in chunk else ""   # drop the cut first line
        return [e for e in self._parse(chunk) if self.keep(e)][-self.limit:]

    def read(self) -> list[LogEntry]:
        return self.read_tagged()[1]

    def read_tagged(self) -> tuple[int, list[LogEntry]]:
        """``read()`` plus the generation it belongs to."""
        with self._lock:
            if not self.path.exists():
                return self.generation, []
            if self.path.stat().st_size < self.offset:
                self.offset, self.partial = 0, ""        # rotated/truncated
            return self.generation, [e for e in self._parse(self._chunk(self.offset)) if self.keep(e)]
