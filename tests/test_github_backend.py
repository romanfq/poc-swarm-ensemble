import pytest

from backends.base import TaskRef
from backends.github import GitHubBackend
from conftest import FakeGh
from dags import gh
from fakes import FakeGitHub


@pytest.fixture
def gh_fake():
    fake = FakeGitHub(page_size=2)
    refs = fake.seed()
    runner = FakeGh(fake)
    gh.set_runner(runner)
    yield fake, refs, runner
    gh.set_runner(None)


def test_rejects_placeholder_repo():
    with pytest.raises(ValueError):
        GitHubBackend("OWNER/matchwire-swarm")


def test_paginates_and_caches(gh_fake):
    fake, refs, runner = gh_fake
    b = GitHubBackend("acme/plan", cache_seconds=60)
    assert len(b.all_tasks()) == 8
    pages = [c for c in runner.calls if c["args"][:2] == ["api", "graphql"]]
    assert len(pages) == 4
    b.ready_tasks()
    b.get_task(TaskRef(refs["T1"]))
    assert len([c for c in runner.calls if c["args"][:2] == ["api", "graphql"]]) == 4


def test_parse_ref_and_short_key(gh_fake):
    b = GitHubBackend("acme/plan")
    assert b.parse_ref("#7") == TaskRef("acme/plan#7")
    assert b.parse_ref("GH-7") == TaskRef("acme/plan#7")
    assert b.parse_ref("other/repo#3") == TaskRef("other/repo#3")
    assert b.short_key(TaskRef("acme/plan#7")) == "GH-7"
    assert b.short_key(TaskRef("other/repo#3")) == "repo-3"
    with pytest.raises(ValueError):
        b.parse_ref("nope")


def test_cross_repo_dependency_uses_single_issue_query(gh_fake):
    fake, refs, runner = gh_fake
    b = GitHubBackend("acme/plan", cache_seconds=60)
    with pytest.raises(KeyError):
        b.get_task(TaskRef("acme/plan#99"))
    assert "number=99" in runner.calls[-1]["args"]


def test_set_status_swaps_only_status_labels(gh_fake):
    fake, refs, runner = gh_fake
    fake.labels.add("swarm:status:claimed")
    b = GitHubBackend("acme/plan", cache_seconds=60)
    ref = TaskRef(refs["T3"])                      # currently swarm:status:blocked
    b.set_status(ref, "claimed")
    edit = [c["args"] for c in runner.calls if c["args"][:2] == ["issue", "edit"]][-1]
    assert edit == ["issue", "edit", refs["T3"].split("#")[1], "--repo", "acme/plan",
                    "--add-label", "swarm:status:claimed", "--remove-label", "swarm:status:blocked"]
    labels = fake.issues[int(refs["T3"].split("#")[1])]["labels"]
    assert "swarm:status:claimed" in labels and "swarm:status:blocked" not in labels
    assert "type:task" in labels
    calls = len(runner.calls)
    b.set_status(ref, "claimed")                   # already there: no edit call
    assert not [c for c in runner.calls[calls:] if c["args"][:2] == ["issue", "edit"]]


def test_missing_label_points_at_backend_init(gh_fake):
    b = GitHubBackend("acme/plan", cache_seconds=0)
    with pytest.raises(gh.GhError) as exc:
        b.set_status(TaskRef(gh_fake[1]["T1"]), "in-progress")
    assert "backend init" in str(exc.value)


def test_done_closes_issue(gh_fake):
    fake, refs, runner = gh_fake
    fake.labels.add("swarm:status:done")
    b = GitHubBackend("acme/plan", cache_seconds=0)
    b.set_status(TaskRef(refs["T1"]), "done")
    assert fake.issues[int(refs["T1"].split("#")[1])]["state"] == "CLOSED"


def test_epic_detection_without_labels(gh_fake):
    fake, refs, runner = gh_fake
    for i in fake.issues.values():
        i["labels"] = [x for x in i["labels"] if not x.startswith("type:")]
    b = GitHubBackend("acme/plan", cache_seconds=0)
    assert b.get_task(TaskRef(refs["E1"])).is_epic          # has sub-issues
    assert not b.get_task(TaskRef(refs["T1"])).is_epic


def test_init_commands_cover_all_labels(gh_fake):
    b = GitHubBackend("acme/plan")
    cmds = b.init_commands(["OWNER/app"])
    names = [c[2] for c in cmds]
    assert "swarm:status:awaiting-review" in names
    assert "swarm:autonomy:human-must-scope" in names
    assert "type:epic" in names and "repo:OWNER/app" in names
    assert all("--force" in c for c in cmds)
    assert "type:epic" not in [c[2] for c in GitHubBackend("acme/plan", use_issue_types=True).init_commands([])]


def test_graphql_errors_surface(gh_fake):
    fake, refs, runner = gh_fake
    runner.handler = lambda a, e, i: '{"errors": [{"message": "Field \'blockedBy\' doesn\'t exist"}]}'
    with pytest.raises(gh.GhError) as exc:
        GitHubBackend("acme/plan").all_tasks()
    assert "blockedBy" in str(exc.value)
