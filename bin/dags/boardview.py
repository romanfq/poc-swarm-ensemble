"""What the Swarm Board shows (whitepaper Ch.10.5), as plain data.

Kept free of Textual so it is tested everywhere; ``bin/board.py`` only lays
these rows out and wires keys to ``dags.actions`` / ``dags.work``.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dags import daemon, feed, snapshot

CLAIM_COLUMNS = ("task", "title", "machine", "human", "clock", "lease", "worker", "state")
REVIEW_COLUMNS = ("task", "title", "PR", "review", "checks")
ARBITRATION_COLUMNS = ("task", "claimants", "why")
PLAN_COLUMNS = ("task", "title", "machine", "plan")
TEST_COLUMNS = ("task", "machine", "recommended", "reason", "time")

# the five left-hand panels: (table id, title)
PANELS = (("claims", "live claims"), ("review", "awaiting review"), ("arbitration", "needs arbitration"),
          ("plans", "plans awaiting review"), ("tests", "test questions"))

# secondary columns the Board dims (it never styles here: names only, board.py resolves them)
SECONDARY_COLUMNS = ("machine", "human", "clock")

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


BAR_GLYPHS = " ▏▎▍▌▋▊▉█"          # eighths of a cell: plain Unicode, no Nerd Font


def bar_text(fraction: float | None, width: int = 8) -> str:
    """A block-character bar, `fraction` of `width` cells full. The length alone carries
    the value, so it reads with colour off."""
    if fraction is None:
        return "·" * width
    eighths = round(min(max(fraction, 0.0), 1.0) * width * 8)
    full, part = divmod(eighths, 8)
    bar = "█" * full + (BAR_GLYPHS[part] if part else "")
    return bar.ljust(width, "░")


def level_of(fraction: float | None) -> str:
    """Semantic name for how burnt down a lease is: ok / warn / crit (/ plain when unknown)."""
    if fraction is None:
        return "plain"
    return "crit" if fraction >= 0.85 else "warn" if fraction >= 0.5 else "ok"


def lease_elapsed_s(snap: snapshot.Snapshot, t: snapshot.TaskView) -> float | None:
    """Seconds since the winning claim was last seen (claim or heartbeat), Ch.6.5."""
    import resolve
    if not t.winner:
        return None
    seen = resolve.last_seen(t.winner, resolve.read_heartbeats(t.dir))
    return None if seen is None else max(0.0, (snap.now - seen).total_seconds())


def lease_fraction(elapsed_s: float | None, lease_s: float) -> float | None:
    """How much of the lease has burnt down: 0 just renewed, 1 expired."""
    if elapsed_s is None or lease_s <= 0:
        return None
    return min(max(elapsed_s / lease_s, 0.0), 1.0)


def lease_cell(elapsed_s: float | None, lease_s: float, width: int = 8) -> str:
    """`█████░░░ 90s`: the bar fills as the lease burns down, then the time left."""
    fraction = lease_fraction(elapsed_s, lease_s)
    if fraction is None:
        return "-"
    left = max(0, int(lease_s - elapsed_s))
    return f"{bar_text(fraction, width)} {f'{left}s' if left < 120 else age_text(left)}"


def claim_leases(snap: snapshot.Snapshot) -> dict[str, float | None]:
    """task key -> fraction of the lease burnt, for the claims table."""
    return {t.key: lease_fraction(lease_elapsed_s(snap, t), snap.lease_s) for t in snap.live_claims}


def short_error(text: str, width: int = 80) -> str:
    """First meaningful line of an error, cut to fit a table cell."""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    line = next((ln for ln in lines if ln.startswith("fatal:")), lines[0] if lines else "")
    return line if len(line) <= width else line[:width - 1] + "…"


def feed_line(text: str) -> str:
    """An event line for the Activity panel: free text a human or worker wrote (a question,
    an answer, a note) is left out, so one event stays one line. feed.py keeps the whole text
    for notifications.log and `swarm.py log`; the `y` dialog shows the full question."""
    return text.split(feed.QUOTE_SEP, 1)[0]


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
        plan_hint = None
        if t.plan_status == "not submitted":
            plan_hint = "plan not submitted"
        elif t.plan_status == "changed since submitted":
            plan_hint = "plan changed since submitted"
        rows.append((t.key, (t.short, t.title, w.machine, w.human or "?", str(w.clock),
                             lease_cell(lease_elapsed_s(snap, t), snap.lease_s), worker,
                             t.state + (" · needs human" if t.needs_human else "")
                             + (" · pausing (quota)" if t.pausing else "")
                             + (f" · {plan_hint}" if plan_hint else "")
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


def test_estimate_text(proposal: dict, scope: str) -> str:
    """Time for a scope from the slowest tests recorded in the checkpoint (a floor)."""
    secs = ((proposal.get("options") or {}).get(scope) or {}).get("seconds")
    return "?" if secs is None else f"≥{age_text(secs)}"


def test_rows(snap: snapshot.Snapshot) -> list[tuple[str, tuple]]:
    """Open "which tests may the worker run?" questions (GH-50)."""
    rows = []
    for t in snap.tests_pending():
        p = t.test_scope["proposal"]
        rec = p.get("recommendation", "full")
        rows.append((t.key, (t.short, t.owner_machine or "-", rec, p.get("reason", ""),
                             test_estimate_text(p, rec))))
    return rows


def test_question_text(proposal: dict) -> str:
    """What the answer dialog shows: the options, the recommendation and each option's time."""
    from dags import testscope
    lines = [testscope.render_text(proposal), "", "Estimated time (slowest recorded tests): "
             + ", ".join(f"{s} {test_estimate_text(proposal, s)}" for s in testscope.SCOPES)]
    return "\n".join(lines)


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
        if st.get("paused"):
            state = "paused — claiming nothing (r resumes)"
        elif snap.share == 0:
            state = "share 0 — out of rotation (t sets a share)"
        else:
            state = "running"
        up = daemon.uptime_text(info or {})
        if up:
            state += f" ({up})"
    others = [f"{m}: {'paused' if s.get('paused') else 'stopped' if s.get('stopped') else 'on'}"
              for m, s in snap.machines.items() if m != snap.machine]
    tail = f"   ·   others — {', '.join(others)}" if others else ""
    return f"{snap.machine} · {state}{tail}"


