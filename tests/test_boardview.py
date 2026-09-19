"""What the Board shows (Ch.10.5), without Textual."""
from datetime import timedelta

import resolve as rv
from dags import boardview, snapshot, work
from dags import ledger as L


def test_age_text():
    assert boardview.age_text(None) == "-"
    assert boardview.age_text(42) == "42s"
    assert boardview.age_text(300) == "5m"
    assert boardview.age_text(3 * 3600 + 5 * 60) == "3h05m"


def test_panels_from_a_live_ledger(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    world.backend.add("T2", title="Parse", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    world.backend.add("T3", title="Store", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    a, b = world.machine("mac-a"), world.machine("mac-b")
    world.scheduler(a, share=2).cycle()               # T1, T2 claimed, awaiting worker
    work.choose_worker(a, rv.index(a.root)["T1"], "vscode", launch=world.launch, platform="darwin")
    work.submit_plan(a, rv.index(a.root)["T1"], "# plan")
    b.coord.pull()
    L.claim(b, rv.index(b.root)["T2"])                 # live conflict on T2
    a.coord.pull()
    snap = snapshot.take(a, share=2)

    claims = dict(boardview.claim_rows(snap))
    assert set(claims) == {"T1", "T2"}
    t1 = claims["T1"]
    assert t1[:4] == ("T1", "Poll", "mac-a", "roman") and t1[6] == "vscode" and t1[7] == "in-progress"
    assert claims["T2"][6] == "awaiting worker"

    assert [k for k, _ in boardview.plan_rows(snap)] == ["T1"]
    assert boardview.plan_rows(snap)[0][1][3] == "pending-review"

    arb = dict(boardview.arbitration_rows(snap, []))
    assert arb == {"T2": ("T2", "mac-a, mac-b", "live conflict")}
    flagged = dict(boardview.arbitration_rows(snap, ["T3"]))           # flagged by the poller's thrash detection
    assert flagged["T3"] == ("T3", "-", "keeps racing (thrash)")
    q = boardview.quota(snap)
    assert (q.used, q.total, q.mine, q.share) == (2, 3, 2, 2)
    assert q.text == "global 2/3   ·   this machine 2/2"
    assert [t.short for t in snap.awaiting_worker()] == ["T2"]

    L.control(b, "pause")
    a.coord.pull()
    snap = snapshot.take(a, share=2)
    assert boardview.machine_line(snap, 123) == "mac-a · running   ·   others — mac-b: paused"
    assert boardview.machine_line(snap, None).startswith("mac-a · daemon not running")
    from dags import timeutil
    info = {"started_utc": timeutil.iso(timeutil.now() - timedelta(hours=3, minutes=5, seconds=10))}
    assert boardview.machine_line(snap, 123, info).startswith("mac-a · running (up 3h 5m)   ·   others")
    assert boardview.machine_line(snap, None, info).startswith("mac-a · daemon not running   ·")

    assert boardview.epic_of(snap, "T1") == "E1"
    assert not boardview.holds_takeover(snap, "E1")
    L.priority(a, "E1", "takeover")
    assert boardview.holds_takeover(snapshot.take(a), "E1")


def test_review_rows_use_live_pr_status(world):
    world.backend.add("T1", title="Poll", labels=["repo:OWNER/app", "type:task"])
    a = world.machine("mac-a")
    world.scheduler(a).cycle()
    d = rv.index(a.root)["T1"]
    cid = rv.read_claims(d)[0].id
    L.complete(a, d, "pr-opened", claim_id=cid, pr_url="https://github.com/OWNER/app/pull/9")
    snap = snapshot.take(a)
    assert boardview.review_rows(snap, {}) == [
        ("T1", ("T1", "Poll", "https://github.com/OWNER/app/pull/9", "pending", "?"))]
    rows = boardview.review_rows(snap, {"T1": {"review": "CHANGES_REQUESTED", "checks": "passing"}})
    assert rows[0][1][3:] == ("changes requested", "passing")


def test_poller_flags_and_log_tail(tmp_path):
    assert boardview.poller_flags(tmp_path) == []
    (tmp_path / "poller-state.json").write_text('{"needs_arbitration": ["T9"]}')
    assert boardview.poller_flags(tmp_path) == ["T9"]
    log = tmp_path / "notifications.log"
    log.write_text("2026-09-16T10:00:00+00:00 [info] old line\n")
    tail = boardview.LogTail(log)
    assert tail.read() == []
    with open(log, "a") as f:
        f.write("2026-09-16T10:01:00+00:00 [finished] claude has finished T1. The PR can be found at u\n")
        f.write("2026-09-16T10:02:00+00:00 [needs-worker] I have claimed T2 for completion, who is my worker?\n"
                "  a) claude\n")
        f.write("2026-09-16T10:03:00+00:00 [feed] Roman paused their swarm (mac-a)\n")
        f.write("2026-09-16T10:04:00+00:00 [idle] Still working on T3?\nsecond line\n")
    assert tail.read() == [("finished", "claude has finished T1. The PR can be found at u"),
                           ("idle", "Still working on T3?\nsecond line")]
    assert tail.read() == []
    log.write_text("")
    assert tail.read() == []
    assert boardview.announcement("finished", "x") == "[swarm-board] x"
