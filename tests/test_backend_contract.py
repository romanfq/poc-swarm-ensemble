"""One suite every IssueBackend adapter must pass (plan §5 'Contract')."""
import pytest

from backends.base import IssueBackend, TaskRef
from fakes import PLAN, FakeGitHub


class FakeAdapter:
    def __init__(self, tmp_path):
        from backends.fake import FakeBackend
        self.b = FakeBackend(tmp_path / "fake-backend.yaml")
        for name, spec in PLAN.items():
            fields = {"title": spec["title"], "body": spec.get("body", ""),
                      "epic": bool(spec.get("epic")), "closed": bool(spec.get("closed"))}
            if spec.get("parent"):
                fields["epic_of"] = spec["parent"]
            if spec.get("deps"):
                fields["blocked_by"] = spec["deps"]
            if spec.get("status"):
                fields["status"] = spec["status"]
            labels = []
            if spec.get("autonomy"):
                labels.append(f"swarm:autonomy:{spec['autonomy']}")
            if spec.get("repo"):
                labels.append(f"repo:{spec['repo']}")
            fields["labels"] = labels
            self.b.add(name, **fields)
        self.refs = {n: TaskRef(n) for n in PLAN}

    def comments(self, ref):
        return self.b.comments(ref)


class GitHubAdapter:
    def __init__(self, tmp_path, issue_types=False):
        from backends.github import GitHubBackend
        from dags import gh
        self.fake = FakeGitHub()
        self.refs = {n: TaskRef(k) for n, k in self.fake.seed(issue_types=issue_types).items()}
        self.fake.labels.update({f"swarm:status:{s}" for s in
                                 ("ready", "claimed", "in-progress", "awaiting-review", "blocked", "done")})
        self.fake.labels.update({"swarm:autonomy:human-must-review", "swarm:autonomy:human-must-scope"})
        gh.set_runner(_Runner(self.fake))
        self.b = GitHubBackend("acme/plan", use_issue_types=issue_types, cache_seconds=0)

    def comments(self, ref):
        return self.fake.comments(ref.key)


class _Runner:
    def __init__(self, handler):
        from conftest import FakeGh
        self.inner = FakeGh(handler)

    def __call__(self, args, **kw):
        return self.inner(args, **kw)


@pytest.fixture(params=["fake", "github", "github-issue-types"])
def adapter(request, tmp_path):
    from dags import gh
    if request.param == "fake":
        yield FakeAdapter(tmp_path)
    else:
        yield GitHubAdapter(tmp_path, issue_types=request.param.endswith("types"))
    gh.set_runner(None)


def keys(refs):
    return sorted(r.key for r in refs)


def test_implements_port(adapter):
    assert isinstance(adapter.b, IssueBackend)


def test_ready_tasks(adapter):
    r = adapter.refs
    # T0 closed, T3 blocked, T4 under a blocked epic, epics never; T2's dependency
    # is left to the ledger (Ch.7.2), so the backend still lists it.
    assert keys(adapter.b.ready_tasks()) == keys([r["T1"], r["T2"], r["T5"]])


def test_get_task_fields(adapter):
    r = adapter.refs
    t = adapter.b.get_task(r["T1"])
    assert t.title == "Poll feed"
    assert "every minute" in t.body
    assert t.autonomy == "auto-pr"
    assert t.repo == "OWNER/app"
    assert t.epic == r["E1"]
    assert not t.is_epic and not t.closed and not t.done
    assert adapter.b.get_task(r["E1"]).is_epic
    assert adapter.b.get_task(r["T2"]).autonomy == "human-must-review"   # default tier
    assert adapter.b.get_task(r["T0"]).done
    assert r["T1"].key in adapter.b.get_task(r["T1"]).spec_markdown()


def test_dependencies_and_epics(adapter):
    r = adapter.refs
    assert adapter.b.dependencies(r["T2"]) == [r["T1"]]
    assert adapter.b.dependencies(r["T1"]) == []
    assert keys(adapter.b.epic_children(r["E1"])) == keys([r["T0"], r["T1"], r["T2"], r["T3"]])
    assert adapter.b.epic_children(r["T1"]) == []


def test_set_status_round_trip(adapter):
    r = adapter.refs
    for status in ("claimed", "in-progress", "awaiting-review"):
        adapter.b.set_status(r["T1"], status)
        assert adapter.b.get_task(r["T1"]).status == status
    adapter.b.set_status(r["T1"], "blocked")
    assert r["T1"] not in adapter.b.ready_tasks()
    adapter.b.set_status(r["T1"], "ready")
    assert r["T1"] in adapter.b.ready_tasks()
    adapter.b.set_status(r["T1"], "done")
    t = adapter.b.get_task(r["T1"])
    assert t.done and t.status == "done"
    assert r["T1"] not in adapter.b.ready_tasks()


def test_blocking_an_epic_halts_its_tasks(adapter):
    r = adapter.refs
    adapter.b.set_status(r["E1"], "blocked")
    assert keys(adapter.b.ready_tasks()) == keys([r["T5"]])


def test_set_autonomy(adapter):
    r = adapter.refs
    adapter.b.set_autonomy(r["T1"], "human-must-review")
    assert adapter.b.get_task(r["T1"]).autonomy == "human-must-review"


def test_post_comment(adapter):
    r = adapter.refs
    adapter.b.post_comment(r["T1"], "## Summary\nhello")
    assert adapter.comments(r["T1"])[-1] == "## Summary\nhello"


def test_coordination_ref_is_stable(adapter):
    r = adapter.refs
    a = adapter.b.coordination_ref(r["T1"])
    assert a and a == adapter.b.coordination_ref(r["T1"])
    assert a != adapter.b.coordination_ref(r["T2"])


def test_short_key(adapter):
    r = adapter.refs
    s = adapter.b.short_key(r["T1"])
    assert s and "/" not in s
