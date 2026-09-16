"""Phase 6: the poller (Ch.9.4), the feed (Ch.10.3) and notify."""
import json
import subprocess

import pytest

import resolve as rv
from dags import records as R
from dags import timeutil
from backends.base import TaskRef
from dags import feed, work, worktree
from dags import ledger as L
from dags.notify import Notifier
from poll import Poller

OK = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731


def kinds(rep):
    return [k for k, _ in rep.events]


def texts(rep, kind):
    return [t for k, t in rep.events if k == kind]


@pytest.fixture
def pr_open(world):
    """mac-a's claude worker has opened a PR for T1 (auto-pr)."""
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll", epic_of="E1", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    world.backend.add("T2", title="Parse", epic_of="E1", blocked_by=["T1"], labels=["repo:OWNER/app"])
    a = world.machine("mac-a")
    poller = Poller(a, notify=world.notify)
    poller.cycle()                                   # first run: quiet baseline
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    wt = worktree.worktree_path(a, d)
    work.submit_plan(a, d, "plan")
    (wt / "poller.py").write_text("x = 1\n")
    work.note(a, d, summary="Adds a poller")
    url = work.finish(a, d, wt, test_runner=OK)
    return world, a, d, wt, url, poller


def test_first_run_is_quiet_then_feed_flows(world):
    world.backend.add("T1", title="x", labels=["repo:OWNER/app"])
    a = world.machine("mac-a")
    world.scheduler(a).cycle()
    p = Poller(a, notify=world.notify, use_gh=False)
    assert p.cycle().events == []
    L.control(a, "pause")
    rep = p.cycle()
    assert texts(rep, "feed") == ["Roman paused their swarm (mac-a)"]
    assert Poller(a, use_gh=False).cycle().events == []   # state persisted in .swarm/poller-state.json


def test_contract_watcher_strips_skill_then_announces(pr_open):
    world, a, d, wt, url, poller = pr_open
    assert (wt / ".swarm-task").exists()
    rep = poller.cycle()
    assert not (wt / ".swarm-task").exists(), "scaffolding removed before announcing"
    assert f"claude has finished T1. The PR can be found at {url}" in texts(rep, "finished")
    assert texts(rep, "review") == [f"T1 is awaiting review: {url}"]
    assert rep.pr_status[rv.read_meta(d)["key"]]["state"] == "OPEN"
    assert (wt / "poller.py").exists(), "the worktree itself stays until merge"
    again = poller.cycle()
    assert "finished" not in kinds(again) and "review" not in kinds(again)


def test_contract_not_fulfilled_without_checkpoint(pr_open):
    world, a, d, wt, url, poller = pr_open
    cp = L.read_checkpoint(d)
    cp.pop("pr_url")
    R.write_replace(d / "checkpoint.yaml", cp)
    poller.cycle()
    assert (wt / ".swarm-task").exists()


def test_merge_marks_done_and_unblocks(pr_open):
    world, a, d, wt, url, poller = pr_open
    poller.cycle()
    world.prs.get(url)["state"] = "MERGED"
    rep = poller.cycle()
    assert texts(rep, "merged") == ["T1 was merged — done; dependants can start"]
    assert rv.is_done(d)
    assert world.backend.get_task(TaskRef("T1")).status == "done"
    assert not wt.exists()
    now = timeutil.now()
    assert rv.ledger_ready(a.root, rv.index(a.root)["T2"], now, 900)
    assert "T1 is done (merged)" in texts(poller.cycle(), "feed")


def test_closed_pr_is_a_rejected_approach(pr_open):
    world, a, d, wt, url, poller = pr_open
    world.prs.get(url)["state"] = "CLOSED"
    rep = poller.cycle()
    assert "rejected" in kinds(rep)
    assert rv.task_state(d, timeutil.now(), 900) == "rejected"
    t = world.backend.get_task(TaskRef("T1"))
    assert t.status == "blocked" and "approach rejected" in world.backend.comments(TaskRef("T1"))[-1]
    assert not rv.ledger_ready(a.root, d, timeutil.now(), 900)
    world.backend.set_status(TaskRef("T1"), "ready")      # a human re-plans
    from dags import plan
    assert plan.sync(a).replanned == ["T1"]
    assert rv.ledger_ready(a.root, d, timeutil.now(), 900)


def test_changes_requested_reopens_once(pr_open):
    world, a, d, wt, url, poller = pr_open
    poller.cycle()
    pr = world.prs.get(url)
    pr["reviews"].append({"id": "R1", "author": {"login": "jane"}, "state": "CHANGES_REQUESTED",
                          "body": "fix: nulls", "submittedAt": "t"})
    rep = poller.cycle()
    assert texts(rep, "changes-requested")
    assert rv.ledger_ready(a.root, d, timeutil.now(), 900)
    assert world.backend.get_task(TaskRef("T1")).status == "ready"
    # a second poller (another human's machine) sees the same review but doesn't double-count
    b = world.machine("mac-b")
    rep_b = Poller(b, notify=world.notify).cycle()
    assert "changes-requested" not in kinds(rep_b)
    reopened = [x for _, x in R.read_dir(d / "completions") if x["kind"] == "reopened"]
    assert len(reopened) == 1


