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

import os  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from functools import partial  # noqa: E402

from rich.markup import escape  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.color import Color  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.command import DiscoveryHit, Hit, Provider  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.theme import Theme  # noqa: E402
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, Markdown,  # noqa: E402
                             OptionList, ProgressBar, RichLog, Select, SelectionList, Static, TextArea)
from textual.widgets.option_list import Option  # noqa: E402
from textual.widgets.selection_list import Selection  # noqa: E402

import resolve  # noqa: E402
import workers  # noqa: E402
from backends.base import AUTONOMY_TIERS  # noqa: E402
from dags import actions, boardview, daemon, draft, feed, gh, snapshot, worktree  # noqa: E402
from dags import records as R  # noqa: E402
from dags import work as worklib  # noqa: E402
from dags.config import Context  # noqa: E402

# ---------------------------------------------------------------------------
# appearance (GH-72): the DAGS palette is a Textual theme, so board.tcss only uses tokens a
# theme controls ($surface $panel $foreground ...) and any built-in theme re-colours the Board.
# Cell styling resolves boardview's semantic names through BoardApp.cell_styles, which is
# rebuilt from the active theme whenever it changes.
# ---------------------------------------------------------------------------

DAGS_THEME = Theme(
    name="dags", dark=True,
    primary="#4A5A75", secondary="#6B7C99", accent="#B23A2F", error="#B23A2F",
    success="#5FA77A", warning="#D9A441",
    foreground="#EEF2F8", background="#15243C", surface="#15243C", panel="#4A5A75",
)

# theme colour used for each of boardview's semantic style names (bold = emphasised)
STYLE_SOURCES = {
    "plain": None, "dim": "secondary", "off": "secondary",
    "ok": "success", "warn": "warning", "crit": "!error", "accent": "!accent",
}


def build_cell_styles(variables: dict[str, str]) -> dict[str, str]:
    """boardview's semantic style names -> Rich styles, from a theme's CSS variables."""
    out = {}
    for name, source in STYLE_SOURCES.items():
        if source is None:
            out[name] = ""
        else:
            bold = source.startswith("!")
            out[name] = ("bold " if bold else "") + Color.parse(variables[source.lstrip("!")]).hex6
    return out


def build_log_styles(styles: dict[str, str]) -> dict[str, str]:
    return {"WARNING": styles["warn"], "ERROR": styles["crit"], "CRITICAL": styles["crit"],
            "DEBUG": styles["dim"]}

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
                _key("f", "freeze", "Freeze"), _key("u", "unpark", "Unpark"),
                _key("p", "pause", "Pause"), _key("o", "open_ticket", "Ticket")]


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
    ("unpark", "Unpark the selected task", "u"),
    ("reassign", "Reassign the selected task", "a"),
    ("takeover", "Take over or release the epic", "k"),
    ("compose_issue", "File an issue from a form", "i"),
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
            with VerticalScroll(id="prompt-scroll"):
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
claimed and unassigned (press w to choose later).

Filing an issue

  i  file an issue   A form: title, body, labels, autonomy, repo, epic (required),
                     depends on, blocked. The labels it will get show before anything
                     is written. e edits the body in $EDITOR, d loads a draft from
                     drafts/, Save writes drafts/<title>.md, File checks it, shows
                     what will be written and asks. Pasting a markdown draft into the
                     body fills the other fields. It never sets an issue ready.
  k  take over       Reserve the selected task's epic for this machine (k again releases)."""


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
                yield Button("Answer on GitHub", id="answer-on-github")
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


