"""Phase 5: scheduler cycle, worker choice, heartbeats (Ch.6.4, 7, 8, 10.7)."""
import json
from datetime import timedelta

import pytest

import resolve as rv
from backends.base import TaskRef
from conftest import sh
from dags import ledger as L
from dags import plan, timeutil, work, worktree
from dags.scheduler import Heartbeater


def _plan(world, *tasks):
    world.backend.add("E1", title="Epic", epic=True)
    for key, extra in tasks:
        world.backend.add(key, title=f"Task {key}", epic_of="E1",
                          labels=["repo:OWNER/app", *extra.pop("labels", [])], **extra)


def test_claims_prepares_and_asks_for_a_worker(world):
    _plan(world, ("T1", {"labels": ["swarm:autonomy:auto-pr"]}))
    a = world.machine("mac-a")
    rep = world.scheduler(a).cycle()
    assert rep.claimed == ["T1"] and rep.awaiting_worker == ["T1"] and not rep.errors
    d = rv.index(a.root)["T1"]
    assert rv.task_state(d, timeutil.now(), 900) == "claimed"
    assert world.backend.get_task(TaskRef("T1")).status == "claimed"
    kind, text = world.notes[-1]
    assert kind == "needs-worker"
    assert text.splitlines() == ["[swarm-board] I have claimed T1 for completion, who is my worker?",
                                 "  a) claude", "  b) IntelliJ + Human", "  c) VSCode + Human"]
    wt = worktree.worktree_path(a, d)
    skill = wt / ".swarm-task"
    assert {p.name for p in skill.iterdir()} == {"README.md", "swarm-task", "spec.md", "conventions.md",
                                                 "context.json"}
    assert "# T1: Task T1" in (skill / "spec.md").read_text()
    ctxjson = json.loads((skill / "context.json").read_text())
    assert ctxjson["claim_id"] == rv.resolve(d, timeutil.now(), 900).winner.id
    assert ctxjson["branch"] == "swarm/T1" and ctxjson["test_command"] == "true"
    assert worktree.is_excluded(wt), ".swarm-task must be git-excluded before any worker touches it"
    assert sh(["git", "status", "--porcelain"], wt).strip() == ""
    assert sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt).strip() == "swarm/T1"
    # a second cycle neither re-claims nor prepares again
    rep2 = world.scheduler(a).cycle()
    assert rep2.claimed == [] and rep2.room == 0 and rep2.awaiting_worker == ["T1"]


@pytest.mark.parametrize("choice,expect", [
    ("a", "osascript"), ("claude", "osascript"), ("b", "open"), ("intellij", "open"), ("VSCode + Human", None)])