def test_overlapping_prs_need_a_merge_order(world):
    world.backend.add("T1", title="a", labels=["repo:OWNER/app"])
    world.backend.add("T2", title="b", labels=["repo:OWNER/app"])
    a = world.machine("mac-a")
    world.scheduler(a, share=2).cycle()
    idx = rv.index(a.root)
    p1 = world.prs.add("OWNER/app", "swarm/T1", files=["src/Config.java", "a.txt"])
    p2 = world.prs.add("OWNER/app", "swarm/T2", files=["src/Config.java"])
    for key, pr in (("T1", p1), ("T2", p2)):
        cid = rv.resolve(idx[key], timeutil.now(), 900).winner.id
        L.complete(a, idx[key], "pr-opened", claim_id=cid, pr_url=pr["url"], worker="claude")
    p = Poller(a, notify=world.notify)
    rep = p.cycle()
    assert rep.overlaps == [("T1", "T2", ["src/Config.java"])]
    assert "T1 and T2 both change src/Config.java in OWNER/app" in texts(rep, "overlap")[0]
    assert "overlap" not in kinds(p.cycle())


def test_stale_heartbeat_is_reported(world):
    world.backend.add("T1", title="a", labels=["repo:OWNER/app"])
    a = world.machine("mac-a")
    p = Poller(a, notify=world.notify, use_gh=False)
    p.cycle()
    world.scheduler(a).cycle()
    import dags.timeutil as tu
    tu.set_offset(30 * 60)
    try:
        rep = p.cycle()
        assert texts(rep, "stale") == ["T1: heartbeat from mac-a went stale; the task is ready to be "
                                       "resumed from its checkpoint"]
        assert "stale" not in kinds(p.cycle())
    finally:
        tu.set_offset(0)


def test_conflicts_escalate_to_arbitration(world):
    world.backend.add("T1", title="a", labels=["repo:OWNER/app"])
    a, b = world.machine("mac-a"), world.machine("mac-b")
    from dags import plan
    plan.sync(a)
    b.coord.pull()
    d = rv.index(a.root)["T1"]
    L.claim(a, d)
    L.claim(b, rv.index(b.root)["T1"])
    a.coord.pull()
    p = Poller(a, notify=world.notify, use_gh=False)
    rep1 = p.cycle()
    assert texts(rep1, "conflict") == ["T1: mac-a and mac-b both hold a live claim"]
    assert "needs-arbitration" not in kinds(rep1)
    rep2 = p.cycle()
    assert texts(rep2, "needs-arbitration")[0].startswith("T1 needs arbitration: mac-a and mac-b")
    assert rep2.needs_arbitration == [rv.read_meta(d)["key"]]
    assert "needs-arbitration" not in kinds(p.cycle())
    L.arbitrate(a, d, rv.read_claims(d)[0].id, "a was first")
    assert p.cycle().needs_arbitration == []


def test_feed_describes_records(world):
    world.backend.add("T1", title="a", labels=["repo:OWNER/app"])
    a = world.machine("mac-a")
    jane = world.machine("jane-mac", human="jane")
    world.scheduler(a).cycle()
    jane.coord.pull()
    d = rv.index(jane.root)["T1"]
    cid = rv.read_claims(d)[0].id
    L.arbitrate(jane, d, cid, "branch touches shared config")
    L.priority(jane, "EPIC-14", "takeover")
    L.set_quota(jane, 4, "more budget")
    L.control(a, "throttle", quota_share=2)
    a.coord.pull()
    lines = [e.text for e in feed.all_events(a.root)]
    assert "Roman's swarm claimed T1 (mac-a)" in lines
    assert "Jane arbitrated T1: mac-a wins — 'branch touches shared config'" in lines
    assert "Jane's swarm took over EPIC-14" in lines
    assert "Jane set the global quota to 4 — 'more budget'" in lines
    assert "Roman throttled mac-a to quota share 2" in lines
    seen = set()
    assert len(feed.new_events(a.root, seen)) == len(lines)
    assert feed.new_events(a.root, seen) == []


def test_notifier_log_desktop_webhook(tmp_path):
    sent, ran, heard = [], [], []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return Resp()

    from dags import notify
    notify.subscribe(lambda t, k: heard.append(t))
    try:
        n = Notifier(tmp_path, {"notify": {"desktop": True, "webhook": "https://hooks.example/x"}},
                     opener=opener, run=lambda cmd, **kw: ran.append(cmd), platform="darwin")
        n("T1 is awaiting review", "review")
    finally:
        notify._listeners.clear()
    assert "[review] T1 is awaiting review" in (tmp_path / "notifications.log").read_text()
    assert sent == [{"text": "[DAGS] T1 is awaiting review", "kind": "review"}]
    assert ran[0][0] == "osascript" and "T1 is awaiting review" in ran[0][2]
    assert heard == ["T1 is awaiting review"]
    quiet = Notifier(tmp_path, {}, opener=opener, run=lambda cmd, **kw: ran.append(cmd), platform="darwin")
    quiet("x")
    assert len(sent) == 1 and len(ran) == 1