def poller_age_s(swarm_dir: Path, now: datetime | None = None) -> float | None:
    """Seconds since the poller last saved its state (it does so every cycle), else None."""
    from dags import timeutil
    try:
        mtime = (Path(swarm_dir) / "poller-state.json").stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now or timeutil.now()).timestamp() - mtime)


def machine_state(snap: snapshot.Snapshot, daemon_pid: int | None) -> tuple[str, str]:
    """(word, style name) for this machine: running / paused / throttled / stopped."""
    st = snap.machines.get(snap.machine, {})
    if st.get("stopped"):
        return "stopped", "off"
    if not daemon_pid:
        return "daemon not running", "off"
    if st.get("paused"):
        return "paused", "warn"
    if snap.share == 0:
        return "throttled (share 0)", "warn"
    return "running", "ok"


def header_rows(snap: snapshot.Snapshot, daemon_pid: int | None, info: dict | None, operator: str,
                poller_age: float | None = None) -> list[tuple[str, str, str]]:
    """The Board's header block as `[(label, value, style name)]`, like dags.panel's rows."""
    word, style = machine_state(snap, daemon_pid)
    up = daemon.uptime_text(info or {}) if daemon_pid else ""
    state = word + (f" ({up})" if up else "")
    if poller_age is None:
        poller = "poller: no cycle yet"
    else:
        fraction = lease_fraction(poller_age, snap.lease_s)
        poller = f"poller: {bar_text(fraction, 4)} {age_text(poller_age)} ago"
        if style == "ok" and level_of(fraction) != "ok":
            style = level_of(fraction)
    others = [f"{m}: {'paused' if s.get('paused') else 'stopped' if s.get('stopped') else 'on'}"
              for m, s in snap.machines.items() if m != snap.machine]
    rows = [("machine", snap.machine, "plain"), ("operator", operator, "plain"),
            ("state", f"{state} · {poller}", style)]
    if others:
        rows.append(("others", ", ".join(others), "dim"))
    return rows


def panel_summary(snap: snapshot.Snapshot, counts: dict[str, int]) -> list[tuple[str, str, int, str]]:
    """The collapsed strip: `[(table id, title, count, style name)]`, style being "accent"
    when the panel holds something a human must act on, "dim" when it is empty."""
    human = {"claims": any(t.needs_human for t in snap.live_claims),
             "arbitration": counts.get("arbitration", 0) > 0,
             "plans": counts.get("plans", 0) > 0,
             "tests": counts.get("tests", 0) > 0}
    out = []
    for tid, title in PANELS:
        n = counts.get(tid, 0)
        out.append((tid, title, n, "accent" if n and human.get(tid) else "dim" if n == 0 else "plain"))
    return out


def idle_text(snap: snapshot.Snapshot, identity: str) -> str:
    """What the Board says when nothing is live: what is ready, what is blocked, how to start."""
    ready = len(snap.ready)
    blocked = sum(1 for t in snap.work if t.state == "open" and not t.ready)
    return "\n".join([
        "Nothing is running.",
        "",
        f"{ready} ready to claim · {blocked} blocked",
        "",
        "Start this machine:",
        f"  ./bin/swarm.py --identity {identity} start --quota-share 1",
    ])


def initial_feed(root: Path, seen: set[str], limit: int = 20) -> list[str]:
    events = feed.new_events(root, seen)
    return [feed_line(e.text) for e in events[-limit:]]


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


def merge_prompt(pr_url: str | None, short: str, checks: str | None) -> tuple[str, bool]:
    """The merge confirmation text, and whether confirming means merging over red checks (GH-29)."""
    base = f"Approve and squash-merge {pr_url} ({short})?"
    if checks == "failing":
        return f"CHECKS ARE FAILING on {pr_url}. Merge {short} anyway?", True
    if checks == "pending":
        return f"{base} Its checks are still running.", False
    return base, False
