"""Swarm Board pilot tests (Ch.10). Need textual — run on the Mac via bin/dev-setup.sh."""
import asyncio
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
            await pilot.press("e")
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
        async with app.run_test(size=(100, 30)) as pilot:
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
            assert max(w.region.right for w in footer.query("FooterKey")) <= 100
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
