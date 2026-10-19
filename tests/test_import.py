"""`backend import` (GH-124): the dry run, the three passes, idempotency, resume, markers, references."""
import itertools

import pytest

from backends.base import AUTONOMY_TIERS, SWARM_STATUSES, BackendError, TaskRef
from backends.fake import FakeBackend
from backends.github import GitHubBackend
from backends.jira import JiraBackend
from dags import bundle as B
from dags import gh, importer, seed
from fakes import FakeGitHub, FakeJira
from test_backend_contract import GitHubAdapter, JiraAdapter, _Runner
from test_bundle import fake_backend

WRITES = ("create_issue", "set_parent", "add_dependency", "update_body", "add_labels", "close_issue",
          "post_comment", "create_label", "set_status", "set_autonomy", "set_kind")


class Killed(BaseException):
    """The process dying: not a BackendError, so nothing in the importer can catch it."""


class Counting:
    """A backend whose write calls are logged; ``kill_after=N`` dies right after the Nth write has happened."""

    def __init__(self, inner, kill_after=None):
        self.inner, self.kill_after, self.log = inner, kill_after, []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name not in WRITES:
            return attr

        def call(*a, **kw):
            out = attr(*a, **kw)
            self.log.append((name, *[x for x in a if isinstance(x, (str, TaskRef))][:1]))
            if self.kill_after is not None and len(self.log) >= self.kill_after:
                raise Killed()
            return out
        return call


def export(backend, **kw):
    kw.setdefault("plan_scope", backend.plan_scope)
    return B.build(backend.export_records(), source={"backend": backend.name, "project": getattr(backend, "repo", None)},
                   tool_commit=None, short_of=lambda k: backend.short_key(TaskRef(k)),
                   url_of=lambda k: backend.web_url(TaskRef(k)), **kw)


def written(bun, tmp_path, name="b.zip"):
    path = tmp_path / name
    B.write_bundle(bun, path)
    return B.read_bundle(path)


def do_import(bun, target, tmp_path, *, opts=None, kill_after=None, mapname="import-map.json", **kw):
    opts = opts or importer.Options()
    counted = Counting(target, kill_after)
    plan = importer.plan_import(bun, counted, opts)
    assert plan.ok, plan.problems
    mapfile = importer.MapFile(tmp_path / mapname, importer.target_identity(counted))
    stats = importer.Importer(plan, counted, mapfile, opts, echo=lambda t: None, sleep=lambda s: None, **kw).run()
    return counted, mapfile, stats


def norm(bun, strip=False):
    """Records by title, with keys, urls, dates and authors gone and what import adds unwrapped."""
    by_id = {r["id"]: r["title"] for r in bun.records}
    out = {}
    for r in bun.records:
        body = B.strip_footer(r["body"]) if strip else r["body"]
        comments = []
        for c in r["comments"]:
            text = c["body"]
            if strip and text.startswith(B.IMPORTED_TAG):
                text = text.split("\n", 3)[3]
            comments.append(text)
        out[r["title"]] = {
            "body": body.rstrip(), "in_plan": r["in_plan"], "kind": r["kind"], "status": r["status"],
            "autonomy": r["autonomy"], "repo": r["repo"], "state": r["state"],
            "parent": by_id.get(r["parent"]), "depends_on": sorted(by_id[d] for d in r["depends_on"]),
            "labels": sorted(lb for lb in r["labels"] if not lb.startswith(B.ANCHOR_PREFIX)),
            "comments": comments}
    return out


def fake_source(tmp_path, extra=None):
    b = fake_backend(tmp_path, extra)
    b.post_comment(TaskRef("T1"), "<!-- dags-plan: abc123 -->\nplan v1")
    b.reply(TaskRef("T1"), "carol", "looks good, but check the rate limit")
    return b


# -- pairs ----------------------------------------------------------------------------------------------------------

def empty_target(kind, tmp_path):
    if kind == "fake":
        return FakeBackend(tmp_path / "target.yaml")
    if kind == "github":
        fake = FakeGitHub(repo="acme/new")
        fake.labels.update({f"swarm:status:{s}" for s in SWARM_STATUSES})
        fake.labels.update({f"swarm:autonomy:{t}" for t in AUTONOMY_TIERS})
        fake.labels.update({"type:epic", "type:task", "repo:OWNER/app"})
        gh.set_runner(_Runner(fake))
        backend = GitHubBackend("acme/new", cache_seconds=0)
        backend.fake = fake
        return backend
    return JiraBackend(FakeJira(project="ISSUE"), "ISSUE", "https://acme.atlassian.net", cache_seconds=0,
                       recent_path=tmp_path / "recent.json")


