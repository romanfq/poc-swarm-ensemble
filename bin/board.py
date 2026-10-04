#!/usr/bin/env python3
"""The Swarm Board (whitepaper Ch.10) — a Textual command centre for humans.

    ./bin/swarm.py board            # in the terminal
    ./bin/swarm.py board --web      # textual serve on http://localhost:4590

It reads the same local clone the poller syncs, shells out to gh only for PR
status and Approve & merge, and every button either writes an ordinary ledger
record (dags.actions / dags.ledger) or runs the gh command a human would type.
What it shows is computed in dags.boardview so it can be tested without Textual.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import threading  # noqa: E402
from functools import partial  # noqa: E402

from rich.markup import escape  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.command import DiscoveryHit, Hit, Provider  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, Markdown,  # noqa: E402
                             OptionList, ProgressBar, RichLog, Static)
from textual.widgets.option_list import Option  # noqa: E402

import workers  # noqa: E402
from dags import actions, boardview, daemon, feed, gh, snapshot, worktree  # noqa: E402
from dags import work as worklib  # noqa: E402
from dags.config import Context  # noqa: E402

# ---------------------------------------------------------------------------
# appearance as data (GH-67): board.tcss refers to these tokens by name ($navy ...) and
# cell styling resolves boardview's semantic names through STYLES, never anywhere else.
# ---------------------------------------------------------------------------

PALETTE = {"navy": "#15243C", "slate": "#4A5A75", "mid": "#6B7C99",
           "rust": "#B23A2F", "pale": "#EEF2F8", "line": "#CFD8E6"}

# boardview's semantic style names -> Rich styles for Text.stylize
STYLES = {
    "plain": "", "dim": f"{PALETTE['mid']}",
    "ok": "#5FA77A", "warn": "#D9A441", "crit": f"bold {PALETTE['rust']}",
    "off": f"{PALETTE['mid']}", "accent": f"bold {PALETTE['rust']}",
}
LOG_STYLES = {"WARNING": STYLES["warn"], "ERROR": STYLES["crit"], "CRITICAL": STYLES["crit"],
              "DEBUG": STYLES["dim"]}

# ---------------------------------------------------------------------------
# links (GH-5): Textual captures the mouse, so the terminal's own Cmd-click
# never sees a URL. Links are made clickable here and open via BoardApp.open_link.
# ---------------------------------------------------------------------------

def link_style(url: str) -> Style:
    return Style(underline=True, link=url) + Style.from_meta({"@click": f"app.open_link({url!r})"})


def linkify(text: str) -> Text:
    """text with each http(s) URL underlined and opening on click."""
    out = Text(text)
    for start, end, url in boardview.find_urls(text):
        out.stylize(link_style(url), start, end)
    return out


class LinkTable(DataTable):
    """A DataTable that remembers which column a click landed on, so selecting a
    row (a click on the highlighted row, or Enter) can open that cell's link."""

    clicked_column: str | None = None

    async def _on_click(self, event) -> None:
        # no super(): Textual dispatches DataTable._on_click itself, after this one
        row, column = event.style.meta.get("row"), event.style.meta.get("column")
        self.clicked_column = None
        if not (isinstance(row, int) and isinstance(column, int) and row >= 0
                and 0 <= column < len(self.ordered_columns)):
            return
        self.clicked_column = str(self.ordered_columns[column].label)
        if row == self.cursor_row:
            # the row is already highlighted: whichever cell was clicked, DataTable
            # should treat it as a click on the cursor and post RowSelected
            self.cursor_coordinate = Coordinate(row, column)

    def action_select_cursor(self) -> None:
        self.clicked_column = None
        super().action_select_cursor()


def _key(keys: str, action: str, label: str) -> Binding:
    return Binding(keys, f"app.{action}", label)


# Each panel lists its own actions: the footer shows the focused panel's, not all of them.
class ClaimsTable(LinkTable):
    BINDINGS = [_key("w", "choose_worker", "Worker"), _key("y", "answer_question", "Answer"),
                _key("f", "freeze", "Freeze"), _key("p", "pause", "Pause"), _key("o", "open_ticket", "Ticket")]


class ReviewTable(LinkTable):
    BINDINGS = [_key("m", "merge", "Merge"), _key("O,shift+o", "open_pr", "PR"), _key("o", "open_ticket", "Ticket")]


class ArbitrationTable(LinkTable):
    BINDINGS = [_key("a", "reassign", "Reassign"), _key("f", "freeze", "Freeze"),
                _key("o", "open_ticket", "Ticket")]


class PlansTable(LinkTable):
    BINDINGS = [_key("v", "review_plan", "Review"), _key("o", "open_ticket", "Ticket")]


