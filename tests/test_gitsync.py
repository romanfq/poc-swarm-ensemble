"""Phase 2: several clones of one bare 'GitHub' remote racing (plan §4, Phase 2)."""
import threading
from datetime import timedelta

import resolve as rv
from conftest import sh
from dags import ledger as L
from dags import records as R
from dags import timeutil
from dags.gitsync import Coord, RepoLock


def _import_task(ctx, key="T1", deps=()):
    d = ctx.root / "tasks" / "E1" / key

    def build():
        return [R.write_new(d / "meta.yaml", {"key": key, "epic": "E1", "dependencies": list(deps),
                                             "logical_clock": rv.next_clock(ctx.root)})]
    ctx.coord.transaction(build, f"import {key}")
    return d


def test_commit_and_push_reach_other_clones(swarm):
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    _import_task(a)
    b.coord.pull()
    assert (b.root / "tasks" / "E1" / "T1" / "meta.yaml").exists()
    assert a.coord.unpushed() == 0


def test_commit_without_changes_is_a_noop(swarm):
    a = swarm.clone("mac-a")
    head = a.coord.head()
    assert a.coord.commit([a.root / "backend.yaml"], "nothing") is False
    assert a.coord.head() == head


def test_concurrent_claims_never_conflict_and_all_clones_agree(swarm):
    machines = [swarm.clone(f"mac-{c}") for c in "abcd"]
    _import_task(machines[0])
    for m in machines:
        m.coord.pull()

    barrier = threading.Barrier(len(machines))
    errors = []

    def race(ctx):
        try:
            barrier.wait()
            L.claim(ctx, ctx.root / "tasks" / "E1" / "T1")
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=race, args=(m,)) for m in machines]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    now = timeutil.now()
    winners = set()
    for m in machines:
        m.coord.pull()
        d = m.root / "tasks" / "E1" / "T1"
        assert len(rv.read_claims(d)) == 4, "every claim survives in the ledger"
        winners.add(rv.resolve(d, now, 900).winner.id)
        status = sh(["git", "status", "--porcelain"], m.root)
        assert status.strip() == "", status
    assert len(winners) == 1


def test_loser_detects_loss_and_withdraws(swarm):
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    d_a = _import_task(a)
    b.coord.pull()
    d_b = b.root / "tasks" / "E1" / "T1"
    # b claims without seeing a's claim first (both offline), then both push
    cid_a = L.claim(a, d_a)
    b_clock_before = rv.next_clock(b.root)
    cid_b = L.claim(b, d_b)          # push triggers pull --rebase; clock computed pre-pull
    assert int(cid_b.rsplit("-", 1)[1]) >= b_clock_before
    b.coord.pull()
    winner = rv.resolve(d_b, timeutil.now(), 900).winner.id
    loser_ctx, loser_cid = (b, cid_b) if winner == cid_a else (a, cid_a)
    loser_dir = loser_ctx.root / "tasks" / "E1" / "T1"
    loser_ctx.coord.pull()
    assert not L.still_mine(loser_ctx, loser_dir, loser_cid)
    L.withdraw(loser_ctx, loser_dir, loser_cid, "lost-race", winner=winner)
    a.coord.pull()
    b.coord.pull()
    for ctx in (a, b):
        d = ctx.root / "tasks" / "E1" / "T1"
        res = rv.resolve(d, timeutil.now(), 900)
        assert res.winner.id == winner and not res.conflict
        assert rv.race_history(d) == {"mac-a|mac-b": 1}