def test_choose_worker_dispatches(world, choice, expect, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/code" if name == "code" else None)
    _plan(world, ("T1", {}))
    a = world.machine("mac-a")
    world.scheduler(a).cycle()
    d = rv.index(a.root)["T1"]
    label = work.choose_worker(a, d, choice, launch=world.launch, platform="darwin")
    cmd = world.launched[-1]
    wt = worktree.worktree_path(a, d)
    if expect == "osascript":
        assert cmd[0] == "osascript" and label == "claude"
        script = " ".join(cmd)
        assert 'tell application "Terminal"' in script and str(wt) in script and "swarm-task" in script
    elif expect == "open":
        assert cmd[:3] == ["open", "-na", "IntelliJ IDEA.app"] and label == "IntelliJ + Human"
        assert cmd[-1] == str(wt / ".swarm-task" / "README.md")
    else:
        assert cmd == ["/usr/local/bin/code", "-n", str(wt), str(wt / ".swarm-task" / "README.md")]
    cp = L.read_checkpoint(d)
    assert cp["worker"] in ("claude", "intellij", "vscode") and cp["branch"] == "swarm/T1"
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"
    assert world.backend.get_task(TaskRef("T1")).status == "in-progress"


def test_default_worker_dispatches_immediately(world):
    _plan(world, ("T1", {}), ("T2", {}))
    a = world.machine("mac-a")
    rep = world.scheduler(a, share=2, worker="claude").cycle()
    assert sorted(rep.dispatched) == ["T1", "T2"]
    assert len(world.launched) == 2
    assert all(L.read_checkpoint(rv.index(a.root)[k])["worker"] == "claude" for k in ("T1", "T2"))


def test_human_must_scope_never_goes_to_ai(world):
    _plan(world, ("T1", {"labels": ["swarm:autonomy:human-must-scope"]}))
    a = world.machine("mac-a")
    assert world.scheduler(a, worker="claude").cycle().claimed == []
    rep = world.scheduler(a).cycle()                 # no default worker: a human will choose
    assert rep.claimed == ["T1"]
    assert "human workers only" in world.notes[-1][1]
    d = rv.index(a.root)["T1"]
    with pytest.raises(work.WorkError):
        work.choose_worker(a, d, "claude", launch=world.launch, platform="darwin")
    assert work.choose_worker(a, d, "intellij", launch=world.launch, platform="darwin") == "IntelliJ + Human"


def test_launchers_are_macos_only(world):
    import workers
    _plan(world, ("T1", {}))
    a = world.machine("mac-a")
    world.scheduler(a).cycle()
    with pytest.raises(workers.WorkerError):
        work.choose_worker(a, rv.index(a.root)["T1"], "claude", launch=world.launch, platform="linux")
    assert world.launched == []


def test_two_machines_one_task(world):
    _plan(world, ("T1", {}))
    a, b = world.machine("mac-a"), world.machine("mac-b")
    plan.sync(a)
    b.coord.pull()
    # both think T1 is ready; b claims without seeing a's claim
    ra = world.scheduler(a, run_plan_sync=False).cycle()
    d_b = rv.index(b.root)["T1"]
    cid_b = L.claim(b, d_b, worker=None)            # simulates b's cycle racing a
    b.coord.pull()
    res = rv.resolve(d_b, timeutil.now(), 900)
    assert ra.claimed == ["T1"] and res.winner.machine == "mac-a"
    rep_b = world.scheduler(b, run_plan_sync=False).cycle()   # b tidies: withdraws its losing claim
    assert rep_b.lost == ["T1"] and rep_b.claimed == []
    b.coord.pull()
    assert cid_b in rv.read_withdrawals(d_b)
    assert rv.read_withdrawals(d_b)[cid_b]["reason"] == "lost-race"
    assert any(k == "lost" for k, _ in world.notes)


def test_quota_share_and_global_n(world):
    _plan(world, ("T1", {}), ("T2", {}), ("T3", {}))
    a, b = world.machine("mac-a"), world.machine("jane-mac", human="jane")
    assert world.scheduler(a, share=1).cycle().claimed == ["T1"]
    L.set_quota(b, 1, "tight budget")
    rep = world.scheduler(b, share=5).cycle()
    assert rep.room == 0 and rep.claimed == []
    L.set_quota(b, 3)
    assert world.scheduler(b, share=5).cycle().claimed == ["T2", "T3"]


def test_throttle_record_overrides_share(world):
    _plan(world, ("T1", {}), ("T2", {}))
    a = world.machine("mac-a")
    L.control(a, "throttle", quota_share=0)
    assert world.scheduler(a, share=2).cycle().room == 0
    L.control(a, "throttle", quota_share=2)
    assert len(world.scheduler(a, share=0).cycle().claimed) == 2


def test_pause_resume_and_stop(world):
    _plan(world, ("T1", {}))
    a = world.machine("mac-a")
    L.control(a, "pause")
    rep = world.scheduler(a).cycle()
    assert rep.paused and rep.claimed == []
    L.control(a, "resume")
    assert world.scheduler(a).cycle().claimed == ["T1"]
    s = world.scheduler(a, started_clock=rv.max_clock(a.root))
    assert not s.cycle().stop_requested
    L.control(a, "stop")
    assert s.cycle().stop_requested
    L.control(a, "start", quota_share=1)
    later = world.scheduler(a, started_clock=rv.max_clock(a.root))
    assert not later.cycle().stop_requested


def test_takeover_is_soft_priority(world):
    world.backend.add("E1", title="E1", epic=True)
    world.backend.add("E2", title="E2", epic=True)
    world.backend.add("A1", title="a", epic_of="E1", labels=["repo:OWNER/app"])
    world.backend.add("B1", title="b", epic_of="E2", labels=["repo:OWNER/app"])
    a, b = world.machine("mac-a"), world.machine("mac-b")
    plan.sync(a)
    L.priority(a, "E1", "takeover")
    assert world.scheduler(b, share=1).cycle().claimed == ["B1"]      # avoids a's epic
    assert world.scheduler(b, share=2).cycle().claimed == ["A1"]      # ...unless nothing else


def test_dependencies_and_blocked_epic(world):
    _plan(world, ("T1", {}), ("T2", {"blocked_by": ["T1"]}))
    a = world.machine("mac-a")
    assert world.scheduler(a, share=5).cycle().claimed == ["T1"]
    world.backend.set_status(TaskRef("E1"), "blocked")
    world.backend.add("T3", title="x", epic_of="E1", labels=["repo:OWNER/app"])
    assert world.scheduler(a, share=5).cycle().claimed == []


def test_arbitration_moves_the_task(world):
    _plan(world, ("T1", {}))
    a, b = world.machine("mac-a"), world.machine("mac-b")
    world.scheduler(a).cycle()
    b.coord.pull()
    d = rv.index(b.root)["T1"]
    cid_b = L.claim(b, d)
    L.arbitrate(b, d, cid_b, "b's branch already has the fix")
    rep = world.scheduler(a, run_plan_sync=False).cycle()
    assert rep.lost == ["T1"]
    a.coord.pull()
    d_a = rv.index(a.root)["T1"]
    w = [x for x in rv.read_withdrawals(d_a).values() if x["machine"] == "mac-a"]
    assert w and w[0]["reason"] == "arbitration"
    rep_b = world.scheduler(b, run_plan_sync=False).cycle()
    assert rep_b.awaiting_worker == ["T1"]
    assert rv.retry_count(d_a, timeutil.now(), 900) == 0


def test_resume_on_another_machine_reuses_branch(world):
    _plan(world, ("T1", {}))
    a, b = world.machine("mac-a"), world.machine("mac-b")
    world.scheduler(a, worker="claude").cycle()
    d_a = rv.index(a.root)["T1"]
    wt_a = worktree.worktree_path(a, d_a)
    (wt_a / "feed.py").write_text("print('half done')\n")
    sh(["git", "add", "feed.py"], wt_a)
    sh(["git", "commit", "-qm", "wip"], wt_a)
    sh(["git", "push", "-q", "origin", "swarm/T1"], wt_a)
    work.note(a, d_a, tried=["polling every 60s"], remaining=["parse scores"])
    # mac-a dies: no heartbeats for 20 minutes
    later = timeutil.now() + timedelta(minutes=20)
    b.coord.pull()
    d_b = rv.index(b.root)["T1"]
    assert rv.ledger_ready(b.root, d_b, later, 900)
    import dags.timeutil as tu
    tu.set_offset(20 * 60)
    try:
        rep = world.scheduler(b, worker="vscode", run_plan_sync=False).cycle()
        assert rep.claimed == ["T1"] and rep.dispatched == ["T1"]
        wt_b = worktree.worktree_path(b, d_b)
        assert (wt_b / "feed.py").read_text() == "print('half done')\n"
        cp = L.read_checkpoint(d_b)
        assert cp["tried"] == ["polling every 60s"] and cp["remaining"] == ["parse scores"]
        assert cp["previous_claims"] and cp["machine"] == "mac-b"
        assert rv.retry_count(d_b, tu.now(), 900) == 1
    finally:
        tu.set_offset(0)


def test_heartbeats_keep_claims_alive_and_idle_limit(world):
    import dags.timeutil as tu
    _plan(world, ("T1", {}))
    a = world.machine("mac-a")
    world.scheduler(a, worker="intellij").cycle()
    d = rv.index(a.root)["T1"]
    hb = Heartbeater(a, notify=world.notify)
    assert hb.cycle() == 1
    assert (d / "heartbeats" / "mac-a.yaml").exists()
    object.__setattr__(a.settings, "human_idle_s", 3600.0)
    clock = {"m": 0}
    written = []

    def advance(to_minutes):
        while clock["m"] < to_minutes:
            clock["m"] += 10                        # heartbeats every 10 min, lease 15
            tu.set_offset(clock["m"] * 60)
            written.append(hb.cycle())

    try:
        advance(50)
        assert all(written) and not [k for k, _ in world.notes if k.startswith("idle")]
        advance(70)                                 # > 1h without progress: ask once
        assert [k for k, _ in world.notes].count("idle") == 1
        assert "Still working on T1?" in world.notes[-1][1]
        work.still_working(a, d)                    # the human answers at 70 min
        advance(120)
        assert [k for k, _ in world.notes].count("idle") == 1
        advance(140)                                # silent again: asked a second time
        assert [k for k, _ in world.notes].count("idle") == 2
        written.clear()
        advance(160)                                # 90 min idle > idle + lease: stop
        assert written[-1] == 0
        assert world.notes[-1][0] == "idle-expire"
        advance(180)
        assert rv.ledger_ready(a.root, d, tu.now(), 900)
    finally:
        tu.set_offset(0)


def test_heartbeat_detects_loss(world):
    _plan(world, ("T1", {}))
    a, b = world.machine("mac-a"), world.machine("mac-b")
    world.scheduler(a).cycle()
    b.coord.pull()
    d_b = rv.index(b.root)["T1"]
    cid_a = rv.resolve(d_b, timeutil.now(), 900).winner.id
    L.arbitrate(b, d_b, "none", "freeze while we talk")
    items_before = [(rv.index(a.root)["T1"], cid_a)]
    lost = L.heartbeat(a, items_before)
    assert lost == items_before


def test_release_is_not_a_failure(world):
    _plan(world, ("T1", {}))
    a = world.machine("mac-a")
    world.scheduler(a).cycle()
    d = rv.index(a.root)["T1"]
    work.release(a, d)
    assert rv.ledger_ready(a.root, d, timeutil.now(), 900)
    assert rv.retry_count(d, timeutil.now(), 900) == 0
    assert world.backend.get_task(TaskRef("T1")).status == "ready"
    with pytest.raises(L.LostClaim):
        work.release(a, d)