def source_for(kind, tmp_path):
    if kind == "fake":
        return fake_source(tmp_path)
    ad = GitHubAdapter(tmp_path) if kind == "github" else JiraAdapter(tmp_path)
    ad.b.post_comment(ad.refs["T1"], "<!-- dags-plan: abc123 -->\nplan v1")
    ad.reply(ad.refs["T1"], "carol", "looks good, but check the rate limit")
    return ad.b


@pytest.fixture(autouse=True)
def _gh_runner_reset():
    yield
    gh.set_runner(None)


PAIRS = [("fake", "fake"), ("github", "fake"), ("fake", "jira"), ("jira", "github"), ("fake", "github")]


@pytest.mark.parametrize("src, dst", PAIRS)
def test_round_trip(src, dst, tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source = source_for(src, tmp_path / "s")
    first = written(export(source), tmp_path)
    target = empty_target(dst, tmp_path / "t")
    do_import(first, target, tmp_path)
    second = export(target)
    assert norm(second, strip=True) == norm(first)
    assert all(B.source_of(r["body"]) for r in second.records)


@pytest.mark.parametrize("src, dst", [("fake", "fake"), ("jira", "github")])
def test_second_hop_does_not_nest(src, dst, tmp_path):
    for d in "stu":
        (tmp_path / d).mkdir()
    first = written(export(source_for(src, tmp_path / "s")), tmp_path)
    mid = empty_target(dst, tmp_path / "t")
    do_import(first, mid, tmp_path)
    middle = written(export(mid), tmp_path, "mid.zip")
    last = empty_target("fake", tmp_path / "u")
    do_import(middle, last, tmp_path, mapname="map2.json")
    final = export(last)
    assert norm(final, strip=True) == norm(middle)
    for r in final.records:
        assert B.source_of(r["body"]) is not None
        assert len(B.FOOTER_RE.findall(r["body"])) == 2               # the source's own footer and ours, no more


# -- idempotency ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dst", ["fake", "github", "jira"])
def test_a_second_run_makes_zero_writes(dst, tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_source(tmp_path / "s")), tmp_path)
    target = empty_target(dst, tmp_path / "t")
    do_import(bun, target, tmp_path)
    before = export(target)
    again, _, stats = do_import(bun, target, tmp_path)
    assert again.log == [] and stats["writes"] == 0
    assert export(target).records == before.records


def snapshot(target):
    if hasattr(getattr(target, "client", None), "settle"):
        target.client.settle()
    return norm(export(target), strip=True), len(target.all_issues())


def count_writes(bun, make_target, tmp_path):
    counted, _, _ = do_import(bun, make_target(), tmp_path, mapname="count-map.json")
    return len(counted.log), snapshot(counted.inner)


@pytest.mark.parametrize("dst", ["fake", "jira"])
def test_killed_at_every_write_then_resumed_gives_the_same_result(dst, tmp_path):
    for d in "s":
        (tmp_path / d).mkdir()
    bun = written(export(fake_source(tmp_path / "s")), tmp_path)
    n = itertools.count()

    def make():
        d = tmp_path / f"t{next(n)}"
        d.mkdir()
        t = empty_target(dst, d)
        if dst == "jira":
            t.client.lag = True                             # search trails the writes
        return t
    total, expected = count_writes(bun, make, tmp_path)
    assert total > 20
    for k in range(1, total):
        target = make()
        mapname = f"map-{k}.json"
        with pytest.raises(Killed):
            do_import(bun, target, tmp_path, kill_after=k, mapname=mapname)
        if dst == "jira":
            target = JiraBackend(target.client, "ISSUE", "https://acme.atlassian.net", cache_seconds=0,
                                 recent_path=target.recent_path)           # a new process
        def settle(_s, t=target):
            if dst == "jira":
                t.client.settle()
        plan = importer.plan_import(bun, target, importer.Options())
        mapfile = importer.MapFile(tmp_path / mapname, importer.target_identity(target))
        importer.Importer(plan, target, mapfile, importer.Options(), echo=lambda t: None, sleep=settle).run()
        assert snapshot(target) == expected, f"killed after write {k}"