class TestsTable(LinkTable):
    BINDINGS = [_key("x", "answer_tests", "Answer"), _key("o", "open_ticket", "Ticket")]


PANEL_TABLES = {"claims": ClaimsTable, "review": ReviewTable, "arbitration": ArbitrationTable,
                "plans": PlansTable, "tests": TestsTable}

# what the ':' command bar offers: (action, name, help)
COMMANDS = [
    ("pause", "Pause this machine", "p: claim nothing new, keep heartbeating"),
    ("resume", "Resume this machine", "r"),
    ("throttle", "Set this machine's quota share", "t: 0 takes it out of rotation"),
    ("stop", "Stop the swarm on this machine", "s"),
    ("choose_worker", "Choose a worker", "w"),
    ("freeze", "Freeze or unfreeze the selected task", "f"),
    ("reassign", "Reassign the selected task", "a"),
    ("takeover", "Take over or release the epic", "e"),
    ("review_plan", "Review the selected plan", "v"),
    ("answer_tests", "Answer a test-scope question", "x"),
    ("answer_question", "Answer a worker's question", "y"),
    ("merge", "Approve and merge the selected PR", "m"),
    ("open_ticket", "Open the ticket", "o"),
    ("open_pr", "Open the PR", "O"),
    ("set_quota", "Set the global quota N", "n"),
    ("log_level", "Cycle the daemon log level", "l"),
    ("refresh", "Refresh now", "ctrl+r"),
    ("help", "Help", "?"),
    ("quit", "Quit the Board", "q"),
]


class BoardCommands(Provider):
    """Every Board action, behind ':' (k9s style)."""

    async def discover(self):
        for action, name, hint in COMMANDS:
            yield DiscoveryHit(name, partial(self.app.run_action, action), help=hint)

    async def search(self, query: str):
        matcher = self.matcher(query)
        for action, name, hint in COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), partial(self.app.run_action, action), help=hint)


# ---------------------------------------------------------------------------
# modal screens
# ---------------------------------------------------------------------------



