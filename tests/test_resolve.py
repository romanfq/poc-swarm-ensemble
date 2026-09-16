import resolve as rv
from conftest import LEASE, at
from dags import records as R

HUMANS = {"roman", "jane"}


def test_next_clock_spans_whole_repo(ledger):
    assert rv.next_clock(ledger.root) == 1
    t = ledger.task("T1")
    assert rv.next_clock(ledger.root) == 2
    ledger.claim(t, "mac-a", 41)
    R.write_new(ledger.root / "control" / "mac-b-pause-77.yaml", {"logical_clock": 77})
    assert rv.next_clock(ledger.root) == 78


def test_returning_machine_jumps_forward(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 500)
    # mac-b was offline and last saw clock 3; it must not reuse a low value
    assert rv.next_clock(ledger.root) == 501


def test_lowest_clock_wins_then_machine_id(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-b", 450)
    ledger.claim(t, "mac-a", 451)
    res = rv.resolve(t, at(1), LEASE)
    assert res.winner.id == "mac-b-450" and res.reason == "clock"
    assert res.conflict

    t2 = ledger.task("T2")
    ledger.claim(t2, "mac-b", 460)
    ledger.claim(t2, "mac-a", 460)
    assert rv.resolve(t2, at(1), LEASE).winner.machine == "mac-a"


def test_both_claims_survive_and_every_reader_agrees(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 451)
    ledger.claim(t, "mac-b", 450)
    assert len(rv.read_claims(t)) == 2
    answers = {rv.resolve(t, at(2), LEASE).winner.id for _ in range(5)}
    assert answers == {"mac-b-450"}


def test_withdrawn_claim_never_wins(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10)
    ledger.claim(t, "mac-b", 11)
    ledger.withdraw(t, "mac-a", 10, reason="failed")
    assert rv.resolve(t, at(1), LEASE).winner.id == "mac-b-11"


def test_lease_expiry_is_lazy(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10, minute=0)
    assert rv.resolve(t, at(14), LEASE).winner is not None
    assert rv.resolve(t, at(16), LEASE).winner is None
    ledger.heartbeat(t, "mac-a", 10, minute=12)
    assert rv.resolve(t, at(26), LEASE).winner.id == "mac-a-10"
    assert rv.resolve(t, at(28), LEASE).winner is None
    assert rv.ledger_ready(ledger.root, t, at(28), LEASE)


def test_arbitration_beats_clock(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 451)
    ledger.claim(t, "mac-b", 450)
    ledger.arbitrate(t, "jane", "mac-a-451", 500)
    res = rv.resolve(t, at(1), LEASE, HUMANS)
    assert res.winner.id == "mac-a-451" and res.reason == "arbitration"


def test_arbitration_from_unknown_human_is_ignored(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 451)
    ledger.claim(t, "mac-b", 450)
    ledger.arbitrate(t, "mallory", "mac-a-451", 500)
    assert rv.resolve(t, at(1), LEASE, HUMANS).winner.id == "mac-b-450"


def test_freeze_and_unfreeze(ledger):
    t = ledger.task("T1")
    ledger.arbitrate(t, "roman", "none", 20)
    res = rv.resolve(t, at(0), LEASE, HUMANS)
    assert res.frozen and res.winner is None
    assert not rv.ledger_ready(ledger.root, t, at(0), LEASE, HUMANS)
    assert rv.task_state(t, at(0), LEASE, HUMANS) == "frozen"
    ledger.arbitrate(t, "roman", None, 21, action="withdraw")
    assert not rv.resolve(t, at(0), LEASE, HUMANS).frozen
    assert rv.ledger_ready(ledger.root, t, at(0), LEASE, HUMANS)


def test_dependencies_gate_readiness(ledger):
    a = ledger.task("A")
    b = ledger.task("B", deps=["A"])
    assert rv.ledger_ready(ledger.root, a, at(0), LEASE)
    assert not rv.ledger_ready(ledger.root, b, at(0), LEASE)
    ledger.complete(a, "mac-a", "done", 30)
    assert rv.ledger_ready(ledger.root, b, at(0), LEASE)


def test_unknown_dependency_blocks(ledger):
    b = ledger.task("B", deps=["NOT-IMPORTED"])
    assert not rv.ledger_ready(ledger.root, b, at(0), LEASE)


def test_awaiting_review_releases_quota_but_holds_task(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10)
    ledger.checkpoint(t, claim_id="mac-a-10", worker="claude")
    assert rv.task_state(t, at(1), LEASE) == "in-progress"
    assert rv.quota_room(ledger.root, "mac-a", share=3, default_n=1, now=at(1), lease_s=LEASE) == 0
    ledger.complete(t, "mac-a", "pr-opened", 11, claim="mac-a-10", pr_url="u")
    assert rv.task_state(t, at(1), LEASE) == "awaiting-review"
    assert rv.quota_room(ledger.root, "mac-a", share=3, default_n=1, now=at(1), lease_s=LEASE) == 1
    # even after its heartbeat is long gone the task isn't handed out again
    assert not rv.ledger_ready(ledger.root, t, at(500), LEASE)


def test_reopen_after_changes_requested(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10)
    ledger.complete(t, "mac-a", "pr-opened", 11, claim="mac-a-10", pr_url="u")
    ledger.complete(t, "mac-b", "reopened", 12, review_id="R1")
    assert rv.task_state(t, at(1), LEASE) == "open"
    assert rv.ledger_ready(ledger.root, t, at(1), LEASE)
    # the old claim is no longer live even though it produced a PR
    assert rv.resolve(t, at(1), LEASE).winner is None
    ledger.claim(t, "mac-b", 13)
    ledger.complete(t, "mac-b", "pr-opened", 14, claim="mac-b-13", pr_url="u")
    # a late duplicate reopen for the same review is ignored
    ledger.complete(t, "mac-c", "reopened", 15, review_id="R1")
    assert rv.task_state(t, at(1), LEASE) == "awaiting-review"
    # the successful claim isn't counted as a failure
    assert rv.retry_count(t, at(100), LEASE) == 0


def test_rejected_holds_until_replanned(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10)
    ledger.complete(t, "mac-a", "pr-opened", 11, claim="mac-a-10")
    ledger.complete(t, "mac-a", "rejected", 12)
    assert rv.task_state(t, at(1), LEASE) == "rejected"
    assert not rv.ledger_ready(ledger.root, t, at(1), LEASE)
    ledger.complete(t, "mac-a", "replanned", 13)
    assert rv.ledger_ready(ledger.root, t, at(1), LEASE)


def test_done_is_final(ledger):
    t = ledger.task("T1")
    ledger.complete(t, "mac-a", "pr-opened", 11)
    ledger.complete(t, "mac-a", "done", 12)
    ledger.complete(t, "mac-b", "reopened", 13, review_id="late")
    assert rv.is_done(t)


def test_global_cap_and_share(ledger):
    tasks = [ledger.task(f"T{i}") for i in range(4)]
    ledger.claim(tasks[0], "mac-a", 10)
    ledger.claim(tasks[1], "mac-b", 11)
    ledger.claim(tasks[2], "mac-b", 12)
    kw = dict(default_n=4, now=at(1), lease_s=LEASE)
    assert rv.quota_room(ledger.root, "mac-a", share=3, **kw) == 1   # global: 4 - 3
    assert rv.quota_room(ledger.root, "mac-a", share=1, **kw) == 0   # share exhausted
    R.write_new(ledger.root / "quota" / "roman-20.yaml", {"human": "roman", "n": 10, "logical_clock": 20})
    assert rv.quota_room(ledger.root, "mac-a", share=3, **kw) == 2
    # expired claims stop counting
    assert rv.quota_room(ledger.root, "mac-a", share=9, default_n=4, now=at(60), lease_s=LEASE) == 9


def test_quota_record_from_unknown_human_ignored(ledger):
    R.write_new(ledger.root / "quota" / "x-5.yaml", {"human": "mallory", "n": 99, "logical_clock": 5})
    assert rv.global_quota(ledger.root, 3, HUMANS) == 3


def test_takeover_priority_is_soft(ledger):
    R.write_new(ledger.root / "priority" / "mac-b-E1-takeover-5.yaml",
                {"machine": "mac-b", "epic": "E1", "action": "takeover", "logical_clock": 5})
    tk = rv.active_takeovers(ledger.root)
    cands = [("a", {"epic": "E1"}), ("b", {"epic": "E2"})]
    assert [k for k, _ in rv.order_candidates(cands, "mac-a", tk)] == ["b", "a"]
    assert [k for k, _ in rv.order_candidates(cands, "mac-b", tk)] == ["a", "b"]
    R.write_new(ledger.root / "priority" / "mac-b-E1-release-6.yaml",
                {"machine": "mac-b", "epic": "E1", "action": "release", "logical_clock": 6})
    assert rv.active_takeovers(ledger.root) == {}


def test_retry_count_and_race_history(ledger):
    t = ledger.task("T1")
    ledger.claim(t, "mac-a", 10)
    ledger.claim(t, "mac-b", 11)
    ledger.withdraw(t, "mac-b", 11, reason="lost-race", winner="mac-a-10", wclock=12)
    assert rv.retry_count(t, at(1), LEASE) == 0
    assert rv.retry_count(t, at(30), LEASE) == 1          # mac-a's lease lapsed
    ledger.claim(t, "mac-c", 20)
    ledger.withdraw(t, "mac-c", 20, reason="failed", wclock=21)
    assert rv.retry_count(t, at(30), LEASE) == 2
    assert rv.race_history(t) == {"mac-a|mac-b": 1}


def test_meta_revisions_override(ledger):
    t = ledger.task("T1", deps=["A"])
    R.write_new(t / "meta" / "mac-a-9.yaml", {"dependencies": [], "logical_clock": 9})
    assert rv.read_meta(t)["dependencies"] == []


def test_corrupt_record_does_not_crash(ledger):
    t = ledger.task("T1")
    (t / "claims").mkdir()
    (t / "claims" / "broken-3.yaml").write_text("{{{ not yaml")
    ledger.claim(t, "mac-a", 4)
    assert rv.resolve(t, at(0), LEASE).winner.id == "mac-a-4"
