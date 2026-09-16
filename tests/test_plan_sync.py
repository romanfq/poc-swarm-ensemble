import pytest

import resolve as rv
from backends.base import TaskRef
from backends.fake import FakeBackend
from dags import ledger as L
from dags import plan
from dags import records as R
from dags import timeutil


@pytest.fixture
def world(swarm, tmp_path):
    backend = FakeBackend(tmp_path / "shared-backend.yaml")
    backend.add("E1", title="Ingestion", epic=True)
    backend.add("T0", title="Schema", epic_of="E1", closed=True, status="done")
    backend.add("T1", title="Poll", epic_of="E1", labels=["swarm:autonomy:auto-pr", "repo:OWNER/app"])
    backend.add("T2", title="Parse", epic_of="E1", blocked_by=["T1", "T0"])
    backend.add("T9", title="Old closed", closed=True)
    a, b = swarm.clone("mac-a"), swarm.clone("mac-b")
    a.set_backend(backend)
    b.set_backend(backend)
    return backend, a, b


def test_import_creates_meta_and_done_records(world):
    backend, a, _ = world
    rep = plan.sync(a)
    assert sorted(rep.imported) == ["E1", "T0", "T1", "T2"]      # T9: closed, nobody depends on it
    assert rep.done_imported == ["T0"]
    idx = rv.index(a.root)
    assert idx["T1"] == a.root / "tasks" / "E1" / "T1"
    assert idx["E1"] == a.root / "tasks" / "E1" / "_epic"
    meta = rv.read_meta(idx["T1"])
    assert meta["repo"] == "OWNER/app" and meta["autonomy"] == "auto-pr" and meta["short"] == "T1"
    assert rv.read_meta(idx["T2"])["repo"] == "OWNER/app"          # default_repo
    assert rv.read_meta(idx["T2"])["dependencies"] == ["T1", "T0"]
    now = timeutil.now()
    assert rv.ledger_ready(a.root, idx["T1"], now, 900)
    assert not rv.ledger_ready(a.root, idx["T2"], now, 900)       # T1 not done yet
    assert not rv.ledger_ready(a.root, idx["E1"], now, 900)       # epics are never claimed


def test_sync_is_idempotent_and_shared(world):
    backend, a, b = world
    plan.sync(a)
    head = a.coord.head()
    rep = plan.sync(a)
    assert not rep.changed and a.coord.head() == head
    rep_b = plan.sync(b)                  # b pulls a's import first
    assert not rep_b.changed


def test_concurrent_first_import_does_not_wedge(world):
    backend, a, b = world
    plan.sync(a, push=False)
    plan.sync(b)                          # b pushes first
    a.coord.push()                        # a's identical-path import rebases (remote wins)
    a.coord.pull()
    assert rv.index(a.root).keys() == rv.index(b.root).keys()


def test_changes_become_revisions(world):
    backend, a, _ = world
    plan.sync(a)
    backend.set_autonomy(TaskRef("T2"), "human-must-scope")
    backend.add("T3", title="New", epic_of="E1")
    rep = plan.sync(a)
    assert rep.imported == ["T3"] and rep.revised == ["T2"]
    d = rv.index(a.root)["T2"]
    assert R.load_yaml(d / "meta.yaml")["autonomy"] == "human-must-review"   # meta.yaml immutable
    assert rv.read_meta(d)["autonomy"] == "human-must-scope"


def test_rejected_then_replanned(world):
    backend, a, _ = world
    plan.sync(a)
    d = rv.index(a.root)["T1"]
    cid = L.claim(a, d)
    L.complete(a, d, "pr-opened", claim_id=cid, pr_url="https://github.com/OWNER/app/pull/1")
    L.complete(a, d, "rejected", pr_url="https://github.com/OWNER/app/pull/1")
    backend.set_status(TaskRef("T1"), "blocked")
    assert not plan.sync(a).replanned
    backend.set_status(TaskRef("T1"), "ready")
    assert plan.sync(a).replanned == ["T1"]
    assert rv.ledger_ready(a.root, d, timeutil.now(), 900)


def test_manual_close_imports_done_and_unblocks(world):
    backend, a, _ = world
    plan.sync(a)
    backend.set_status(TaskRef("T1"), "done")
    assert plan.sync(a).done_imported == ["T1"]
    assert rv.ledger_ready(a.root, rv.index(a.root)["T2"], timeutil.now(), 900)


def test_stale_backend_status_is_reset(world):
    backend, a, _ = world
    plan.sync(a)
    backend.set_status(TaskRef("T1"), "in-progress")     # label left behind by a dead machine
    rep = plan.sync(a)
    assert rep.status_fixed == ["T1"]
    assert backend.get_task(TaskRef("T1")).status == "ready"


def test_live_claim_keeps_backend_status(world):
    backend, a, _ = world
    plan.sync(a)
    L.claim(a, rv.index(a.root)["T1"])
    backend.set_status(TaskRef("T1"), "claimed")
    assert plan.sync(a).status_fixed == []


def test_repeated_failure_downgrades_autonomy_once(world):
    backend, a, _ = world
    plan.sync(a)
    d = rv.index(a.root)["T1"]
    for _ in range(3):
        cid = L.claim(a, d)
        L.withdraw(a, d, cid, "worker-failed")
    rep = plan.sync(a)
    assert rep.downgraded == ["T1"]
    assert backend.get_task(TaskRef("T1")).autonomy == "human-must-review"
    assert rv.read_meta(d)["autonomy"] == "human-must-review" and rv.read_meta(d)["downgrades"] == 1
    assert "3 failed attempts" in backend.comments(TaskRef("T1"))[-1]
    assert plan.sync(a).downgraded == []                 # no second downgrade for the same failures


def test_lookup_by_short_or_dir(world):
    backend, a, _ = world
    plan.sync(a)
    assert rv.lookup(a.root, "t1") == rv.index(a.root)["T1"]
    assert rv.label(rv.index(a.root)["T1"]) == "T1"
    assert a.task_dir_for("T1") == rv.index(a.root)["T1"]
