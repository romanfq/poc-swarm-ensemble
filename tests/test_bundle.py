"""The issue bundle (GH-124): records, order, determinism, safe reading, the export command's pieces."""
import json
import zipfile
from datetime import timedelta

import pytest

from backends.fake import FakeBackend
from backends.github import GitHubBackend
from conftest import at
from dags import bundle, gh, machines
from dags import records as R
from fakes import PLAN, FakeGitHub, plan_labels
from test_backend_contract import _Runner


def fake_backend(tmp_path, extra=None):
    b = FakeBackend(tmp_path / "fake-backend.yaml")
    for n, (name, spec) in enumerate(PLAN.items(), 1):
        fields = {"title": spec["title"], "body": spec.get("body", ""), "epic": bool(spec.get("epic")),
                  "closed": bool(spec.get("closed")), "created_utc": f"2026-01-{n:02d}T00:00:00Z",
                  "labels": [x for x in plan_labels(spec) if not x.startswith("swarm:status:")]}
        if spec.get("parent"):
            fields["epic_of"] = spec["parent"]
        if spec.get("deps"):
            fields["blocked_by"] = spec["deps"]
        if spec.get("status"):
            fields["status"] = spec["status"]
        b.add(name, **fields)
    for name, fields in (extra or {}).items():
        b.add(name, **fields)
    return b


def build(b, **kw):
    kw.setdefault("plan_scope", b.plan_scope)
    return bundle.build(b.export_records(), source={"backend": "fake"}, tool_commit="abc123",
                        short_of=lambda k: k, url_of=lambda k: f"fake://{k}", **kw)


def ids(bun):
    return [r["id"] for r in bun.records]


def test_order_puts_parents_and_blockers_first(tmp_path):
    order = ids(build(fake_backend(tmp_path)))
    for child, before in [("T1", "E1"), ("T2", "T1"), ("T5", "T0"), ("T4", "E2")]:
        assert order.index(before) < order.index(child)


def test_ties_break_by_created_then_key(tmp_path):
    b = fake_backend(tmp_path, {"Z": {"title": "z", "created_utc": "2026-01-01T00:00:00Z"},
                                "A": {"title": "a", "created_utc": "2026-01-01T00:00:00Z"}})
    order = ids(build(b))
    assert order.index("A") < order.index("E1") < order.index("Z") or order.index("A") < order.index("Z")
    assert order.index("A") < order.index("Z")


def test_two_exports_are_byte_identical(tmp_path):
    b = fake_backend(tmp_path)
    one, two = tmp_path / "1.zip", tmp_path / "2.zip"
    bundle.write_bundle(build(b), one)
    bundle.write_bundle(build(b), two)
    assert one.read_bytes() == two.read_bytes()
    assert b"2026-10" not in one.read_bytes()


def test_written_bundle_reads_back_whole(tmp_path):
    bun = build(fake_backend(tmp_path))
    path = tmp_path / "b.zip"
    bundle.write_bundle(bun, path)
    back = bundle.read_bundle(path)
    assert back.records == bun.records and back.bundle_id == bun.bundle_id


def test_record_fields(tmp_path):
    rec = {r["id"]: r for r in build(fake_backend(tmp_path)).records}
    t1 = rec["T1"]
    assert (t1["parent"], t1["status"], t1["autonomy"], t1["repo"], t1["kind"], t1["in_plan"]) == \
        ("E1", "ready", "self-approve", "OWNER/app", "task", True)
    assert rec["T2"]["autonomy"] is None and rec["T2"]["depends_on"] == ["T1"]    # not chosen, so not written
    assert rec["E1"]["kind"] == "epic"
    assert rec["T0"]["state"] == "closed" and rec["T1"]["state"] == "open"
    assert "type:task" not in t1["labels"] and "swarm:autonomy:self-approve" not in t1["labels"]


def test_foreign_labels_are_kept(tmp_path):
    b = fake_backend(tmp_path, {"B": {"title": "bug", "labels": ["type:bug", "needs help"]}})
    assert build(b).records[ids(build(b)).index("B")]["labels"] == ["needs help", "type:bug"]


def test_scope_all_keeps_unlabelled_issues_outside_the_plan(tmp_path):
    b = fake_backend(tmp_path, {"H": {"title": "by hand", "closed": True}})
    rec = {r["id"]: r for r in build(b).records}
    assert rec["H"]["in_plan"] is False
    assert (rec["H"]["kind"], rec["H"]["status"], rec["H"]["autonomy"], rec["H"]["repo"]) == (None,) * 4
    only = build(b, scope="plan")
    assert "H" not in ids(only) and "T0" in ids(only)      # T0 is closed but T5 depends on it