def test_same_meta_imported_twice_resolves_to_remote_copy(swarm):
    """Two machines importing the same task race on one path (add/add).
    The pull falls back to 'remote wins' instead of wedging the clone."""
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    d_b = b.root / "tasks" / "E1" / "T1"
    R.write_new(d_b / "meta.yaml", {"key": "T1", "machine": "mac-b", "logical_clock": 5})
    b.coord.commit([d_b / "meta.yaml"], "b import")
    _import_task(a)          # a pushes first
    b.coord.push()           # b must rebase over a's add/add conflict
    a.coord.pull()
    assert R.load_yaml(d_b / "meta.yaml") == R.load_yaml(a.root / "tasks" / "E1" / "T1" / "meta.yaml")
    assert not (b.root / ".git" / "rebase-merge").exists()
    assert sh(["git", "status", "--porcelain"], b.root).strip() == ""


def test_single_writer_heartbeat_remote_wins_on_conflict(swarm):
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    d_a = _import_task(a)
    cid = L.claim(a, d_a)
    b.coord.pull()
    d_b = b.root / "tasks" / "E1" / "T1"
    # a stale copy of a's heartbeat edited on b (should never happen, but must not wedge)
    R.write_replace(d_b / "heartbeats" / "mac-a.yaml", {"claim_id": cid, "logical_clock": 1})
    b.coord.commit([d_b / "heartbeats" / "mac-a.yaml"], "bogus")
    L.heartbeat(a, [(d_a, cid)])
    b.coord.push()
    b.coord.pull()
    assert R.load_yaml(d_b / "heartbeats" / "mac-a.yaml")["logical_clock"] > 1


def test_heartbeat_keeps_lease_alive_across_clones(swarm):
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    d_a = _import_task(a)
    cid = L.claim(a, d_a)
    L.heartbeat(a, [(d_a, cid)])
    b.coord.pull()
    d_b = b.root / "tasks" / "E1" / "T1"
    later = timeutil.now() + timedelta(minutes=14)
    assert rv.resolve(d_b, later, 900).winner.id == cid
    assert rv.resolve(d_b, later + timedelta(minutes=5), 900).winner is None


def test_checkpoint_refuses_non_owner(swarm):
    import pytest
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    d_a = _import_task(a)
    cid = L.claim(a, d_a)
    L.update_checkpoint(a, d_a, cid, summary="started", append={"tried": ["x"]})
    b.coord.pull()
    d_b = b.root / "tasks" / "E1" / "T1"
    with pytest.raises(L.LostClaim):
        L.update_checkpoint(b, d_b, "mac-b-999", summary="hijack")
    cp = L.read_checkpoint(d_b)
    assert cp["summary"] == "started" and cp["tried"] == ["x"]


def test_repo_lock_is_reentrant_and_exclusive(tmp_path):
    lock = RepoLock(tmp_path / "git.lock")
    order = []
    with lock:
        with lock:
            order.append("outer")

        def other():
            with RepoLock(tmp_path / "git.lock"):
                order.append("other")
        t = threading.Thread(target=other)
        t.start()
        t.join(0.2)
        assert order == ["outer"], "other thread must wait"
    t.join(2)
    assert order == ["outer", "other"]


def test_no_remote_is_local_only(tmp_path):
    sh(["git", "init", "-q", "-b", "main"], tmp_path)
    c = Coord(tmp_path)
    (tmp_path / "f.yaml").write_text("a: 1\n")
    c.pull()
    assert c.commit([tmp_path / "f.yaml"], "x")
    c.push()
    assert c.unpushed() == 0


def test_control_and_quota_records(swarm):
    a, b = swarm.clone("mac-a"), swarm.clone("jane-mac", human="jane")
    L.control(a, "pause")
    L.control(a, "throttle", quota_share=1)
    L.set_quota(b, 5, reason="more budget")
    a.coord.pull()
    st = L.machine_control(a.root, "mac-a")
    assert st["paused"] and st["quota_share"] == 1
    assert rv.global_quota(a.root, 3, a.human_names) == 5
    L.control(a, "resume")
    assert not L.machine_control(a.root, "mac-a")["paused"]
    assert b.coord.author_email(next((b.root / "quota").glob("jane-*.yaml"))) == "t@example.com"
