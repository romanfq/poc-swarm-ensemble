"""GH-3: plan reviews and checkpoint changes reach the activity feed (Ch.10.3)."""
import resolve as rv
from conftest import sh
from dags import actions, feed, work
from dags import ledger as L
from dags import records as R
from dags.notify import Notifier
from poll import Poller


def _task(world, key="T1", autonomy="human-must-review"):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add(key, title=f"Task {key}", epic_of="E1",
                      labels=["repo:OWNER/app", f"swarm:autonomy:{autonomy}"])


def _lines(ctx):
    ctx.coord.pull()
    return [e.text for e in feed.all_events(ctx.root)]


def _events(ctx, kind):
    return [e for e in feed.all_events(ctx.root) if e.kind == kind]


def test_plan_submitted_approved_and_sent_back(world):
    _task(world)
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    work.submit_plan(a, d, "# Plan\npoll every 60s")
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"], "changes-requested", "use the push feed")
    work.submit_plan(a, d, "# Plan v2")
    jane.coord.pull()
    work.approve_plan(jane, rv.index(jane.root)["T1"])
    lines = _lines(a)
    assert lines.count("claude submitted a plan for T1 (mac-a) — waiting for review") == 2
    assert "Jane sent the plan for T1 back — 'use the push feed'" in lines
    assert "Jane approved the plan for T1" in lines
    assert [e.kind for e in feed.all_events(a.root) if e.kind.startswith(("plan", "event:plan"))] == [
        "event:plan-submitted", "plan-review:changes-requested", "event:plan-submitted", "plan-review:approved"]


def test_self_approved_plan(world):
    _task(world, autonomy="auto-pr")
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    work.submit_plan(a, rv.index(a.root)["T1"], "plan")
    assert "claude submitted a plan for T1 (mac-a) — self-approved (auto-pr)" in _lines(a)


def test_event_shares_the_checkpoint_commit_and_clock(world):
    _task(world)
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    head = a.coord.head()
    work.block(a, d, "store cancelled matches?")
    assert len(a.coord.recent_files(head)) == 1                     # only the event is new
    assert sh(["git", "rev-list", "--count", f"{head}..HEAD"], a.root).strip() == "1"
    ev, = [e for _, e in R.read_dir(d / "events") if e["kind"] == "needs-human"]
    assert ev["claim_id"] == L.read_checkpoint(d)["claim_id"]
    assert R.clock_of(ev) == R.clock_of(L.read_checkpoint(d))
    assert "T1 needs a human decision — 'store cancelled matches?'" in _lines(a)


def test_worker_dispatched_awaiting_and_still_working(world):
    _task(world)
    a = world.machine("mac-a")
    s = world.scheduler(a)
    s.cycle()
    world.scheduler(a).cycle()                                       # a restart doesn't repeat it
    d = rv.index(a.root)["T1"]
    assert _lines(a).count("T1 is waiting for a worker on mac-a") == 1
    work.choose_worker(a, d, "vscode", launch=world.launch, platform="darwin")
    work.still_working(a, d)
    lines = _lines(a)
    assert "Roman's swarm handed T1 to VSCode + Human (mac-a)" in lines
    assert "Roman confirmed T1 is still being worked on (mac-a)" in lines


def test_quota_pause_requested_and_lifted(world):
    world.backend.add("E1", title="Epic", epic=True)
    for k in ("T1", "T2"):
        world.backend.add(k, title=k, epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    a = world.machine("mac-a")
    world.scheduler(a, share=2, worker="claude").cycle()
    L.control(a, "throttle", quota_share=1)
    world.scheduler(a, share=2).cycle()
    L.control(a, "throttle", quota_share=2)
    world.scheduler(a, share=2).cycle()
    lines = _lines(a)
    assert "The quota was lowered: T2 was asked to record its progress and stop within 15 minutes (mac-a)" in lines
    assert "The quota was raised again: T2 can carry on (mac-a)" in lines


def test_pause_and_resume_are_idempotent(world):
    a = world.machine("mac-a")
    assert actions.pause(a) == "paused mac-a"
    assert actions.pause(a) == "mac-a is already paused"
    assert actions.resume(a) == "resumed mac-a"
    assert actions.resume(a) == "mac-a isn't paused"
    assert [e.kind for e in feed.all_events(a.root)] == ["control:pause", "control:resume"]


def test_poller_stamps_with_the_record_time_and_skips_own_announcements(world, tmp_path):
    _task(world)
    a = world.machine("mac-a")
    p = Poller(a, notify=Notifier(tmp_path), use_gh=False)
    p.cycle()
    L.control(a, "pause")
    world.scheduler(a, worker="claude").cycle()     # paused: no claim
    wall = R.read_dir(a.root / "control")[-1][1]["wall_utc"]
    rep = p.cycle()
    assert rep.events == [("feed", "Roman paused their swarm (mac-a)")] and rep.stamps == [wall]
    assert (tmp_path / "notifications.log").read_text().startswith(f"{wall} [feed] Roman paused")
    L.control(a, "resume")
    world.scheduler(a, worker="claude").cycle()     # dispatch: the scheduler notified it already
    texts = [t for _, t in p.cycle().events]
    assert "Roman resumed their swarm (mac-a)" in texts
    assert not any("handed T1" in t for t in texts)
    b = world.machine("mac-b")
    pb = Poller(b, use_gh=False)
    pb.state["first_run"] = False
    assert any("handed T1 to claude (mac-a)" in t for _, t in pb.cycle().events)


def test_notifier_uses_the_given_time(tmp_path):
    n = Notifier(tmp_path)
    n("hello", "feed", at="2026-09-19T21:41:39+00:00")
    n("now")
    first, second = (tmp_path / "notifications.log").read_text().splitlines()
    assert first == "2026-09-19T21:41:39+00:00 [feed] hello"
    assert second.endswith("[info] now")