class IssueComposer(ModalScreen[None]):
    """File an issue from a form (GH-83, D34). It builds a ``draft.Draft`` and hands it to the
    same validate / plan_filing / file_draft path as ``swarm.py backend file``; nothing is
    filed here that the CLI could not file, and nothing is ever set ready."""
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("e", "edit_body", "Editor"), Binding("f2", "edit_body", show=False, priority=True),
                Binding("d", "load_draft", "Load draft"),
                Binding("ctrl+s", "save", "Save", priority=True),
                Binding("q", "noop", show=False)]
    FIELDS = ("title", "labels", "autonomy", "repo", "epic", "depends_on", "status")

    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx = ctx
        self.code_repos = sorted((ctx.backend_cfg.get("repos") or {}).keys())
        self.path: Path | None = None               # the draft file this form came from or was saved to
        self.url: str | None = None                 # stamped by an earlier, part-way filing
        self.extra: dict = {"labels": [], "depends_on": [], "autonomy": None, "repo": None,
                            "epic": None, "status": None}   # draft values the widgets can't show
        self.loaded = False
        self.choices: dict[str, set[str]] = {"f-autonomy": set(AUTONOMY_TIERS), "f-repo": set(self.code_repos),
                                              "f-epic": set()}
        self.pending: draft.Draft | None = None
        self.body_length = 0
        self.submitting = False

    # -- layout --------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="composer"):
            yield Label("File an issue", markup=False)
            with VerticalScroll(id="composer-form"):
                yield Label("Title", markup=False)
                yield Input(id="f-title", placeholder="one line")
                yield Static("", id="err-title", classes="problem", markup=False)
                yield Label("Body (e: open $EDITOR; paste a markdown draft to fill the form)", markup=False)
                yield TextArea(id="f-body")
                yield Label("Labels", markup=False)
                yield SelectionList(id="f-labels")
                yield Static("", id="err-labels", classes="problem", markup=False)
                yield Label("Autonomy (blank: the default tier)", markup=False)
                yield Select([(t, t) for t in AUTONOMY_TIERS], id="f-autonomy", prompt="default")
                yield Static("", id="err-autonomy", classes="problem", markup=False)
                yield Label("Repo", markup=False)
                yield Select([(r, r) for r in self.code_repos], id="f-repo", prompt="choose a repo")
                yield Static("", id="err-repo", classes="problem", markup=False)
                yield Label("Epic (required)", markup=False)
                yield Select([], id="f-epic", prompt="loading epics…")
                yield Static("", id="err-epic", classes="problem", markup=False)
                yield Label("Depends on (open tasks)", markup=False)
                yield SelectionList(id="f-depends")
                yield Static("", id="err-depends_on", classes="problem", markup=False)
                yield Checkbox("Blocked (status: blocked)", id="f-blocked")
                yield Static("", id="err-status", classes="problem", markup=False)
            yield Static("", id="preview", markup=False)
            yield Static("", id="composer-status", markup=False)
            with Horizontal(id="buttons"):
                yield Button("File", id="file", variant="primary")
                yield Button("Save draft", id="save")
                yield Button("Load draft", id="load")
                yield Button("Editor", id="edit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-title", Input).focus()
        if len(self.code_repos) == 1:
            self.query_one("#f-repo", Select).value = self.code_repos[0]
        self.load_backend()
        self.refresh_preview()

    def say(self, text: str, error: bool = False) -> None:
        status = self.query_one("#composer-status", Static)
        status.update(text)
        status.set_class(error, "problem")

    # -- backend reads, off the UI thread ------------------------------------------------
    @work(thread=True, group="composer-load", exit_on_error=False)
    def load_backend(self) -> None:
        try:
            backend = self.ctx.backend
            have = sorted(backend.existing_labels())
            issues = backend.all_issues()
            labels = [lb for lb in have if not lb.startswith(draft.OWNED_PREFIXES)]
            epics = [(f"{backend.short_key(t.ref)}  {t.title}", t.ref.key) for t in issues
                     if t.is_epic and not t.closed]
            tasks = [(f"{backend.short_key(t.ref)}  {t.title}", t.ref.key) for t in issues
                     if not t.is_epic and not t.done]
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.loaded_failed, str(e))
            return
        self.app.call_from_thread(self.loaded_ok, labels, epics, tasks)

    def loaded_failed(self, message: str) -> None:
        self.say(f"couldn't read the tracker: {message}", error=True)
        self.query_one("#f-epic", Select).prompt = "tracker unavailable"

    def loaded_ok(self, labels, epics, tasks) -> None:
        self.query_one("#f-labels", SelectionList).add_options([Selection(lb, lb) for lb in labels])
        self.choices["f-epic"] = {key for _, key in epics}
        self.query_one("#f-epic", Select).set_options(epics)
        self.query_one("#f-epic", Select).prompt = "choose an epic" if epics else "no epics on the tracker"
        self.query_one("#f-depends", SelectionList).add_options([Selection(text, key) for text, key in tasks])
        self.loaded = True
        if self.pending is not None:
            pending, self.pending = self.pending, None
            self.fill(pending)
        self.refresh_preview()

    # -- form <-> draft ------------------------------------------------------------------
    @staticmethod
    def chosen(select: Select) -> str | None:
        value = select.value
        return value if isinstance(value, str) and value else None

    def current_draft(self) -> draft.Draft:
        extra = self.extra
        labels = list(self.query_one("#f-labels", SelectionList).selected)
        deps = list(self.query_one("#f-depends", SelectionList).selected)
        status = "blocked" if self.query_one("#f-blocked", Checkbox).value else extra["status"]
        return draft.Draft(
            title=self.query_one("#f-title", Input).value.strip(),
            body=self.query_one("#f-body", TextArea).text,
            labels=labels + [x for x in extra["labels"] if x not in labels],
            autonomy=self.chosen(self.query_one("#f-autonomy", Select)) or extra["autonomy"],
            repo=self.chosen(self.query_one("#f-repo", Select)) or extra["repo"],
            epic=self.chosen(self.query_one("#f-epic", Select)) or extra["epic"],
            depends_on=deps + [x for x in extra["depends_on"] if x not in deps],
            status=status, url=self.url)

    def set_select(self, widget_id: str, key: str, value: str | None) -> None:
        select = self.query_one(widget_id, Select)
        self.extra[key] = None
        if value in self.choices[widget_id.lstrip("#")]:
            select.value = value
        else:
            select.clear()
            self.extra[key] = value

    def set_list(self, widget_id: str, key: str, values: list[str]) -> None:
        sl = self.query_one(widget_id, SelectionList)
        known = {sl.get_option_at_index(i).value for i in range(sl.option_count)}
        sl.deselect_all()
        for v in values:
            if v in known:
                sl.select(v)
        self.extra[key] = [v for v in values if v not in known]

    def fill(self, d: draft.Draft) -> None:
        """Put a parsed draft into the form, leaving only the real body in the TextArea."""
        if not self.loaded:                         # lists not read yet: apply when they arrive
            self.pending = d
            self.query_one("#f-title", Input).value = d.title
            self.set_body(d.body)
            return
        self.url = d.url
        self.query_one("#f-title", Input).value = d.title
        self.set_body(d.body)
        self.set_list("#f-labels", "labels", d.labels)
        self.set_list("#f-depends", "depends_on", [self.resolve_ref(x) for x in d.depends_on])
        self.set_select("#f-autonomy", "autonomy", d.autonomy)
        self.set_select("#f-repo", "repo", d.repo)
        self.set_select("#f-epic", "epic", self.resolve_ref(d.epic))
        self.query_one("#f-blocked", Checkbox).value = d.status == "blocked"
        self.extra["status"] = d.status if d.status != "blocked" else None
        self.refresh_preview()

    def resolve_ref(self, text: str | None) -> str | None:
        """A draft says ``epic: 7``; the widgets hold ``OWNER/REPO#7``. Text that isn't a reference
        is passed on as it is, so ``validate`` can name it."""
        if not text:
            return None
        try:
            return self.ctx.backend.parse_ref(text).key
        except ValueError:
            return text

    def set_body(self, text: str) -> None:
        area = self.query_one("#f-body", TextArea)
        self.body_length = len(text)
        area.load_text(text)

    # -- live preview and paste ----------------------------------------------------------
    def refresh_preview(self) -> None:
        d = self.current_draft()
        try:
            labels = draft.resolved_labels(self.ctx.backend, d)
            text = "Labels on the issue: " + (", ".join(labels) if labels else "(none)")
        except Exception as e:  # noqa: BLE001
            text = f"Labels on the issue: can't work out ({e})"
        self.query_one("#preview", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        key = {"f-autonomy": "autonomy", "f-repo": "repo", "f-epic": "epic"}.get(event.select.id or "")
        if key and self.chosen(event.select):
            self.extra[key] = None
        self.refresh_preview()

    def on_selection_list_selected_changed(self, event) -> None:
        self.refresh_preview()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.refresh_preview()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        grew = len(text) - self.body_length
        self.body_length = len(text)
        if grew > 1 and draft.looks_like_draft(text):       # a paste, not typing a heading
            self.fill(draft.parse(text))
        else:
            self.refresh_preview()

    # -- the editor, saved drafts --------------------------------------------------------
    def action_noop(self) -> None:
        pass

    def action_edit_body(self) -> None:
        editor = shlex.split(os.environ.get("EDITOR") or "vi")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(self.query_one("#f-body", TextArea).text)
            name = f.name
        try:
            with self.app.suspend():
                subprocess.run([*editor, name], check=False)
            text = Path(name).read_text(encoding="utf-8")
        except (OSError, subprocess.SubprocessError) as e:
            self.say(f"the editor failed: {e}", error=True)
            return
        finally:
            Path(name).unlink(missing_ok=True)
        if draft.looks_like_draft(text):
            self.fill(draft.parse(text))
        else:
            self.set_body(text)
            self.refresh_preview()

    @property
    def drafts_dir(self) -> Path:
        return self.ctx.root / draft.DRAFTS_DIR

    def action_load_draft(self) -> None:
        files = sorted(p for p in self.drafts_dir.glob("*.md") if p.name.lower() != "readme.md")
        if not files:
            self.say(f"no drafts in {draft.DRAFTS_DIR}/", error=True)
            return

        def picked(name):
            if not name:
                return
            path = self.drafts_dir / name
            try:
                d = draft.load(path)
            except draft.DraftError as e:
                self.say(str(e), error=True)
                return
            self.path = path
            self.fill(d)
            self.say(f"loaded {draft.DRAFTS_DIR}/{name}")
        self.app.push_screen(ChoiceScreen("Load which draft?", [(p.name, p.name) for p in files]), picked)

    def target_path(self, d: draft.Draft) -> Path:
        if self.path is not None:
            return self.path
        name = R.slug(d.title).lower()[:60] + ".md"
        path = self.drafts_dir / name
        if path.exists() or (self.drafts_dir / draft.FILED_DIR / name).exists():
            raise draft.DraftError(f"{draft.DRAFTS_DIR}/{name} already exists; load it with d or change the title")
        return path

    def save_draft(self) -> Path:
        d = self.current_draft()
        if not d.title:
            raise draft.DraftError("title: missing (the draft's file is named after it)")
        path = self.target_path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(draft.render(d), encoding="utf-8")
        self.path = path
        return path

    def action_save(self) -> None:
        try:
            path = self.save_draft()
        except draft.DraftError as e:
            self.say(str(e), error=True)
            return
        self.say(f"saved {draft.DRAFTS_DIR}/{path.name} (not filed)")

    # -- submit --------------------------------------------------------------------------
    def show_problems(self, problems: list[draft.Problem]) -> None:
        by_field: dict[str, list[str]] = {}
        for p in problems:
            by_field.setdefault(p.field, []).append(p.message)
        for name in self.FIELDS:
            self.query_one(f"#err-{name}", Static).update("\n".join(by_field.get(name, [])))
        self.say(f"{len(problems)} problem(s); nothing was filed" if problems else "", error=bool(problems))

    def action_submit(self) -> None:
        if self.submitting:
            return
        try:
            self.ctx.require_human()
        except Exception as e:  # noqa: BLE001
            self.say(str(e), error=True)
            return
        self.submitting = True
        self.say("checking…")
        self.check(self.current_draft())

    @work(thread=True, group="composer-check", exit_on_error=False)
    def check(self, d: draft.Draft) -> None:
        backend = self.ctx.backend
        try:
            problems = draft.validate(d, backend, self.code_repos)
            filing = None if problems else draft.plan_filing(d, backend)
            lines = filing.lines(backend.short_key) if filing else []
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.check_failed, str(e))
            return
        self.app.call_from_thread(self.checked, d, problems, filing, lines)

    def check_failed(self, message: str) -> None:
        self.submitting = False
        self.say(message, error=True)

    def checked(self, d, problems, filing, lines) -> None:
        self.submitting = False
        self.show_problems(problems)
        if problems:
            return
        text = "File this issue?\n\n" + "\n".join(lines) + "\n\nIt is not set ready."

        def answered(ok):
            if ok:
                self.file(d, filing)
        self.app.push_screen(ConfirmScreen(text), answered)

    def file(self, d: draft.Draft, filing) -> None:
        try:
            path = self.save_draft()                # file_draft stamps and moves this file
        except draft.DraftError as e:
            self.say(str(e), error=True)
            return
        app, ctx = self.app, self.ctx

        def job() -> str:
            try:
                res = draft.file_draft(ctx, path, d, filing)
            except draft.DraftError as e:
                app.call_from_thread(app.say, f"filing {d.title!r} stopped: {e}")
                raise
            if res.synced:
                app.call_from_thread(app.say, f"filed {res.url} — the ledger has it; it is not ready")
            else:
                app.call_from_thread(app.say, f"filed {res.url} — plan sync failed ({res.sync_error}); "
                                              f"run `swarm.py plan sync` or the swarm can't see it")
            return f"filed {res.url}"
        app.run_job(f"filed {d.title}", job)
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"file": self.action_submit, "save": self.action_save, "load": self.action_load_draft,
         "edit": self.action_edit_body, "cancel": self.action_cancel}[str(event.button.id)]()

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# the Board
# ---------------------------------------------------------------------------