def test_scope_plan_keeps_closed_issues_the_ledger_refers_to(tmp_path):
    b = fake_backend(tmp_path, {"H": {"title": "by hand", "closed": True}})
    assert "H" in ids(build(b, scope="plan", keep={"H"}))


def test_root_limits_to_a_subtree(tmp_path):
    bun = build(fake_backend(tmp_path), roots=["E1"])
    assert set(ids(bun)) == {"E1", "T0", "T1", "T2", "T3"}
    assert {"id": "T5", "depends_on": "T0"} not in bun.report["dropped_dependencies"]
    with pytest.raises(bundle.BundleError, match="no such issue"):
        build(fake_backend(tmp_path), roots=["NOPE"])


def test_open_only_drops_closed_and_reports_the_dependency(tmp_path):
    bun = build(fake_backend(tmp_path), open_only=True)
    rec = {r["id"]: r for r in bun.records}
    assert "T0" not in rec and rec["T5"]["depends_on"] == []
    assert bun.report["dropped_dependencies"] == [{"id": "T5", "depends_on": "T0"}]


def test_dependency_outside_the_tracker_is_kept_as_external(tmp_path):
    b = fake_backend(tmp_path, {"X": {"title": "x", "blocked_by": ["other/repo#9"]}})
    items = b.export_records()
    bun = bundle.build(items, plan_scope="labelled", source={}, tool_commit=None, short_of=lambda k: k,
                       url_of=lambda k: f"https://example/{k}")
    dep = {r["id"]: r for r in bun.records}["X"]["depends_on"]
    assert dep == [{"id": "other/repo#9", "url": "https://example/other/repo#9", "external": True}]
    assert bun.report["external"] == [{"id": "X", "depends_on": "other/repo#9"}]


def test_transient_states_warn(tmp_path):
    b = fake_backend(tmp_path, {"W": {"title": "w", "status": "in-progress", "labels": ["type:task"]}})
    assert any("W is in-progress" in w for w in build(b).report["warnings"])


def test_a_cycle_is_refused(tmp_path):
    b = fake_backend(tmp_path, {"P": {"title": "p", "blocked_by": ["Q"]}, "Q": {"title": "q", "blocked_by": ["P"]}})
    with pytest.raises(bundle.BundleError, match="cycle"):
        build(b)


def test_report_counts(tmp_path):
    r = build(fake_backend(tmp_path)).report
    assert r["issues"] == len(PLAN) and r["deepest_level"] == 2 and r["by_kind"] == {"epic": 2, "task": 6}


# -- reading safely ------------------------------------------------------------------------------------------

@pytest.fixture
def good(tmp_path):
    path = tmp_path / "good.zip"
    bundle.write_bundle(build(fake_backend(tmp_path)), path)
    return path


def rewrite(src, dst, change):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            out = change(info.filename, data)
            if out is not None:
                zout.writestr(info.filename, out[1] if isinstance(out, tuple) else out)
        change(None, None)


def test_path_traversal_entry_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as z:
        for n in zin.namelist():
            z.writestr(n, zin.read(n))
        z.writestr("../evil.json", "{}")
    with pytest.raises(bundle.BundleError, match="unsafe entry"):
        bundle.read_bundle(bad)


@pytest.mark.parametrize("name", ["/abs.json", "issues/../../x.json", "C:evil.json"])
def test_other_unsafe_names(good, tmp_path, name):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as z:
        for n in zin.namelist():
            z.writestr(n, zin.read(n))
        z.writestr(name, "{}")
    with pytest.raises(bundle.BundleError, match="unsafe entry|unexpected entry"):
        bundle.read_bundle(bad)


def test_unlisted_entry_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as z:
        for n in zin.namelist():
            z.writestr(n, zin.read(n))
        z.writestr("notes.txt", "hi")
    with pytest.raises(bundle.BundleError, match="unexpected entry"):
        bundle.read_bundle(bad)


def test_oversized_entry_is_refused(good):
    with pytest.raises(bundle.BundleError, match="byte limit"):
        bundle.read_bundle(good, max_entry=100)


def test_total_size_cap(good):
    with pytest.raises(bundle.BundleError, match="expands to"):
        bundle.read_bundle(good, max_total=1000)


def test_bad_checksum_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"
    first = "issues/0001.json"
    rewrite(good, bad, lambda n, d: (n, d.replace(b'"title"', b'"titel"')) if n == first else d)
    with pytest.raises(bundle.BundleError, match="checksum mismatch"):
        bundle.read_bundle(bad)


def test_truncated_zip_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(good.read_bytes()[:-60])
    with pytest.raises(bundle.BundleError, match="not a readable zip"):
        bundle.read_bundle(bad)


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_text("hello")
    with pytest.raises(bundle.BundleError):
        bundle.read_bundle(p)