class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Cancel"), Binding("y", "yes", "Yes"), Binding("n", "no", "No")]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, markup=False)
            with Horizontal(id="buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


def as_int(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class InputScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, placeholder: str = "", value: str = "", numeric: bool = False):
        super().__init__()
        self.prompt, self.placeholder, self.value, self.numeric = prompt, placeholder, value, numeric

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, markup=False)
            yield Input(value=self.value, placeholder=self.placeholder, id="answer",
                        type="integer" if self.numeric else "text")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(text or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(ModalScreen[str | None]):
    """A question with a few options; letters a, b, c… pick directly (Ch.10.7)."""
    BINDINGS = [Binding("escape", "cancel", "Not now"),
                *[Binding(k, f"pick('{k}')", show=False) for k in "abcdef"]]

    def __init__(self, question: str, options: list[tuple[str, str]], letters: list[str] | None = None):
        super().__init__()
        self.question = question
        self.options = options
        self.letters = letters

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, markup=False)
            prompts = []
            for i, (oid, label) in enumerate(self.options):
                text = f"{self.letters[i]}) {label}" if self.letters else label
                prompts.append(Option(Text(text), id=oid))
            yield OptionList(*prompts, id="choices")

    def on_mount(self) -> None:
        self.query_one("#choices", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_pick(self, letter: str) -> None:
        if self.letters and letter in self.letters:
            self.dismiss(self.options[self.letters.index(letter)][0])

    def on_click(self, event) -> None:
        if event.widget is self:                      # a click outside the dialog
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """Taking this machine out of rotation

  p  pause     Claims nothing new; keeps heartbeating what it holds.
               Every machine sees it. r resumes.
  t  share 0   Claims nothing new AND releases what it holds (running work
               checkpoints, then goes a lease later). Survives a daemon
               restart (the share lives in the ledger). t sets a share again.
  s  stop      The daemon shuts down. Claims lapse when their leases expire.

Pause keeps your claims alive; share 0 and stop let them go.

In the worker prompt: Esc, a click outside, or "Not now" leaves the task
claimed and unassigned (press w to choose later)."""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", show=False),
                Binding("q", "close", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(HELP_TEXT, markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class PlanScreen(ModalScreen[str | None]):
    """Review gate for human-must-review plans (plan §2.8)."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, task: str, plan_md: str, links: list[tuple[str, str]] | None = None,
                 submit_mode: bool = False):
        super().__init__()
        # not self.task: Textual's MessagePump already owns that name.
        self.task_key = task
        self.plan_md = plan_md
        self.links = links or []
        self.submit_mode = submit_mode

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Plan for {self.task_key}", markup=False)
            for name, url in self.links:
                yield Static(linkify(f"{name}: {url}"), classes="link", markup=False)
            with VerticalScroll(id="plan"):
                yield Markdown(self.plan_md or "_empty plan_")
            with Horizontal(id="buttons"):
                if self.submit_mode:
                    yield Button("Submit & approve", id="submit-approved", variant="success")
                    yield Button("Submit & request changes", id="submit-changes-requested", variant="warning")
                    yield Button("Submit only", id="submit-only")
                else:
                    yield Button("Approve", id="approved", variant="success")
                    yield Button("Request changes", id="changes-requested", variant="warning")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ScopeQuestionScreen(ModalScreen[tuple[str, bool] | None]):
    """A worker asks which tests it may run (GH-50). The answer is (scope, targeted_is_enough)."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, task: str, text: str, recommended: str):
        super().__init__()
        self.task_key = task
        self.text = text
        self.recommended = recommended

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Which tests may {self.task_key}'s worker run?", markup=False)
            with VerticalScroll(id="plan"):
                yield Static(self.text, markup=False)
            yield Checkbox("Targeted is enough: done won't run the full suite", id="enough")
            with Horizontal(id="buttons"):
                for scope, label in (("none", "None"), ("targeted", "Targeted"),
                                     ("neighbours", "+ neighbours"), ("full", "Full")):
                    yield Button(label + (" (recommended)" if scope == self.recommended else ""), id=scope,
                                 variant="success" if scope == self.recommended else "default")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        enough = self.query_one("#enough", Checkbox).value and event.button.id != "full"
        self.dismiss((str(event.button.id), enough))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# the Board
# ---------------------------------------------------------------------------

class BoardApp(App):
    TITLE = "DAGS Swarm Board"
    CSS_PATH = "board.tcss"
    COMMANDS = App.COMMANDS | {BoardCommands}

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **PALETTE}

    # The footer shows the focused panel's actions (see the *Table classes) plus these four;
    # every other key still works from anywhere and is listed behind ':'.
    BINDINGS = [
        Binding("tab", "cycle_panel(1)", "Panel", priority=True),
        Binding("shift+tab", "cycle_panel(-1)", "Panel", show=False, priority=True),
        Binding("colon", "command_palette", "Commands"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("p", "pause", "Pause", show=False),
        Binding("r", "resume", "Resume", show=False),
        Binding("t", "throttle", "Throttle", show=False),
        Binding("s", "stop", "Stop", show=False),
        Binding("w", "choose_worker", "Worker", show=False),
        Binding("f", "freeze", "Freeze/unfreeze", show=False),
        Binding("a", "reassign", "Reassign", show=False),
        Binding("e", "takeover", "Take over epic", show=False),
        Binding("v", "review_plan", "Review plan", show=False),
        Binding("x", "answer_tests", "Answer tests", show=False),
        Binding("y", "answer_question", "Answer worker", show=False),
        Binding("m", "merge", "Approve & merge", show=False),
        Binding("o", "open_ticket", "Ticket", show=False),
        Binding("O,shift+o", "open_pr", "PR", show=False),
        Binding("n", "set_quota", "Global N", show=False),
        Binding("l", "log_level", "Log level", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, ctx: Context, refresh_s: float = 5.0, launch=None, platform: str | None = None,
                 use_gh: bool = True, run_poller: bool | None = None, open_url=None):
        super().__init__()
        self.ctx = ctx
        self.refresh_s = refresh_s
        self.launch = launch
        self.platform = platform
        self.use_gh = use_gh
        self.run_poller = run_poller
        self._open_link = open_url             # None: Textual's App.open_url (a browser tab under --web)
        self.snap: snapshot.Snapshot | None = None
        self.pr_status: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.tail = boardview.LogTail(ctx.swarm_dir / "notifications.log")
        self.daemon_log = boardview.DaemonLogTail(ctx.swarm_dir / daemon.LOGFILE)
        self.prompted: set[str] = set()
        self.prompt_open = False
        self._poller = None
        self._refresh_lock = threading.Lock()
        self.said: list[str] = []
        self.panel = "claims"                     # the focused (expanded) left panel
        self.counts: dict[str, int] = {}

    # -- layout ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="header-block"):
            yield Static("", id="machine", markup=False)
            with Horizontal(id="quota-row"):
                yield Label("quota", id="quota-name", markup=False)
                yield ProgressBar(total=1, show_eta=False, id="quota")
                yield Label("", id="quota-label", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                for tid, _ in boardview.PANELS:
                    yield PANEL_TABLES[tid](id=tid, cursor_type="row")
                yield Static("", id="idle", markup=False, classes="hidden")
                yield Static("", id="strip", markup=False)
            with Vertical(id="right"):
                yield RichLog(id="feed", wrap=True, markup=False, highlight=False, max_lines=500)
                yield Static("", id="daemon-log-title", classes="panel-title", markup=False)
                yield RichLog(id="daemon-log", wrap=True, markup=False, highlight=False, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.ctx.identity} · operator {self.ctx.operator}"
        for tid, cols in (("claims", boardview.CLAIM_COLUMNS), ("review", boardview.REVIEW_COLUMNS),
                          ("arbitration", boardview.ARBITRATION_COLUMNS), ("plans", boardview.PLAN_COLUMNS),
                          ("tests", boardview.TEST_COLUMNS)):
            self.query_one(f"#{tid}", DataTable).add_columns(*cols)
        self.query_one("#header-block").border_title = "swarm"
        self.query_one("#feed").border_title = "activity"
        for tid, title in boardview.PANELS:
            self.query_one(f"#{tid}").border_title = f"{title} (0)"
        self.show_panel("claims")
        log = self.query_one("#feed", RichLog)
        for line in boardview.initial_feed(self.ctx.root, self.seen):
            log.write(linkify(line))
        self.load_daemon_log()
        self.refresh_data()
        self.set_interval(self.refresh_s, self.refresh_data)

    # -- data ------------------------------------------------------------------------
    @work(thread=True, group="refresh", exit_on_error=False)
    def refresh_data(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return                                  # a refresh is already running
        try:
            self._refresh()
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, escape(f"refresh failed: {e}"), severity="warning")
        finally:
            self._refresh_lock.release()

    def _refresh(self) -> None:
        ctx = self.ctx
        try:
            ctx.coord.pull()
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, escape(f"sync failed: {e}"), severity="warning")
        run_poller = self.run_poller if self.run_poller is not None else daemon.running_pid(ctx) is None
        if run_poller:
            if self._poller is None:
                from poll import Poller
                from dags.notify import Notifier
                self._poller = Poller(ctx, Notifier(ctx.swarm_dir, ctx.local), use_gh=self.use_gh)
            try:
                rep = self._poller.cycle()
                self.pr_status.update(rep.pr_status)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self.notify, escape(f"poller: {e}"), severity="warning")
        snap = snapshot.take(ctx)
        if self.use_gh and not run_poller:
            for t in snap.awaiting_review:
                found = gh.repo_from_pr_url(t.pr_url or "")
                if not found:
                    continue
                try:
                    pr = gh.pr_view(*found)
                    self.pr_status[t.key] = {"state": pr.get("state"), "review": pr.get("reviewDecision"),
                                             "checks": gh.checks_summary(pr), "url": t.pr_url}
                except Exception:  # noqa: BLE001
                    pass
        new_feed = [e.text for e in feed.new_events(ctx.root, self.seen)]
        notes = self.tail.read()
        logged = self.daemon_log.read_tagged()
        flagged = boardview.poller_flags(ctx.swarm_dir)
        pid = daemon.running_pid(ctx)
        info = daemon.info(ctx) if pid else {}
        self.call_from_thread(self.apply, snap, new_feed, notes, flagged, pid, logged, info)

    # which cells carry an accent: only what needs a human (restraint, GH-67)
    ACCENT_COLUMN = {"arbitration": "why", "plans": "plan", "tests": "task"}

    def _cell(self, tid: str, key: str, column: str, text: str, linked: bool, leases: dict) -> Text:
        cell = Text(text, style="underline" if linked and text else "")
        if column in boardview.SECONDARY_COLUMNS:
            cell.stylize(STYLES["dim"])
        elif column == "lease":
            cell.stylize(STYLES[boardview.level_of(leases.get(key))])
        elif column == self.ACCENT_COLUMN.get(tid):
            cell.stylize(STYLES["accent"])
        elif column == "state":
            at = text.find("needs human")
            if at >= 0:
                cell.stylize(STYLES["accent"], at, at + len("needs human"))
        return cell

    def _fill(self, tid: str, rows: list[tuple[str, tuple]], leases: dict | None = None) -> None:
        table = self.query_one(f"#{tid}", DataTable)
        selected = self.selected_key(table)
        table.clear()
        names = [str(c.label) for c in table.ordered_columns]
        for key, cells in rows:
            table.add_row(*(self._cell(tid, key, names[i], str(c), names[i] in boardview.LINK_COLUMNS and bool(c),
                                       leases or {}) for i, c in enumerate(cells)), key=key)
        if selected is not None:
            for i, (key, _) in enumerate(rows):
                if key == selected:
                    try:
                        table.move_cursor(row=i)
                    except Exception:  # noqa: BLE001
                        pass
                    break

    # -- focus-driven panels (GH-67) -----------------------------------------------------
    def show_panel(self, tid: str) -> None:
        """Expand one left panel to fill the column; the rest collapse into the strip."""
        self.panel = tid
        for pid, _ in boardview.PANELS:
            self.query_one(f"#{pid}").set_class(pid != tid, "collapsed")
        self.query_one(f"#{tid}").focus()
        self.render_strip()

    def render_strip(self) -> None:
        strip = Text()
        for tid, title, n, style in (boardview.panel_summary(self.snap, self.counts) if self.snap else []):
            if tid == self.panel:
                continue
            if strip:
                strip.append("  ")
            strip.append(f"{title} {n}", style=STYLES[style])
        self.query_one("#strip", Static).update(strip)

    def action_cycle_panel(self, step: int = 1) -> None:
        if self.busy():
            (self.screen.focus_next if step > 0 else self.screen.focus_previous)()
            return
        ids = [t for t, _ in boardview.PANELS]
        live = [t for t in ids if self.counts.get(t)] or ids
        at = live.index(self.panel) if self.panel in live else -1
        self.show_panel(live[(at + step) % len(live)])

    def on_descendant_focus(self, event) -> None:
        widget = event.widget
        if isinstance(widget, LinkTable) and widget.id != self.panel and not widget.has_class("collapsed"):
            self.show_panel(str(widget.id))

    def apply(self, snap, new_feed, notes, flagged, pid, logged, info=None) -> None:
        self.snap = snap
        header = Text()
        for i, (label, value, style) in enumerate(boardview.header_rows(
                snap, pid, info, self.ctx.operator, boardview.poller_age_s(self.ctx.swarm_dir, snap.now))):
            header.append(("\n" if i else "") + label.ljust(9), style=STYLES["dim"])
            header.append(value, style=STYLES[style])
        self.query_one("#machine", Static).update(header)
        q = boardview.quota(snap)
        self.query_one("#quota-label", Label).update(q.text)
        self.query_one("#quota", ProgressBar).update(total=max(q.total, 1), progress=min(q.used, max(q.total, 1)))
        tables = {"claims": boardview.claim_rows(snap), "review": boardview.review_rows(snap, self.pr_status),
                  "arbitration": boardview.arbitration_rows(snap, flagged), "plans": boardview.plan_rows(snap),
                  "tests": boardview.test_rows(snap)}
        for tid, rows in tables.items():
            self._fill(tid, rows, boardview.claim_leases(snap) if tid == "claims" else None)
        self.counts = {tid: len(rows) for tid, rows in tables.items()}
        for tid, title in boardview.PANELS:
            self.query_one(f"#{tid}").border_title = f"{title} ({self.counts[tid]})"
        self.layout_panels(snap)
        log = self.query_one("#feed", RichLog)
        for line in new_feed:
            log.write(linkify(line))
        for kind, text in notes:
            log.write(linkify(boardview.announcement(kind, text)))
        generation, entries = logged
        if generation == self.daemon_log.generation:     # else the panel was reloaded meanwhile
            self.write_daemon_log(entries)
        self.maybe_prompt_worker()

    def layout_panels(self, snap) -> None:
        """Idle block when every panel is empty; else make sure the expanded panel is one with rows."""
        idle = not any(self.counts.values())
        for pid, _ in boardview.PANELS:
            self.query_one(f"#{pid}").set_class(idle or pid != self.panel, "collapsed")
        self.query_one("#idle", Static).set_class(not idle, "hidden")
        self.query_one("#strip", Static).set_class(idle, "hidden")
        if idle:
            self.query_one("#idle", Static).update(boardview.idle_text(snap, self.ctx.identity))
            return
        if not self.counts.get(self.panel):
            nxt = next((t for t, _ in boardview.PANELS if self.counts.get(t)), self.panel)
            self.show_panel(nxt)
        else:
            self.render_strip()

    # -- the daemon's log (GH-10) --------------------------------------------------------
    def write_daemon_log(self, entries) -> None:
        panel = self.query_one("#daemon-log", RichLog)
        for e in entries:
            panel.write(Text(e.line, style=LOG_STYLES.get(e.level, "")))

    def load_daemon_log(self) -> None:
        """(Re)fill the panel from the file at the current level."""
        tail = self.daemon_log
        path = f".swarm/{daemon.LOGFILE}"
        self.query_one("#daemon-log-title", Static).update(f"Daemon log ({path}, this machine) · ≥{tail.level}")
        panel = self.query_one("#daemon-log", RichLog)
        panel.clear()
        entries = tail.backlog()
        if not entries:
            where = "yet" if tail.path.exists() else f"— {path} doesn't exist here"
            panel.write(f"no daemon log entries at ≥{tail.level} on {self.ctx.identity} {where}")
        self.write_daemon_log(entries)

    def action_log_level(self) -> None:
        if self.busy():
            return
        self.daemon_log.level = boardview.next_level(self.daemon_log.level)
        self.load_daemon_log()

    # -- selection -----------------------------------------------------------------------
    @staticmethod
    def selected_key(table: DataTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    def selected_task(self, prefer: tuple[str, ...] = ("claims", "review", "arbitration", "plans", "tests")) -> str | None:
        focused = self.focused
        if isinstance(focused, DataTable):
            key = self.selected_key(focused)
            if key:
                return key
        for tid in prefer:
            key = self.selected_key(self.query_one(f"#{tid}", DataTable))
            if key:
                return key
        self.notify("select a task first", severity="warning")
        return None

    def task_dir(self, key: str):
        return self.ctx.task_dir_for(key)

    def busy(self) -> bool:
        return isinstance(self.screen, ModalScreen)

    # -- running actions off the UI thread -----------------------------------------------------
    @work(thread=True, group="actions", exit_on_error=False)
    def run_job(self, label: str, fn, *args, **kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, escape(f"{label} failed: {e}"), severity="error", timeout=8)
            return
        message = result if isinstance(result, str) and result else label
        self.call_from_thread(self.notify, escape(message))
        self.call_from_thread(self.refresh_data)

    def say(self, text: str) -> None:
        self.said.append(text)
        self.query_one("#feed", RichLog).write(linkify(f"[swarm-board] {text}"))

    # -- machine commands (Ch.10.2) -------------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_pause(self) -> None:
        if self.busy():
            return
        self.run_job("paused", actions.pause, self.ctx)

    def action_resume(self) -> None:
        if self.busy():
            return
        self.run_job("resumed", actions.resume, self.ctx)

    def action_throttle(self) -> None:
        if self.busy():
            return
        def done(value):
            n = as_int(value)
            if value is not None and n is None:
                self.notify(f"not a number: {value}", severity="error")
            elif n == 0:
                def sure(ok):
                    if ok:
                        self.run_job("share 0 — out of rotation", actions.throttle, self.ctx, 0)
                self.push_screen(ConfirmScreen(
                    f"Share 0 takes {self.ctx.identity} out of rotation: it claims nothing new and "
                    "releases the claims it holds (ones with no worker at once; running ones "
                    "checkpoint and go a lease later). It stays that way after a restart. "
                    "Set a share with t to undo. To keep your claims, pause (p) instead."), sure)
            elif n is not None:
                self.run_job(f"quota share set to {n}", actions.throttle, self.ctx, n)
        current = "" if not self.snap or self.snap.share is None else str(self.snap.share)
        self.push_screen(InputScreen("Quota share for this machine:", "e.g. 2", current, numeric=True), done)

    def action_stop(self) -> None:
        if self.busy():
            return
        def done(ok):
            if ok:
                self.run_job("stop requested", self._stop)
        self.push_screen(ConfirmScreen(f"Stop the swarm on {self.ctx.identity}? Leases lapse normally."), done)

    def _stop(self) -> str:
        from dags import ledger
        ledger.control(self.ctx, "stop")
        if daemon.running_pid(self.ctx):
            daemon.terminate(self.ctx, wait_s=0)
        return "stop requested — loops finish their current cycle"

    def action_set_quota(self) -> None:
        if self.busy():
            return
        def done(value):
            n = as_int(value)
            if value is not None and n is None:
                self.notify(f"not a number: {value}", severity="error")
            elif n is not None:
                self.run_job(f"global quota set to {n}", actions.set_quota, self.ctx, n)
        current = "" if not self.snap else str(self.snap.quota_n)
        self.push_screen(InputScreen("Global quota N (humans.yaml only):", "e.g. 3", current, numeric=True), done)

    # -- task commands ----------------------------------------------------------------------------
    def action_freeze(self) -> None:
        if self.busy():
            return
        key = self.selected_task()
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        if view and view.res.arbitration is not None:
            self.run_job(f"{view.short} unfrozen", actions.unfreeze, self.ctx, view.dir, "lifted on the Board")
            return

        def done(reason):
            if reason:
                self.run_job(f"{view.short} frozen", actions.freeze, self.ctx, view.dir, reason)
        self.push_screen(InputScreen(f"Freeze {view.short} — reason (required):"), done)

    def action_reassign(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("arbitration", "claims"))
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        claimants = [(c.id, f"{c.machine} ({c.human or '?'}, clock {c.clock})") for c in view.res.valid] or \
                    [(c.id, f"{c.machine} (clock {c.clock}, expired)") for c in view.res.claims]
        if not claimants:
            self.notify(f"{view.short} has no claimants", severity="warning")
            return

        def picked(winner):
            if not winner:
                return

            def reason_given(reason):
                if reason:
                    self.run_job(f"{view.short} awarded to {winner}", actions.reassign,
                                    self.ctx, view.dir, winner, reason)
            self.push_screen(InputScreen(f"Why does {winner} win {view.short}?"), reason_given)
        self.push_screen(ChoiceScreen(f"Who should own {view.short}?", claimants), picked)

    def action_takeover(self) -> None:
        if self.busy():
            return
        key = self.selected_task()
        if not key or not self.snap:
            return
        epic = boardview.epic_of(self.snap, key)
        if not epic:
            self.notify("that task has no epic", severity="warning")
            return
        if boardview.holds_takeover(self.snap, epic):
            self.run_job(f"released {epic}", actions.release_epic, self.ctx, epic)
        else:
            self.run_job(f"took over {epic}", actions.takeover, self.ctx, epic)

    def action_merge(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("review",))
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        if view.state != "awaiting-review":
            self.notify(f"{view.short} has no PR awaiting review", severity="warning")
            return

        text, over_red = boardview.merge_prompt(
            view.pr_url, view.short, (self.pr_status.get(view.key) or {}).get("checks"))

        def done(ok):
            if ok:
                self.run_job(f"merged {view.pr_url}", actions.merge, self.ctx, view.dir,
                             allow_failing_checks=over_red)
        self.push_screen(ConfirmScreen(text), done)

    # -- links (GH-5) ------------------------------------------------------------------------------
    def action_open_link(self, url: str) -> None:
        self.open_link(url)

    def open_link(self, url: str) -> None:
        """Open url on the UI thread: through Textual, a new tab in the viewer's browser
        under `board --web` and the default browser in a terminal."""
        try:
            (self._open_link or self.open_url)(url)
        except Exception as e:  # noqa: BLE001
            self.notify(escape(f"could not open {url}: {e}"), severity="error", timeout=8)
            return
        self.notify(escape(f"opened {url}"))

    def open_task_link(self, key: str, kind: str) -> None:
        if kind == "pr":
            url = actions.pr_url(self.task_dir(key))
            missing = "no PR yet"
        else:
            url = actions.ticket_url(self.ctx, self.task_dir(key))
            missing = "no ticket link"
        if url:
            self.open_link(url)
        else:
            self.notify(missing, severity="warning")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row, or a click on the highlighted row: open the clicked cell's link."""
        table = event.data_table
        if self.busy() or event.row_key.value is None:
            return
        column = getattr(table, "clicked_column", None)
        self.open_task_link(event.row_key.value, boardview.link_kind(table.id or "", column))

    def action_open_ticket(self) -> None:
        if self.busy():
            return
        key = self.selected_task()
        if key:
            self.open_task_link(key, "ticket")

    def action_open_pr(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("review",))
        if key:
            self.open_task_link(key, "pr")

    def action_review_plan(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("plans", "claims"))
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        plan_path = self.ctx.worktrees_dir / worktree.task_slug(view.dir) / ".swarm-task" / "plan.md"
        draft_md = plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else None
        submit_mode = view.plan_status in ("not submitted", "changed since submitted")
        plan_md = draft_md if submit_mode else view.checkpoint.get("plan_md")
        if not plan_md:
            self.notify(f"{view.short} has no plan yet", severity="warning")
            return

        def done(decision):
            if not decision:
                return
            if decision == "submit-changes-requested":
                def note_done(note):
                    if note is None:
                        return
                    def submit_then_request():
                        worklib.submit_plan(self.ctx, view.dir, plan_md)
                        worklib.approve_plan(self.ctx, view.dir, "changes-requested", note)
                        return f"plan for {view.short}: submitted and changes requested"
                    self.run_job(f"plan for {view.short}: submitted and changes requested",
                                submit_then_request)
                self.push_screen(InputScreen(f"Why should {view.short} change the plan?"), note_done)
                return
            if decision == "submit-approved":
                def submit_then_approve():
                    worklib.submit_plan(self.ctx, view.dir, plan_md)
                    worklib.approve_plan(self.ctx, view.dir, "approved")
                    return f"plan for {view.short}: submitted and approved"
                self.run_job(f"plan for {view.short}: submitted and approved", submit_then_approve)
                return
            if decision == "submit-only":
                def submit_only():
                    worklib.submit_plan(self.ctx, view.dir, plan_md)
                    return f"plan for {view.short}: submitted"
                self.run_job(f"plan for {view.short}: submitted", submit_only)
                return
            self.run_job(f"plan for {view.short}: {decision}", worklib.approve_plan,
                         self.ctx, view.dir, decision)
        links = [("Ticket", actions.ticket_url(self.ctx, view.dir)), ("PR", view.pr_url)]
        self.push_screen(PlanScreen(view.short, plan_md, [(n, u) for n, u in links if u],
                                    submit_mode=submit_mode), done)

    def action_answer_tests(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("tests", "claims"))
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        if not view.test_scope or view.test_scope["status"] != "pending":
            self.notify(f"{view.short} has no open test question", severity="warning")
            return
        proposal = view.test_scope["proposal"]

        def done(answer):
            if answer:
                scope, enough = answer
                self.run_job(f"tests for {view.short}: {scope}", worklib.answer_tests,
                             self.ctx, view.dir, scope, enough)
        self.push_screen(ScopeQuestionScreen(view.short, boardview.test_question_text(proposal),
                                         proposal.get("recommendation", "full")), done)

    def action_answer_question(self) -> None:
        """Answer a worker's `block` question (GH-2)."""
        if self.busy():
            return
        key = self.selected_task(("tests", "claims"))
        view = self.snap.by_key(key) if key and self.snap else None
        if view is None:
            return
        if not view.needs_human:
            self.notify(f"{view.short} has no open question", severity="warning")
            return

        def done(value):
            if value:
                self.run_job(f"answered {view.short}", worklib.answer_question, self.ctx, view.dir, value)
        self.push_screen(InputScreen(f"{view.short} asks: {view.needs_human}", "your answer"), done)

    # -- worker choice (Ch.7.3, Ch.10.7) ------------------------------------------------------------
    def maybe_prompt_worker(self) -> None:
        if self.prompt_open or not self.snap or self.busy():
            return
        for view in self.snap.awaiting_worker():
            if view.winner.id not in self.prompted:
                self.prompted.add(view.winner.id)
                self.prompt_worker(view)
                return

    def action_choose_worker(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("claims",))
        if not key or not self.snap:
            return
        view = self.snap.by_key(key)
        if view is None:
            self.notify(f"{key} is gone — refreshing", severity="warning")
            self.refresh_data()
            return
        if view not in self.snap.awaiting_worker():
            self.notify(f"{view.short} isn't waiting for a worker on this machine", severity="warning")
            return
        self.prompt_worker(view)

    def prompt_worker(self, view) -> None:
        allowed = workers.allowed_for(view.autonomy)
        pairs = [(letter, name) for letter, name in workers.LETTERS.items() if name in allowed]
        options = [(name, workers.WORKERS[name].label) for _, name in pairs]
        letters = [letter for letter, _ in pairs]
        for letter, oid, label in zip("defg", ("not-now", "not-now-pause"),
                                      ("Not now", "Not now, and pause this machine"), strict=False):
            letters.append(letter)
            options.append((oid, label))
        question = f"I have claimed {view.short} for completion, who is my worker?"
        self.say(question)
        self.prompt_open = True

        def done(choice):
            self.prompt_open = False
            if choice in (None, "not-now", "not-now-pause"):
                if choice == "not-now-pause":
                    self.run_job("paused", actions.pause, self.ctx)
                    self.say(f"Not now — this machine is paused (r resumes). {view.short} stays claimed "
                             f"and unassigned; press w when you want to choose.")
                    return
                self.say(f"No worker chosen for {view.short} yet — press w to choose later.")
                return
            label = workers.WORKERS[choice].label
            self.say(f"Ok, you have selected {label}. Handing over {view.short} to it — "
                     f"when done, it will announce with the PR link here.")
            self.run_job(f"{view.short} handed to {label}", worklib.choose_worker, self.ctx, view.dir,
                            choice, launch=self.launch, platform=self.platform)
            self.call_later(self.maybe_prompt_worker)
        self.push_screen(ChoiceScreen(f"[swarm-board] {question}", options, letters), done)


def run(ctx: Context, **kwargs) -> None:
    BoardApp(ctx, **kwargs).run()


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="DAGS Swarm Board")
    p.add_argument("--root")
    p.add_argument("--identity")
    p.add_argument("--refresh", type=float, default=5.0)
    args = p.parse_args(argv)
    run(Context(args.root, identity=args.identity), refresh_s=args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