class BoardApp(App):
    TITLE = "DAGS Swarm Board"
    CSS_PATH = "board.tcss"
    COMMANDS = App.COMMANDS | {BoardCommands}

    # The footer shows the focused panel's actions (see the *Table classes) plus these five;
    # every other key still works from anywhere and is listed behind ':'.
    BINDINGS = [
        Binding("tab", "cycle_panel(1)", "Panel", priority=True),
        Binding("shift+tab", "cycle_panel(-1)", "Panel", show=False, priority=True),
        Binding("colon", "command_palette", "Commands"),
        Binding("i", "compose_issue", "File issue"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("p", "pause", "Pause", show=False),
        Binding("r", "resume", "Resume", show=False),
        Binding("t", "throttle", "Throttle", show=False),
        Binding("s", "stop", "Stop", show=False),
        Binding("w", "choose_worker", "Worker", show=False),
        Binding("f", "freeze", "Freeze/unfreeze", show=False),
        Binding("u", "unpark", "Unpark", show=False),
        Binding("a", "reassign", "Reassign", show=False),
        Binding("k", "takeover", "Take over epic", show=False),
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
        self.register_theme(DAGS_THEME)
        self.theme = DAGS_THEME.name
        self.cell_styles = build_cell_styles(self.get_css_variables())
        self.log_styles = build_log_styles(self.cell_styles)

    def _on_theme_changed(self, _theme=None) -> None:
        """Cell and log colours are Rich styles, not CSS: rebuild them and repaint."""
        self.cell_styles = build_cell_styles(self.get_css_variables())
        self.log_styles = build_log_styles(self.cell_styles)
        if self.is_running and self.snap is not None:
            self.refresh_data()
            self.load_daemon_log()

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
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
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
        new_feed = [boardview.feed_line(e.text) for e in feed.new_events(ctx.root, self.seen)]
        notes = self.tail.read()
        logged = self.daemon_log.read_tagged()
        flagged = boardview.poller_flags(ctx.swarm_dir)
        pid = daemon.running_pid(ctx)
        info = daemon.info(ctx) if pid else {}
        self.call_from_thread(self.apply, snap, new_feed, notes, flagged, pid, logged, info)

    # which cells carry an accent: only what needs a human (restraint, GH-67)
    ACCENT_COLUMN = {"arbitration": "why", "plans": "plan", "tests": "task"}

    def _cell(self, tid: str, key: str, column: str, text: str, linked: bool, leases: dict,
              kinds: dict | None = None) -> Text:
        cell = Text(text, style="underline" if linked and text else "")
        if column in boardview.SECONDARY_COLUMNS:
            cell.stylize(self.cell_styles["dim"])
        elif column == "lease":
            cell.stylize(self.cell_styles[boardview.level_of(leases.get(key))])
        elif column == self.ACCENT_COLUMN.get(tid):
            cell.stylize(self.cell_styles["accent"])
        elif tid == "claims" and column == "state":
            cell.stylize(self.cell_styles[(kinds or {}).get(key, "plain")])
        elif tid == "claims" and column == "task" and (kinds or {}).get(key) == "accent":
            cell.stylize(self.cell_styles["accent"])      # findable at a glance in a long list (GH-102)
        return cell

    def _fill(self, tid: str, rows: list[tuple[str, tuple]], leases: dict | None = None,
              kinds: dict | None = None) -> None:
        table = self.query_one(f"#{tid}", DataTable)
        selected = self.selected_key(table)
        table.clear()
        names = [str(c.label) for c in table.ordered_columns]
        for key, cells in rows:
            table.add_row(*(self._cell(tid, key, names[i], str(c), names[i] in boardview.LINK_COLUMNS and bool(c),
                                       leases or {}, kinds) for i, c in enumerate(cells)), key=key)
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
            strip.append(f"{title} {n}", style=self.cell_styles[style])
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
            header.append(("\n" if i else "") + label.ljust(9), style=self.cell_styles["dim"])
            header.append(value, style=self.cell_styles[style])
        self.query_one("#machine", Static).update(header)
        q = boardview.quota(snap)
        self.query_one("#quota-label", Label).update(q.text)
        self.query_one("#quota", ProgressBar).update(total=max(q.total, 1), progress=min(q.used, max(q.total, 1)))
        tables = {"claims": boardview.claim_rows(snap), "review": boardview.review_rows(snap, self.pr_status),
                  "arbitration": boardview.arbitration_rows(snap, flagged), "plans": boardview.plan_rows(snap),
                  "tests": boardview.test_rows(snap)}
        for tid, rows in tables.items():
            claims = tid == "claims"
            self._fill(tid, rows, boardview.claim_leases(snap) if claims else None,
                       boardview.claim_kinds(snap) if claims else None)
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
            panel.write(Text(e.line, style=self.log_styles.get(e.level, "")))

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

    def hand_over(self, view, choice: str) -> str:
        handed = worklib.choose_worker(self.ctx, view.dir, choice, launch=self.launch, platform=self.platform)
        return handed.message(view.short)

    def say(self, text: str) -> None:
        self.said.append(text)
        self.query_one("#feed", RichLog).write(linkify(f"[swarm-board] {text}"))

    # -- machine commands (Ch.10.2) -------------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_compose_issue(self) -> None:
        if self.busy():
            return
        self.push_screen(IssueComposer(self.ctx))

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

    def action_unpark(self) -> None:
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
        self.run_job(f"{view.short} unparked", actions.unpark, self.ctx, view.dir, "unparked on the Board")

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

    def has_open_question(self, key: str) -> bool:
        return bool(resolve.open_question(self.task_dir(key)))

    def open_task_link(self, key: str, kind: str) -> None:
        if kind == "pr":
            url = actions.pr_url(self.task_dir(key))
            missing = "no PR yet"
        else:
            # a blocked task's link goes to the comment with its question (GH-33)
            url = actions.answer_url(self.ctx, self.task_dir(key)) if self.has_open_question(key) \
                else actions.ticket_url(self.ctx, self.task_dir(key))
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
            if decision == "answer-on-github":
                url = actions.answer_url(self.ctx, view.dir)
                if url:
                    self.open_link(url)
                else:
                    self.notify(f"{view.short} has no issue link", severity="warning")
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
                self.push_screen(InputScreen(f"Why should {view.short} change the plan? (the discussion belongs on the issue: use Answer on GitHub)"), note_done)
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
            self.say(f"Launching {view.short}…")
            self.say(f"Ok, you have selected {label}. Handing over {view.short} to it — "
                     f"when done, it will announce with the PR link here.")
            self.run_job(f"{view.short} handed to {label}", self.hand_over, view, choice)
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
