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

from rich.markup import escape  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, Markdown,  # noqa: E402
                             OptionList, ProgressBar, RichLog, Static)
from textual.widgets.option_list import Option  # noqa: E402

import workers  # noqa: E402
from dags import actions, boardview, daemon, feed, gh, snapshot  # noqa: E402
from dags import work as worklib  # noqa: E402
from dags.config import Context  # noqa: E402

# ---------------------------------------------------------------------------
# modal screens
# ---------------------------------------------------------------------------

MODAL_CSS = """
ModalScreen { align: center middle; }
#dialog { width: 80; max-width: 95%; height: auto; max-height: 90%; border: thick $accent;
          background: $surface; padding: 1 2; }
#dialog Label { width: 100%; margin-bottom: 1; }
#buttons { height: auto; align-horizontal: right; }
#buttons Button { margin-left: 1; }
#plan { height: 20; border: round $primary; }
"""


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
    BINDINGS = [Binding("escape", "cancel", "Later"),
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

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanScreen(ModalScreen[str | None]):
    """Review gate for human-must-review plans (plan §2.8)."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, task: str, plan_md: str):
        super().__init__()
        # not self.task: Textual's MessagePump already owns that name.
        self.task_key = task
        self.plan_md = plan_md

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Plan for {self.task_key}", markup=False)
            with VerticalScroll(id="plan"):
                yield Markdown(self.plan_md or "_empty plan_")
            with Horizontal(id="buttons"):
                yield Button("Approve", id="approved", variant="success")
                yield Button("Request changes", id="changes-requested", variant="warning")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# the Board
# ---------------------------------------------------------------------------

class BoardApp(App):
    TITLE = "DAGS Swarm Board"
    CSS = MODAL_CSS + """
    #machine { height: 1; padding: 0 1; background: $boost; }
    #quota-row { height: 3; padding: 0 1; }
    #quota-label { width: auto; padding-right: 2; content-align: left middle; height: 3; }
    #main { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; }
    .panel-title { height: 1; padding: 0 1; background: $primary-background; text-style: bold; }
    DataTable { height: 1fr; min-height: 4; }
    #feed { height: 1fr; border: round $primary; }
    #daemon-log { height: 1fr; border: round $warning; }
    """
    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("t", "throttle", "Throttle"),
        Binding("s", "stop", "Stop"),
        Binding("w", "choose_worker", "Worker"),
        Binding("f", "freeze", "Freeze/unfreeze"),
        Binding("a", "reassign", "Reassign"),
        Binding("e", "takeover", "Take over epic"),
        Binding("v", "review_plan", "Review plan"),
        Binding("m", "merge", "Approve & merge"),
        Binding("o", "open_ticket", "Ticket"),
        Binding("O,shift+o", "open_pr", "PR", show=False),
        Binding("n", "set_quota", "Global N"),
        Binding("l", "log_level", "Log level"),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
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
        self._open_link = open_url or actions.open_url
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

    # -- layout ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="machine", markup=False)
        with Horizontal(id="quota-row"):
            yield Label("quota", id="quota-label", markup=False)
            yield ProgressBar(total=1, show_eta=False, id="quota")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Live claims", classes="panel-title")
                yield DataTable(id="claims", cursor_type="row", zebra_stripes=True)
                yield Static("Awaiting review", classes="panel-title")
                yield DataTable(id="review", cursor_type="row", zebra_stripes=True)
                yield Static("Needs arbitration", classes="panel-title")
                yield DataTable(id="arbitration", cursor_type="row", zebra_stripes=True)
                yield Static("Plans awaiting review", classes="panel-title")
                yield DataTable(id="plans", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield Static("Activity", classes="panel-title")
                yield RichLog(id="feed", wrap=True, markup=False, highlight=False, max_lines=500)
                yield Static("", id="daemon-log-title", classes="panel-title", markup=False)
                yield RichLog(id="daemon-log", wrap=True, markup=False, highlight=False, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.ctx.identity} · operator {self.ctx.operator}"
        for tid, cols in (("claims", boardview.CLAIM_COLUMNS), ("review", boardview.REVIEW_COLUMNS),
                          ("arbitration", boardview.ARBITRATION_COLUMNS), ("plans", boardview.PLAN_COLUMNS)):
            self.query_one(f"#{tid}", DataTable).add_columns(*cols)
        log = self.query_one("#feed", RichLog)
        for line in boardview.initial_feed(self.ctx.root, self.seen):
            log.write(line)
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
        self.call_from_thread(self.apply, snap, new_feed, notes, flagged, pid, logged)

    def _fill(self, tid: str, rows: list[tuple[str, tuple]]) -> None:
        table = self.query_one(f"#{tid}", DataTable)
        selected = self.selected_key(table)
        table.clear()
        for key, cells in rows:
            table.add_row(*(Text(str(c)) for c in cells), key=key)
        if selected is not None:
            for i, (key, _) in enumerate(rows):
                if key == selected:
                    try:
                        table.move_cursor(row=i)
                    except Exception:  # noqa: BLE001
                        pass
                    break

    def apply(self, snap, new_feed, notes, flagged, pid, logged=(0, ())) -> None:
        self.snap = snap
        self.query_one("#machine", Static).update(boardview.machine_line(snap, pid))
        q = boardview.quota(snap)
        self.query_one("#quota-label", Label).update(q.text)
        self.query_one("#quota", ProgressBar).update(total=max(q.total, 1), progress=min(q.used, max(q.total, 1)))
        self._fill("claims", boardview.claim_rows(snap))
        self._fill("review", boardview.review_rows(snap, self.pr_status))
        self._fill("arbitration", boardview.arbitration_rows(snap, flagged))
        self._fill("plans", boardview.plan_rows(snap))
        log = self.query_one("#feed", RichLog)
        for line in new_feed:
            log.write(line)
        for kind, text in notes:
            log.write(boardview.announcement(kind, text))
        generation, entries = logged
        if generation == self.daemon_log.generation:     # else the panel was reloaded meanwhile
            self.write_daemon_log(entries)
        self.maybe_prompt_worker()

    # -- the daemon's log (GH-10) --------------------------------------------------------
    LOG_STYLES = {"WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold red", "DEBUG": "dim"}

    def write_daemon_log(self, entries) -> None:
        panel = self.query_one("#daemon-log", RichLog)
        for e in entries:
            panel.write(Text(e.line, style=self.LOG_STYLES.get(e.level, "")))

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

    def selected_task(self, prefer: tuple[str, ...] = ("claims", "review", "arbitration", "plans")) -> str | None:
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
        self.query_one("#feed", RichLog).write(f"[swarm-board] {text}")

    # -- machine commands (Ch.10.2) -------------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_data()

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

        def done(ok):
            if ok:
                self.run_job(f"merged {view.pr_url}", actions.merge, self.ctx, view.dir)
        self.push_screen(ConfirmScreen(f"Approve and squash-merge {view.pr_url} ({view.short})?"), done)

    def action_open_ticket(self) -> None:
        if self.busy():
            return
        key = self.selected_task()
        if key:
            url = actions.ticket_url(self.ctx, self.task_dir(key))
            if url:
                self.run_job(f"opened {url}", self._open_link, url)
            else:
                self.notify("no ticket link", severity="warning")

    def action_open_pr(self) -> None:
        if self.busy():
            return
        key = self.selected_task(("review",))
        if key:
            url = actions.pr_url(self.task_dir(key))
            if url:
                self.run_job(f"opened {url}", self._open_link, url)
            else:
                self.notify("no PR yet", severity="warning")

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
        plan_md = view.checkpoint.get("plan_md")
        if not plan_md:
            self.notify(f"{view.short} has no plan yet", severity="warning")
            return

        def done(decision):
            if decision:
                self.run_job(f"plan for {view.short}: {decision}", worklib.approve_plan,
                                self.ctx, view.dir, decision)
        self.push_screen(PlanScreen(view.short, plan_md), done)

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
        question = f"I have claimed {view.short} for completion, who is my worker?"
        self.say(question)
        self.prompt_open = True

        def done(choice):
            self.prompt_open = False
            if not choice:
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
