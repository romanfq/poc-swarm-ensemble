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
            app.query_one("#claims").focus()
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
            plans.focus()
            await pilot.press("v")
            await until(pilot, lambda: isinstance(app.screen, board.PlanScreen))
            await pilot.click("#approved")
            await until(pilot, lambda: rv.plan_status(d, a.human_names) == "approved")
            cid = rv.resolve(d, timeutil.now(), 900).winner.id
            from dags import ledger as L
            L.complete(a, d, "pr-opened", claim_id=cid, pr_url=pr["url"], worker="claude")
            review = app.query_one("#review")
            await until(pilot, lambda: review.row_count == 1)
            review.focus()
            await pilot.press("O")
            await until(pilot, lambda: opened == [pr["url"]])
            await pilot.press("m")
            await until(pilot, lambda: isinstance(app.screen, board.ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: rv.is_done(d))
    run(go())
    assert merged == [("OWNER/app", str(pr["number"]))]


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
