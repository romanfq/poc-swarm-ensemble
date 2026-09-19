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
    backend.add("T2", title="Parse", epic_of="E1", blocked_by=["T1", "T0"], labels=["type:task"])
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
    backend.add("T3", title="New", epic_of="E1", labels=["type:task"])
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


# -- plan scope (GH-18) ----------------------------------------------------------------

def test_unlabelled_issues_are_skipped_and_counted(world):
    backend, a, _ = world
    backend.add("U1", title="A thought filed at midnight")
    backend.add("U2", title="Another one", epic_of="E1")
    backend.add("U3", title="Closed and unlabelled", closed=True)
    rep = plan.sync(a)
    assert sorted(rep.imported) == ["E1", "T0", "T1", "T2"]
    assert sorted(rep.skipped) == ["U1", "U2"]                   # open ones only
    assert "2 skipped (no swarm label)" in rep.summary()
    assert not {"U1", "U2", "U3"} & rv.index(a.root).keys()
    rep = plan.sync(a)
    assert not rep.changed and rep.summary() == "no changes, 2 skipped (no swarm label)"


def test_plan_scope_all_imports_unlabelled_issues(world, tmp_path):
    backend, a, _ = world
    backend.add("U1", title="A thought filed at midnight")
    everything = FakeBackend(backend.path, plan_scope="all")
    rep = plan.sync(a, everything)
    assert "U1" in rep.imported and rep.skipped == []


def test_epic_without_a_status_still_imports(world):
    backend, a, _ = world
    assert not backend.get_task(TaskRef("E1")).labels             # epic=True, no labels at all
    assert "E1" in plan.sync(a).imported


def test_unlabelled_dependency_is_reported_not_imported(world):
    backend, a, _ = world
    backend.add("D1", title="Blocker nobody labelled")
    backend.add("T4", title="Needs D1", epic_of="E1", blocked_by=["D1"], labels=["type:task"])
    rep = plan.sync(a)
    assert "T4" in rep.imported and "D1" not in rep.imported
    assert "D1" not in rep.skipped                                # an error, not a quiet skip
    assert any("T4 depends on D1" in e and "backend init --apply" in e for e in rep.errors)
    assert not rv.ledger_ready(a.root, rv.index(a.root)["T4"], timeutil.now(), 900)

    fixes, notes = plan.membership_fixes(a, backend)
    assert [(f.task.ref.key, f.status, f.kind) for f in fixes] == [("D1", "ready", "task")]
    assert "T4" in fixes[0].reason and notes == []
    for f in fixes:
        plan.apply_fix(backend, f)
    rep = plan.sync(a)
    assert rep.imported == ["D1"] and not rep.errors
    backend.set_status(TaskRef("D1"), "done")
    plan.sync(a)
    assert rv.ledger_ready(a.root, rv.index(a.root)["T4"], timeutil.now(), 900)


def test_dependencies_of_unlabelled_dependencies_are_found(world):
    backend, a, _ = world
    backend.add("D2", title="Deeper", closed=True)
    backend.add("D1", title="Blocker", blocked_by=["D2"])
    backend.add("T4", title="Needs D1", epic_of="E1", blocked_by=["D1"], labels=["type:task"])
    fixes, _ = plan.membership_fixes(a, backend)
    got = {f.task.ref.key: (f.status, f.reason) for f in fixes}
    assert got["D1"] == ("ready", "dependency of T4")
    assert got["D2"] == ("done", "dependency of D1")


def test_ledger_tracked_issues_without_labels_are_relabelled(world):
    """A swarm that predates plan_scope: its issues are in the ledger but unlabelled."""
    backend, a, _ = world
    everything = FakeBackend(backend.path, plan_scope="all")
    backend.add("L1", title="Hand-filed, already done", epic_of="E1")
    backend.add("L2", title="Hand-filed, open", epic_of="E1")
    backend.add("L3", title="Hand-filed, claimed", epic_of="E1")
    plan.sync(a, everything)                                       # imported under the old rule
    idx = rv.index(a.root)
    cid = L.claim(a, idx["L1"])
    L.complete(a, idx["L1"], "done", claim_id=cid)
    L.claim(a, idx["L3"])
    fixes, notes = plan.membership_fixes(a, backend)
    got = {f.task.ref.key: (f.status, f.kind) for f in fixes}
    assert got == {"L1": ("done", "task"), "L2": ("ready", "task"), "L3": ("claimed", "task")}
    assert notes == []
    for f in fixes:
        plan.apply_fix(backend, f)
    assert plan.membership_fixes(a, backend) == ([], [])
    assert {"L2", "L3"} <= {t.ref.key for t in backend.all_tasks()}
    assert backend.get_task(TaskRef("L1")).closed                  # done closes it, as set_status always has