def test_a_lost_map_refuses_and_rebuild_map_recovers_it(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_source(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    _, mapfile, _ = do_import(bun, target, tmp_path)
    lost = dict(mapfile.issues)
    (tmp_path / "import-map.json").unlink()
    assert len(importer.anchored_issues(target)) == len(bun.records)       # the CLI refuses to create on this
    plan = importer.plan_import(bun, target, importer.Options())
    fresh = importer.MapFile(tmp_path / "import-map.json", importer.target_identity(target))
    assert importer.rebuild_map(plan, target, fresh, sleep=lambda s: None, echo=lambda t: None) == len(bun.records)
    assert {k: v["target"] for k, v in fresh.issues.items()} == {k: v["target"] for k, v in lost.items()}
    counted = Counting(target)
    importer.Importer(plan, counted, fresh, importer.Options(), echo=lambda t: None, sleep=lambda s: None).run()
    assert [c for c in counted.log if c[0] != "create_label"] == [c for c in counted.log if c[0] not in WRITES] == []


def test_force_new_imports_the_bundle_again_with_its_own_anchor(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_source(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    do_import(bun, target, tmp_path)
    opts = importer.Options(force_new=True)
    plan = importer.plan_import(bun, target, opts, anchored=len(bun.records))
    assert plan.run_id != bun.bundle_id
    (tmp_path / "import-map.json").rename(tmp_path / "old.json")
    mapfile = importer.MapFile(tmp_path / "import-map.json", importer.target_identity(target))
    importer.Importer(plan, target, mapfile, opts, echo=lambda t: None, sleep=lambda s: None).run()
    assert len(target.all_issues()) == 2 * len(bun.records)


def test_a_newer_export_creates_only_the_new_issues_and_reports_the_changed(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    source = fake_backend(tmp_path / "s")
    old = written(export(source), tmp_path, "old.zip")
    target = empty_target("fake", tmp_path / "t")
    do_import(old, target, tmp_path)
    source.add("N1", title="New one", epic_of="E1", labels=["type:task"], created_utc="2026-02-01T00:00:00Z")
    source.update_body(TaskRef("T1"), "changed after the first export")
    new = written(export(source), tmp_path, "new.zip")
    assert new.bundle_id != old.bundle_id
    counted, mapfile, stats = do_import(new, target, tmp_path)
    assert stats["created"] == 1 and stats["changed"] == 1
    assert [c[0] for c in counted.log if c[0] == "create_issue"] == ["create_issue"]
    t1 = next(t for t in target.all_issues() if t.title == "Poll feed")
    assert "changed after" not in t1.body                               # create-only: nothing is edited


def test_an_issue_stuck_in_creating_that_search_cannot_settle_stops_and_names_it(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    with pytest.raises(Killed):
        do_import(bun, target, tmp_path, kill_after=3)                   # the third create ran, its map entry says so
    mapfile = importer.MapFile(tmp_path / "import-map.json", importer.target_identity(target))
    stuck = next(i for i, e in mapfile.issues.items() if e["step"] == "creating")
    reads = iter([[], None])
    real = target.all_issues

    def lagging():                                                       # the first listing misses it, the second does not
        return [] if next(reads) == [] else real()
    target.all_issues = lagging
    plan = importer.plan_import(bun, target, importer.Options())
    with pytest.raises(importer.ImportStop, match="cannot tell whether"):
        importer.Importer(plan, target, mapfile, importer.Options(), echo=lambda t: None, sleep=lambda s: None).run()
    assert stuck in str(mapfile.issues) and mapfile.issues[stuck]["step"] == "creating"


def test_a_rate_limit_flushes_the_map_and_exits_with_the_resume_time(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")

    class Limited(BackendError):
        retry_after = 90

    calls = []
    real = target.create_issue

    def create(*a, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise Limited("too many requests")
        return real(*a, **kw)
    target.create_issue = create
    plan = importer.plan_import(bun, target, importer.Options())
    mapfile = importer.MapFile(tmp_path / "import-map.json", importer.target_identity(target))
    with pytest.raises(importer.ImportStop) as stop:
        importer.Importer(plan, target, mapfile, importer.Options(), echo=lambda t: None, sleep=lambda s: None,
                          clock=lambda: 0).run()
    assert stop.value.code == importer.EXIT_RATE_LIMITED and stop.value.resume_at == "1970-01-01T00:01:30Z"
    assert len(calls) == 3                                               # it did not loop on the failing issue
    saved = importer.MapFile(tmp_path / "import-map.json", importer.target_identity(target))
    assert sum(e.get("target") is not None for e in saved.issues.values()) == 2


def test_two_importers_cannot_share_a_map(tmp_path):
    path = tmp_path / "import-map.json"
    with importer.Lock(path):
        with pytest.raises(importer.ImportRefused, match="another import is running"):
            importer.Lock(path).__enter__()
    with importer.Lock(path):
        pass


# -- the order of the passes ------------------------------------------------------------------------------------------

def test_no_swarm_label_exists_until_every_issue_is_created_and_linked(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    counted, _, _ = do_import(bun, target, tmp_path)
    names = [c[0] for c in counted.log]
    first_label = names.index("add_labels")
    assert {"create_issue", "set_parent", "add_dependency", "post_comment"} >= set(names[:first_label]) - {"create_label"}
    assert names[:first_label].count("create_issue") == len(bun.records)
    assert "create_issue" not in names[first_label:] and "set_parent" not in names[first_label:]
    assert names.index("close_issue") > first_label if "close_issue" in names else True


# -- judging the bundle (the dry run) --------------------------------------------------------------------------------------

def plan_for(bun, target, **kw):
    return importer.plan_import(bun, target, importer.Options(**kw))


def test_a_different_plan_scope_is_refused(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    target.plan_scope = "all"
    assert any("plan_scope" in p for p in plan_for(bun, target).problems)


def test_two_issues_with_one_seed_id_are_refused(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    marker = "<!-- dags-seed: X1 -->"
    src = fake_backend(tmp_path / "s", {"A": {"title": "a", "body": marker}, "B": {"title": "b", "body": marker}})
    bun = written(export(src), tmp_path)
    assert any("same dags-seed id X1" in p for p in plan_for(bun, empty_target("fake", tmp_path / "t")).problems)


def deep_source(tmp_path):
    src = fake_backend(tmp_path)
    src.add("G1", title="Grandchild", epic_of="T1", labels=["type:task"], created_utc="2026-03-01T00:00:00Z")
    return src


def test_a_hierarchy_too_deep_for_the_target_is_refused_and_listed(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(deep_source(tmp_path / "s")), tmp_path)
    target = empty_target("jira", tmp_path / "t")
    plan = plan_for(bun, target)
    assert not plan.ok and "3 levels deep and jira offers 2" in plan.problems[0] and "G1" in plan.problems[0]


def test_flatten_drops_the_containers_in_between(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(deep_source(tmp_path / "s")), tmp_path)
    target = empty_target("jira", tmp_path / "t")
    plan = plan_for(bun, target, flatten=True)
    assert plan.ok and list(plan.dropped) == ["T1"]
    assert any("T1" in n and "not created" in n for n in plan.notes)
    counted, _, _ = do_import(bun, target, tmp_path, opts=importer.Options(flatten=True))
    titles = {t.title: t for t in target.all_issues()}
    assert "Poll feed" not in titles and len(titles) == len(bun.records) - 1
    assert "Part of: Poll feed" in titles["Grandchild"].body
    assert titles["Grandchild"].epic.key == titles["Ingestion"].ref.key


def test_a_closed_blocked_issue_is_imported_open_and_blocked(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    src = fake_backend(tmp_path / "s", {"CB": {"title": "closed but blocked", "closed": True, "status": "blocked",
                                                "labels": ["type:task"]}})
    bun = written(export(src), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    plan = plan_for(bun, target)
    assert plan.kept_open == ["CB"]
    do_import(bun, target, tmp_path)
    t = next(t for t in target.all_issues() if t.title == "closed but blocked")
    assert not t.closed and t.status == "blocked"


def test_hold_imports_open_tasks_blocked_and_counts_what_would_be_ready(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    plain = plan_for(bun, empty_target("fake", tmp_path / "t"))
    assert plain.ready_after == 4                                       # T1, T2, T4, T5
    held = plan_for(bun, empty_target("fake", tmp_path / "t"), hold=True)
    assert held.ready_after == 0
    target = empty_target("fake", tmp_path / "t")
    do_import(bun, target, tmp_path, opts=importer.Options(hold=True))
    by = {t.title: t for t in target.all_issues()}
    assert by["Poll feed"].status == "blocked" and by["Schema"].status == "done"
    assert by["Ingestion"].status is None


def test_a_label_the_target_cannot_hold_is_renamed_and_listed(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    src = fake_backend(tmp_path / "s", {"L": {"title": "l", "labels": ["needs review"]}})
    bun = written(export(src), tmp_path)
    plan = plan_for(bun, empty_target("jira", tmp_path / "t"))
    assert plan.renamed_labels == [("L", "needs review", "needs_review")]


def test_a_github_target_without_the_swarm_labels_asks_for_backend_init(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_backend(tmp_path / "s")), tmp_path)
    target = empty_target("github", tmp_path / "t")
    target.fake.labels.clear()
    assert any("backend init" in p for p in plan_for(bun, target).problems)


def test_the_dry_run_counts_replies_the_import_will_skip(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    bun = written(export(fake_source(tmp_path / "s")), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    plan = importer.plan_import(bun, target, importer.Options(), is_human=lambda a: a == "carol")
    assert plan.skipped_replies == ["T1: 1 human reply since its latest plan/question comment will be skipped by "
                                    "replies(); repost them after the move"]


# -- the swarm's markers ------------------------------------------------------------------------------------------------------

def test_a_seed_marker_survives_and_seed_finds_the_issue(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    src = fake_backend(tmp_path / "s", {"S": {"title": "seeded", "body": "text\n\n<!-- dags-seed: PLAN-1 -->\n"}})
    bun = written(export(src), tmp_path)
    for kind in ("fake", "jira"):
        target = empty_target(kind, tmp_path / "t")
        do_import(bun, target, tmp_path, mapname=f"{kind}.json")
        assert set(seed.seeded(target)) == {"PLAN-1"}
        (tmp_path / "t" / "target.yaml").unlink(missing_ok=True)


def test_plan_and_block_comments_are_carried_unchanged_and_tagged(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    src = fake_source(tmp_path / "s")
    src.post_comment(TaskRef("T2"), "DAGS: worker needs a human decision\n\nwhich?")
    bun = written(export(src), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    do_import(bun, target, tmp_path)
    t1 = next(t for t in target.all_issues() if t.title == "Poll feed")
    comments = target.list_comments(t1.ref)
    assert comments[0].body.startswith("<!-- dags-imported -->\nOriginally by @dags-bot on ")
    assert comments[0].body.endswith("\n\n<!-- dags-plan: abc123 -->\nplan v1")
    t2 = next(t for t in target.all_issues() if t.title == "Parse feed")
    assert "\n\nDAGS: worker needs a human decision\n\nwhich?" in target.list_comments(t2.ref)[0].body
    from dags import work
    assert all(work._MARKER_RE.search(c.body) for c in (comments[0], target.list_comments(t2.ref)[0]))


# -- references ------------------------------------------------------------------------------------------------------------------

FORMS = B.reference_forms(
    [{"id": "org/plan#14", "short": "GH-14", "url": "https://github.com/org/plan/issues/14"},
     {"id": "org/plan#3", "short": "GH-3", "url": "https://github.com/org/plan/issues/3"}],
    {"org/plan#14": ("KAN-9", "https://x.atlassian.net/browse/KAN-9"),
     "org/plan#3": ("KAN-2", "https://x.atlassian.net/browse/KAN-2")}, "org/plan")


@pytest.mark.parametrize("text, expected", [
    ("see #14 and org/plan#3", "see KAN-9 and KAN-2"),
    ("https://github.com/org/plan/issues/14#issuecomment-5", "https://x.atlassian.net/browse/KAN-9#issuecomment-5"),
    ("PR #122 fixes it", "PR #122 fixes it"),                                     # not an issue in the map
    ("#140 and #1400 and x#14", "#140 and #1400 and x#14"),
    ("GH-14, GH-3.", "KAN-9, KAN-2."),
    ("in `code #14` and ```\n#14\n```", "in `code #14` and ```\n#14\n```"),
    ("<!-- note #14 -->", "<!-- note #14 -->"),
    ("Imported from org/plan#14 (https://github.com/org/plan/issues/14)\nreal #14",
     "Imported from org/plan#14 (https://github.com/org/plan/issues/14)\nreal KAN-9"),
    ("Originally by @a on 2026 (https://github.com/org/plan/issues/14#issuecomment-5)\n#3",
     "Originally by @a on 2026 (https://github.com/org/plan/issues/14#issuecomment-5)\nKAN-2"),
])
def test_reference_rewriting(text, expected):
    assert B.rewrite_refs(text, FORMS) == expected


def test_imported_bodies_and_comments_get_their_references_rewritten(tmp_path):
    for d in "st":
        (tmp_path / d).mkdir()
    src = fake_backend(tmp_path / "s", {"R": {"title": "refs", "body": "blocked by T1, see `T1`", "labels": ["type:task"],
                                              "comments": [{"author": "x", "body": "also T0", "created_at": "c1",
                                                            "url": "fake://R/comments/1"}]}})
    bun = written(export(src), tmp_path)
    target = empty_target("fake", tmp_path / "t")
    _, mapfile, stats = do_import(bun, target, tmp_path)
    r = next(t for t in target.all_issues() if t.title == "refs")
    t1, t0 = mapfile.issues["T1"]["target"], mapfile.issues["T0"]["target"]
    assert r.body.startswith(f"blocked by {t1}, see `T1`") and "Imported from R (" in r.body
    assert f"also {t0}" in target.list_comments(r.ref)[0].body
    assert stats["edited"] >= 1