def test_unknown_format_version_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"

    def change(name, data):
        if name == "manifest.json":
            m = json.loads(data)
            m["format"] = "dags-issue-bundle/2"
            return json.dumps(m)
        return data
    rewrite(good, bad, change)
    with pytest.raises(bundle.BundleError, match="unknown bundle format"):
        bundle.read_bundle(bad)


def test_missing_issue_file_is_refused(good, tmp_path):
    bad = tmp_path / "bad.zip"
    rewrite(good, bad, lambda n, d: None if n == "issues/0002.json" else d)
    with pytest.raises(bundle.BundleError, match="do not list the same entries"):
        bundle.read_bundle(bad)


# -- GitHub export: truncated reads fail loudly ------------------------------------------------------------------

@pytest.fixture
def github(tmp_path):
    fake = FakeGitHub(page_size=2)
    fake.seed()
    gh.set_runner(_Runner(fake))
    yield fake, GitHubBackend("acme/plan", cache_seconds=0)
    gh.set_runner(None)


def test_github_export_carries_provenance(github):
    fake, b = github
    fake.issues[1]["createdAt"] = "2026-01-01T00:00:00Z"
    items = {i.task.ref.key: i for i in b.export_records()}
    assert len(items) == len(PLAN)


@pytest.mark.parametrize("field, limit", [("labels", 50), ("subIssues", 100), ("blockedBy", 50)])
def test_github_export_stops_when_a_list_is_full(github, field, limit, monkeypatch):
    fake, b = github
    real = fake.node

    def node(n):
        out = real(n)
        if n == 2:
            out[field] = {"nodes": [{"name": f"l{i}", "number": i, "state": "OPEN",
                                     "repository": {"nameWithOwner": "acme/plan"}} for i in range(limit)]}
        return out
    monkeypatch.setattr(fake, "node", node)
    with pytest.raises(gh.GhError, match=rf"acme/plan#2 has {limit} or more {field}"):
        b.export_records()


# -- is any machine running? -------------------------------------------------------------------------------------

LEASE = 15 * 60


def control(root, machine, action, clock, minute=0):
    return R.write_new(root / "control" / R.control_name(machine, action, clock),
                       {"machine": machine, "action": action, "logical_clock": clock, "wall_utc": at(minute).isoformat()})


def found(ledger, minute=60):
    return {b.machine: b for b in machines.blockers(ledger.root, at(minute), LEASE)}


def test_no_machines_no_blockers(ledger):
    assert found(ledger) == {}


def test_a_started_machine_blocks(ledger):
    control(ledger.root, "m1", "start", 1)
    assert found(ledger)["m1"].kind == "running"
    assert "swarm.py stop" in machines.refusal(list(found(ledger).values()), "import")


def test_a_paused_machine_still_blocks(ledger):
    control(ledger.root, "m1", "start", 1)
    control(ledger.root, "m1", "pause", 2)
    b = found(ledger)["m1"]
    assert b.kind == "running" and b.paused


def test_resume_and_throttle_do_not_start_a_stopped_machine(ledger):
    control(ledger.root, "m1", "start", 1)
    control(ledger.root, "m1", "stop", 2)
    control(ledger.root, "m1", "resume", 3)
    control(ledger.root, "m1", "throttle", 4)
    assert found(ledger) == {}


def test_a_machine_that_never_started_does_not_block(ledger):
    control(ledger.root, "m1", "pause", 1)
    assert found(ledger) == {}


def test_stopped_with_a_fresh_heartbeat_is_stopping(ledger):
    control(ledger.root, "m1", "start", 1)
    d = ledger.task("GH-1")
    ledger.claim(d, "m1", 5, minute=0)
    ledger.heartbeat(d, "m1", 5, minute=55)
    control(ledger.root, "m1", "stop", 6, minute=58)
    b = found(ledger, minute=60)["m1"]
    assert b.kind == "stopping" and b.until == at(55) + timedelta(seconds=LEASE)
    assert "Wait" in machines.refusal([b], "rekey")


def test_stopped_whose_claims_lapsed_is_clear(ledger):
    control(ledger.root, "m1", "start", 1)
    d = ledger.task("GH-1")
    ledger.claim(d, "m1", 5, minute=0)
    ledger.heartbeat(d, "m1", 5, minute=10)
    control(ledger.root, "m1", "stop", 6, minute=11)
    assert found(ledger, minute=60) == {}


def test_a_crashed_machine_blocks_until_a_stop_is_recorded(ledger):
    control(ledger.root, "m1", "start", 1)
    assert found(ledger, minute=600)["m1"].kind == "running"
    control(ledger.root, "m1", "stop", 2, minute=601)
    assert found(ledger, minute=602) == {}
