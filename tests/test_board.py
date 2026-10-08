"""Swarm Board pilot tests (Ch.10). Need textual — run on the Mac via bin/dev-setup.sh."""
import asyncio
from pathlib import Path
import time

import pytest

pytest.importorskip("textual")

import resolve as rv  # noqa: E402
from dags import timeutil  # noqa: E402
from dags import records as R  # noqa: E402
from dags import work  # noqa: E402

board = pytest.importorskip("board")


async def until(pilot, cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if cond():
            return True
    raise AssertionError("condition not met in time")


def shown(widget) -> str:
    """Text of a Static/Label across Textual versions."""
    for attr in ("content", "renderable"):
        value = getattr(widget, attr, None)
        if value is not None:
            return str(value)
    return str(widget.render())


def log_text(widget) -> str:
    """A RichLog's text with its wrapping undone."""
    return " ".join(line.text.strip() for line in widget.lines)


def records(root, sub):
    return [d for _, d in R.read_dir(root / sub)]


def run(coro):
    asyncio.run(coro)


@pytest.fixture
def setup(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    world.backend.add("T2", title="Parse", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    a = world.machine("mac-a")
    world.scheduler(a, share=1).cycle()           # T1 claimed, awaiting a worker
    opened = []
    app = board.BoardApp(a, refresh_s=0.2, launch=world.launch, platform="darwin",
                         use_gh=False, run_poller=False, open_url=opened.append)
    return world, a, app, opened


def test_worker_prompt_and_choice(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("c")                         # VSCode + Human
            await until(pilot, lambda: world.launched)
            await until(pilot, lambda: rv.task_state(rv.index(a.root)["T1"], timeutil.now(), 900)
                        == "in-progress")
            said = "\n".join(app.said)
            assert "I have claimed T1 for completion, who is my worker?" in said
            assert "Ok, you have selected VSCode + Human. Handing over T1" in said
    run(go())
    assert "Visual Studio Code.app" in world.launched[0] or world.launched[0][1] == "-n"


def test_panels_render(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            claims = app.query_one("#claims")
            await until(pilot, lambda: claims.row_count == 1)
            assert "global 1/3" in shown(app.query_one("#quota-label"))
            assert "mac-a" in shown(app.query_one("#machine"))
    run(go())


def test_pause_resume_and_quota(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            await pilot.press("p")
            await until(pilot, lambda: any(d["action"] == "pause" for d in records(a.root, "control")))
            await pilot.press("r")
            await until(pilot, lambda: any(d["action"] == "resume" for d in records(a.root, "control")))
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            inp = app.screen.query_one("#answer")
            inp.value = ""
            await pilot.press("5", "enter")
            await until(pilot, lambda: rv.global_quota(a.root, 3) == 5)
            await pilot.press("t")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            app.screen.query_one("#answer").value = ""
            await pilot.press("2", "enter")
            await until(pilot, lambda: any(d.get("quota_share") == 2 for d in records(a.root, "control")))
    run(go())


def test_freeze_unfreeze_and_takeover(setup):
    world, a, app, _ = setup
    d = rv.index(a.root)["T1"]

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            await until(pilot, lambda: app.query_one("#claims").row_count == 1)
            app.show_panel("claims")
            await pilot.press("f")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            await pilot.press(*"hold", "enter")
            await until(pilot, lambda: rv.active_arbitration(d) is not None)
            assert rv.active_arbitration(d)["reason"] == "hold"
            # frozen tasks leave the live-claims panel; unfreeze by key from the snapshot
            await until(pilot, lambda: app.snap and app.snap.by_key("T1").state == "frozen")
            app.selected_task = lambda *a_, **k: "T1"
            await pilot.press("f")
            await until(pilot, lambda: rv.active_arbitration(d) is None)
            await pilot.press("k")
            await until(pilot, lambda: rv.active_takeovers(a.root).get("E1") == {"mac-a"})
    run(go())


def test_review_plan_and_merge(setup, monkeypatch):
    world, a, app, opened = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    work.submit_plan(a, d, "# The plan\nDo it.")
    # a human on this machine reviews it on the Board
    pr = world.prs.add("OWNER/app", "swarm/T1")
    merged = []

    def fake_merge(repo, number, body=""):
        merged.append((repo, number))
        world.prs.get(pr["url"])["state"] = "MERGED"
    monkeypatch.setattr("dags.gh.approve_and_merge", fake_merge)

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            plans = app.query_one("#plans")
            await until(pilot, lambda: plans.row_count == 1)
            app.show_panel("plans")
            await pilot.press("v")
            await until(pilot, lambda: isinstance(app.screen, board.PlanScreen))
            await pilot.click("#approved")
            await until(pilot, lambda: rv.plan_status(d, a.human_names) == "approved")
            cid = rv.resolve(d, timeutil.now(), 900).winner.id
            from dags import ledger as L
            L.complete(a, d, "pr-opened", claim_id=cid, pr_url=pr["url"], worker="claude")
            review = app.query_one("#review")
            await until(pilot, lambda: review.row_count == 1)
            app.show_panel("review")
            await pilot.press("O")
            await until(pilot, lambda: opened == [pr["url"]])
            await pilot.press("m")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: rv.is_done(d))
    run(go())
    assert merged == [("OWNER/app", str(pr["number"]))]


def test_submit_unsubmitted_plan_from_board(setup):
    world, a, app, _ = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    wt = __import__("dags.worktree", fromlist=["worktree_path"]).worktree_path(a, d)
    (wt / ".swarm-task").mkdir(parents=True, exist_ok=True)
    (wt / ".swarm-task" / "plan.md").write_text("# Draft\nDo it.\n")

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            app.selected_task = lambda *args, **kwargs: "T1"
            await until(pilot, lambda: app.snap and app.snap.by_key("T1").plan_status == "not submitted")
            await pilot.press("v")
            await until(pilot, lambda: isinstance(app.screen, board.PlanScreen))
            await pilot.click("#submit-approved")
            await until(pilot, lambda: rv.plan_status(d, a.human_names) == "approved")
            from dags import ledger as L
            cp = L.read_checkpoint(d)
            assert cp["plan_md"].startswith("# Draft")
            assert cp["plan_sha"]
            assert any(r.get("decision") == "approved" for _, r in __import__("dags.records", fromlist=["read_dir"]).read_dir(d / "plan-reviews"))
    run(go())


def test_non_humans_get_an_error_not_a_crash(setup):
    world, a, app, _ = setup
    (a.swarm_dir / "local.yaml").write_text((a.swarm_dir / "local.yaml").read_text()
                                            .replace("human: roman", "human: mallory"))
    notes = []

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            app.notify = lambda msg, **kw: notes.append((msg, kw.get("severity")))
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            app.screen.query_one("#answer").value = ""
            await pilot.press("7", "enter")
            await until(pilot, lambda: any(sev == "error" for _, sev in notes))
    run(go())
    assert rv.global_quota(a.root, 3) == 3


def test_daemon_log_panel(setup):
    """GH-10: this machine's .swarm/swarm.log is on the Board, with a level filter."""
    world, a, app, _ = setup
    log = a.swarm_dir / "swarm.log"
    log.write_text("2026-09-18 07:35:00,000 INFO dags.daemon: daemon up\n"
                   "2026-09-18 07:35:32,000 WARNING dags.scheduler: T9: dispatch failed: before the Board\n")

    def text():
        return log_text(app.query_one("#daemon-log"))

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            await until(pilot, lambda: "before the Board" in text())
            assert "daemon up" not in text()
            assert "≥WARNING" in shown(app.query_one("#daemon-log-title"))
            with open(log, "a") as f:
                f.write("2026-09-18 07:36:00,000 ERROR dags.scheduler: T1: dispatch failed: new one\n")
            await until(pilot, lambda: "new one" in text())
            await pilot.press("l")                                   # WARNING -> INFO
            await until(pilot, lambda: "daemon up" in text())
            assert "≥INFO" in shown(app.query_one("#daemon-log-title"))
            await pilot.press("l")                                   # INFO -> ERROR
            await until(pilot, lambda: "before the Board" not in text())
            assert "new one" in text()
    run(go())


def test_daemon_log_panel_says_when_there_is_no_log(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            panel = app.query_one("#daemon-log")
            await until(pilot, lambda: "doesn't exist here" in log_text(panel))
    run(go())


def test_feed_links_open_on_click(setup):
    world, a, app, opened = setup
    url = "https://github.com/OWNER/app/pull/9"

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            log = app.query_one("#feed")
            log.clear()
            app.say(f"T1 is done. The PR can be found at {url}.")
            await until(pilot, lambda: any(url in line.text for line in log.lines))
            y, line = next((i, line.text) for i, line in enumerate(log.lines) if url in line.text)
            assert url + "." in line                                    # wrapped whole, not split
            await pilot.click("#feed", offset=(1 + line.index(url) + 3, 1 + y))   # inside the border
            await until(pilot, lambda: opened == [url])
    run(go())


def test_table_cells_open_their_links(setup):
    from dags import actions
    from dags import ledger as L
    world, a, app, opened = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    pr = world.prs.add("OWNER/app", "swarm/T1")
    L.complete(a, d, "pr-opened", claim_id=rv.resolve(d, timeutil.now(), 900).winner.id,
               pr_url=pr["url"], worker="claude")
    world.scheduler(a, share=2).cycle()           # T2 claimed, so the claims panel has a row

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            panel = app.query_one("#daemon-log")
            await until(pilot, lambda: "doesn't exist here" in log_text(panel))
            review = app.query_one("#review")
            await until(pilot, lambda: review.row_count == 1)
            app.show_panel("review")
            await pilot.pause()
            x = sum(c.get_render_width(review) for c in review.ordered_columns[:2]) + 2
            assert review.cursor_row == 0                  # the only row is highlighted, so
            await pilot.click("#review", offset=(x + 1, 2))    # a click on its PR cell opens the PR
            await until(pilot, lambda: opened == [pr["url"]])
            await pilot.click("#review", offset=(3, 2))    # and its task cell opens the ticket
            await until(pilot, lambda: len(opened) == 2)
            claims = app.query_one("#claims")
            await until(pilot, lambda: claims.row_count >= 1)
            app.show_panel("claims")
            await pilot.press("enter")                      # Enter on a claims row: its ticket
            await until(pilot, lambda: len(opened) == 3)
            key = app.selected_key(claims)
            assert opened[2] == actions.ticket_url(a, a.task_dir_for(key))
    run(go())
    assert opened[1] == actions.ticket_url(a, d)


def test_not_now_leaves_the_task_unassigned(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            assert "Not now" in [str(o.prompt) for o in app.screen.query_one("#choices").options][3]
            await pilot.press("d")
            await until(pilot, lambda: not isinstance(app.screen, board.ChoiceScreen))
            assert any("press w when you want to choose" in t or "press w to choose later" in t
                       for t in app.said)
            assert not world.launched
            assert not any(d["action"] == "pause" for d in records(a.root, "control"))
    run(go())


def test_not_now_and_pause_pauses_the_machine(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("e")
            await until(pilot, lambda: any(d["action"] == "pause" for d in records(a.root, "control")))
            assert not world.launched
    run(go())


def test_share_zero_asks_for_confirmation(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            await pilot.press("t")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            app.screen.query_one("#answer").value = ""
            await pilot.press("0", "enter")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            assert "out of rotation" in app.screen.message
            await pilot.press("n")
            await pilot.pause(0.3)
            assert not any(d.get("quota_share") == 0 for d in records(a.root, "control"))
            await pilot.press("t")
            await until(pilot, lambda: isinstance(app.screen, board.InputScreen))
            app.screen.query_one("#answer").value = ""
            await pilot.press("0", "enter")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: any(d.get("quota_share") == 0 for d in records(a.root, "control")))
    run(go())


def test_stylesheet_uses_only_theme_tokens(setup):
    import re
    from pathlib import Path
    world, a, app, _ = setup
    text = re.sub(r"/\*.*?\*/", "", Path(board.__file__).with_name("board.tcss").read_text(), flags=re.S)
    assert not re.search(r":[^;{}]*(#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\()", text)  # no literal colours
    assert not getattr(board.BoardApp, "CSS", "")                              # the class holds no CSS at all
    used = set(re.findall(r"\$([a-z][a-z-]*)", text))
    assert used
    assert not used & {"navy", "slate", "mid", "pale", "line", "rust"}
    assert used <= set(app.get_css_variables())                                 # every token is theme-supplied
    assert app.theme == "dags" and "dags" in app.available_themes
    for name in ("primary", "secondary", "accent", "success", "warning", "error"):
        assert getattr(board.DAGS_THEME, name)


def test_switching_theme_recolours_the_board(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            await pilot.pause()
            dark_bg = app.screen.styles.background
            dark_cells = dict(app.cell_styles)
            app.theme = "textual-light"
            await pilot.pause()
            assert app.screen.styles.background != dark_bg
            assert app.cell_styles != dark_cells
            app.theme = "dags"
            await pilot.pause()
            assert app.screen.styles.background == dark_bg
    run(go())


def test_focused_panel_has_room_and_footer_fits(setup):
    world, a, app, _ = setup

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:       # 100 before the i (File issue) key joined the footer
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("escape")
            claims = app.query_one("#claims")
            await until(pilot, lambda: claims.row_count == 1)
            assert claims.size.height - 3 >= 12                       # rows visible, minus border + header
            assert claims.border_title == "live claims (1)"
            assert app.query_one("#review").has_class("collapsed")
            await pilot.pause()
            footer = app.query_one("Footer")
            assert footer.size.height == 1
            assert max(w.region.right for w in footer.query("FooterKey")) <= 120
    run(go())


def test_tab_cycles_panels(setup):
    world, a, app, _ = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    work.submit_plan(a, d, "# The plan\nDo it.")

    async def go():
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.query_one("#claims").row_count == 1)
            await until(pilot, lambda: app.counts.get("plans") == 1)
            await pilot.press("tab")
            assert app.panel == "plans"                               # only panels with rows
            await pilot.press("tab")
            assert app.panel == "claims"
            assert not app.query_one("#idle").display
    run(go())


def test_idle_block_when_nothing_is_live(world):
    a = world.machine("mac-a")
    app = board.BoardApp(a, refresh_s=0.2, launch=world.launch, platform="darwin",
                         use_gh=False, run_poller=False)

    async def go():
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.snap is not None)
            assert app.query_one("#idle").display
            assert not app.query_one("#claims").display
            assert "start --quota-share 1" in shown(app.query_one("#idle"))
    run(go())


def test_input_dialog_scrolls_a_long_prompt():
    from textual.app import App
    from textual.containers import VerticalScroll
    long = "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(12))

    class Host(App):                                    # the real stylesheet, without the Board's panels
        CSS_PATH = str(Path(board.__file__).with_name("board.tcss"))

        def on_mount(self):                             # the theme, as BoardApp takes it
            self.register_theme(board.DAGS_THEME)
            self.theme = board.DAGS_THEME.name

    async def go():
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(board.InputScreen(long))
            await pilot.pause()
            scroll = app.screen.query_one("#prompt-scroll", VerticalScroll)
            assert scroll.virtual_size.height > scroll.size.height     # overflows, but scrolls
            assert app.screen.query_one("#answer").region.bottom <= 30   # the Input stays on screen
    run(go())


def test_answer_on_github_opens_the_recorded_comment(setup):
    world, a, app, opened = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    work.submit_plan(a, d, "# The plan\nDo it.")
    from dags import ledger as L
    url = L.read_checkpoint(d)["plan_comment_url"]
    assert url

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            app.selected_task = lambda *args, **kwargs: "T1"
            await until(pilot, lambda: app.snap and app.snap.by_key("T1").plan_status == "pending-review")
            await pilot.press("v")
            await until(pilot, lambda: isinstance(app.screen, board.PlanScreen))
            await pilot.click("#answer-on-github")
            await until(pilot, lambda: opened == [url])
            assert rv.plan_status(d, a.human_names) == "pending-review"      # opening the thread decides nothing
    run(go())


def test_open_ticket_on_a_blocked_task_opens_its_question(setup):
    world, a, app, opened = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    work.block(a, d, "keep cancelled matches?")
    from dags import ledger as L
    url = L.read_checkpoint(d)["question_comment_url"]

    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            app.selected_task = lambda *args, **kwargs: "T1"
            await until(pilot, lambda: app.snap and app.snap.by_key("T1") is not None)
            app.action_open_ticket()
            await until(pilot, lambda: opened == [url])
    run(go())


def test_needs_human_is_visible_in_a_narrow_claims_table(setup):
    """GH-102: a long title no longer pushes the signal off a 100-column terminal."""
    world, a, app, _ = setup
    d = rv.index(a.root)["T1"]
    work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    work.block(a, d, "keep cancelled matches?")

    async def go():
        async with app.run_test(size=(100, 40)) as pilot:
            claims = app.query_one("#claims")
            await until(pilot, lambda: claims.row_count == 1)
            width = claims.size.width
            first = claims.ordered_columns[0].get_render_width(claims) + claims.ordered_columns[1].get_render_width(claims)
            assert first < width                          # task and state both fit on screen
            row = claims.get_row_at(0)
            state = row[1]
            assert state.plain.startswith("needs human")
            assert app.cell_styles["accent"] in {str(sp.style) for sp in state.spans}
    run(go())


# ---------------------------------------------------------------------------
# the issue composer (GH-110): a form in front of dags/draft.py
# ---------------------------------------------------------------------------

DRAFT_TEXT = """---
labels: [next-version]
autonomy: human-must-review
repo: OWNER/app
epic: 1
depends_on: [4]
status: blocked
---
# Pasted title

The real body.

## Detail
"""
EPIC = "acme/plan#1"


@pytest.fixture
def composing(swarm, monkeypatch):
    from backends.github import GitHubBackend
    from conftest import FakeGh
    from dags import gh
    from fakes import FakeGitHub
    fake = FakeGitHub(repo="acme/plan")
    fake.seed()
    fake.labels.update({"next-version", "bug", "swarm:autonomy:human-must-review", "swarm:status:blocked",
                        "repo:OWNER/app"})
    gh.set_runner(FakeGh(fake))
    a = swarm.clone("mac-a")
    a.set_backend(GitHubBackend("acme/plan", cache_seconds=0))
    monkeypatch.setattr(a, "require_human", lambda: "roman")
    (a.root / "drafts").mkdir(exist_ok=True)
    app = board.BoardApp(a, refresh_s=0.5, use_gh=False, run_poller=False, open_url=[].append)
    yield a, fake, app
    gh.set_runner(None)


async def open_composer(app, pilot):
    await pilot.press("i")
    await until(pilot, lambda: isinstance(app.screen, board.IssueComposer) and app.screen.loaded)
    return app.screen


def option_values(widget):
    return [widget.get_option_at_index(i).value for i in range(widget.option_count)]


def drafts_in(a, sub=""):
    return sorted(p.name for p in (a.root / "drafts" / sub).glob("*.md"))


def test_composer_opens_and_populates_from_the_backend(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            assert option_values(c.query_one("#f-labels")) == ["bug", "next-version"]   # no swarm:*, repo:*, type:*
            assert c.choices["f-epic"] == {"acme/plan#1", "acme/plan#2"}                  # epics, nothing else
            deps = option_values(c.query_one("#f-depends"))
            assert "acme/plan#4" in deps and "acme/plan#1" not in deps and "acme/plan#3" not in deps  # open tasks
            assert c.choices["f-autonomy"] == set(board.AUTONOMY_TIERS)
            assert c.query_one("#f-repo").value == "OWNER/app"        # the only repo in backend.yaml
            assert "swarm:autonomy:" in shown(c.query_one("#preview"))
    run(go())


def test_preview_follows_every_change(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-autonomy").value = "human-must-review"
            await until(pilot, lambda: "swarm:autonomy:human-must-review" in shown(c.query_one("#preview")))
            c.query_one("#f-labels").select("bug")
            await until(pilot, lambda: shown(c.query_one("#preview")).split(": ", 1)[1].startswith("bug,"))
            c.query_one("#f-blocked").value = True
            await until(pilot, lambda: "swarm:status:blocked" in shown(c.query_one("#preview")))
            from dags import draft
            assert shown(c.query_one("#preview")) == "Labels on the issue: " + ", ".join(
                draft.resolved_labels(a.backend, c.current_draft()))
    run(go())


def test_pasting_a_draft_fills_the_other_fields(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").load_text(DRAFT_TEXT)
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            assert c.query_one("#f-body").text == "The real body.\n\n## Detail\n"
            assert c.query_one("#f-epic").value == EPIC
            assert c.query_one("#f-autonomy").value == "human-must-review"
            assert c.query_one("#f-blocked").value is True
            assert list(c.query_one("#f-labels").selected) == ["next-version"]
            assert list(c.query_one("#f-depends").selected) == ["acme/plan#4"]
    run(go())


def test_typing_a_heading_is_not_a_paste(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").focus()
            await pilot.press("#", " ", "x")
            await pilot.pause(0.2)
            assert c.query_one("#f-body").text == "# x" and c.query_one("#f-title").value == ""
    run(go())


def test_invalid_form_stays_open_names_the_fields_and_files_nothing(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-title").value = "No epic"
            await pilot.click("#file")
            await until(pilot, lambda: shown(c.query_one("#err-epic")))
            assert isinstance(app.screen, board.IssueComposer)
            assert "missing" in shown(c.query_one("#err-epic"))
            assert not shown(c.query_one("#err-title"))
    run(go())
    assert len(fake.issues) == 8 and drafts_in(a) == [] and drafts_in(a, "filed") == []


def test_ready_status_in_a_pasted_draft_is_refused(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").load_text(DRAFT_TEXT.replace("status: blocked", "status: ready"))
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            await pilot.click("#file")
            await until(pilot, lambda: shown(c.query_one("#err-status")))
            assert "ready" in shown(c.query_one("#err-status")) and len(fake.issues) == 8
    run(go())


def test_submit_goes_through_confirm_then_files_syncs_and_moves(composing):
    a, fake, app = composing

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").load_text(DRAFT_TEXT)
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            await pilot.click("#file")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            assert "create  task: Pasted title" in app.screen.message and len(fake.issues) == 8
            await pilot.press("n")                                    # No: the form stays, nothing filed
            await until(pilot, lambda: isinstance(app.screen, board.IssueComposer))
            assert len(fake.issues) == 8 and drafts_in(a) == []
            await pilot.click("#file")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: len(fake.issues) == 9)
            await until(pilot, lambda: any("filed https://github.com/acme/plan/issues/9" in s for s in app.said))
            assert not isinstance(app.screen, board.IssueComposer)
    run(go())
    assert "swarm:status:ready" not in fake.issues[9]["labels"]
    assert fake.issues[9]["parent"] == 1 and fake.issues[9]["blockedBy"] == [4]
    assert drafts_in(a) == [] and drafts_in(a, "filed") == ["pasted-title.md"]
    assert "issues/9" in (a.root / "drafts" / "filed" / "pasted-title.md").read_text()
    assert any("the ledger has it" in s for s in app.said)


def test_plan_sync_failure_is_reported_on_the_feed(composing, monkeypatch):
    a, fake, app = composing
    from dags import plan

    def boom(ctx):
        raise RuntimeError("offline")
    monkeypatch.setattr(plan, "sync", boom)

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").load_text(DRAFT_TEXT)
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            await pilot.click("#file")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: any("plan sync failed (offline)" in s for s in app.said))
    run(go())
    assert len(fake.issues) == 9


def test_save_writes_a_draft_without_filing(composing):
    a, fake, app = composing
    from dags import draft

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-title").value = "Saved for later"
            c.query_one("#f-body").load_text("Some body\n")
            c.query_one("#f-labels").select("bug")
            await pilot.click("#save")
            await until(pilot, lambda: drafts_in(a) == ["saved-for-later.md"])
            assert isinstance(app.screen, board.IssueComposer)
    run(go())
    d = draft.load(a.root / "drafts" / "saved-for-later.md")
    assert (d.title, d.body, d.labels) == ("Saved for later", "Some body\n", ["bug"])
    assert len(fake.issues) == 8


def test_d_loads_a_draft_and_filing_it_moves_that_file(composing):
    a, fake, app = composing
    (a.root / "drafts" / "mine.md").write_text(DRAFT_TEXT)
    (a.root / "drafts" / "README.md").write_text("# not a draft\n")
    (a.root / "drafts" / "filed").mkdir()
    (a.root / "drafts" / "filed" / "old.md").write_text("# Old\n")

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.set_focus(c.query_one("#f-blocked"))                    # a text field would take the 'd'
            await pilot.press("d")
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            assert [o for o, _ in app.screen.options] == ["mine.md"]   # not filed/, not README
            await pilot.press("enter")
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            assert c.path == a.root / "drafts" / "mine.md" and c.query_one("#f-epic").value == EPIC
            await pilot.click("#file")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: len(fake.issues) == 9)
            await until(pilot, lambda: "mine.md" in drafts_in(a, "filed"))
    run(go())
    assert drafts_in(a) == ["README.md"]


def test_partly_failed_filing_leaves_the_stamped_draft(composing, monkeypatch):
    a, fake, app = composing
    from backends.github import GitHubBackend
    from dags import gh

    def refuse(self, ref, parent):
        raise gh.GhError(["issue", "edit"], 1, "no permission")
    monkeypatch.setattr(GitHubBackend, "set_parent", refuse)
    (a.root / "drafts" / "mine.md").write_text(DRAFT_TEXT)

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.set_focus(c.query_one("#f-blocked"))
            await pilot.press("d")
            await until(pilot, lambda: isinstance(app.screen, board.ChoiceScreen))
            await pilot.press("enter")
            await until(pilot, lambda: c.query_one("#f-title").value == "Pasted title")
            await pilot.click("#file")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: any("stopped" in s for s in app.said))
    run(go())
    assert drafts_in(a) == ["mine.md"] and drafts_in(a, "filed") == []
    assert "url: https://github.com/acme/plan/issues/9" in (a.root / "drafts" / "mine.md").read_text()


def test_e_edits_the_body_in_the_editor(composing, monkeypatch):
    a, fake, app = composing
    import contextlib
    seen = []

    def fake_editor(cmd, check=False):
        seen.append(cmd)
        Path(cmd[-1]).write_text("Edited in the editor\n")
    monkeypatch.setenv("EDITOR", "myedit --wait")
    monkeypatch.setattr(board.subprocess, "run", fake_editor)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            c = await open_composer(app, pilot)
            c.query_one("#f-body").load_text("first draft")
            c.set_focus(c.query_one("#f-blocked"))
            await pilot.press("e")
            await until(pilot, lambda: c.query_one("#f-body").text == "Edited in the editor\n")
    run(go())
    assert seen[0][:2] == ["myedit", "--wait"]
