"""Slice 1 of the Jira backend (GH-123): the tracker-neutral glue that does not need Jira itself."""
from backends.base import BackendError, Task, TaskRef, TrackerHelpers
from dags import gh, plan
from dags.config import Context


def test_ghError_is_a_backend_error():
    assert issubclass(gh.GhError, BackendError)
    assert isinstance(gh.GhError(["x"], 1, "boom"), BackendError)


def test_github_adapter_has_the_tracker_helpers():
    from backends.github import GitHubBackend
    assert isinstance(GitHubBackend("acme/plan"), TrackerHelpers)


class _Stub:
    def __init__(self, backend, humans):
        self.backend_cfg, self.humans = {"backend": backend}, humans

    human_by_github = Context.human_by_github
    human_by_tracker = Context.human_by_tracker


HUMANS = [{"name": "roman", "github": "RomanFQ", "jira": "5b10ac8d82e05b22cc7d4ef5"}]


def test_human_by_tracker_github_is_case_insensitive():
    c = _Stub("github", HUMANS)
    assert c.human_by_tracker("romanfq") == "roman"
    assert c.human_by_tracker("5b10ac8d82e05b22cc7d4ef5") is None
    assert c.human_by_tracker(None) is None


def test_human_by_tracker_jira_matches_account_id_exactly():
    c = _Stub("jira", HUMANS)
    assert c.human_by_tracker("5b10ac8d82e05b22cc7d4ef5") == "roman"
    assert c.human_by_tracker("5B10AC8D82E05B22CC7D4EF5") is None
    assert c.human_by_tracker("romanfq") is None


def _setup(world, blocked_epic=False):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Task T1", epic_of="E1", labels=["repo:OWNER/app", "type:task"])
    world.backend.lagging_search = True        # the search still says ready
    fresh = {"E1": Task(TaskRef("E1"), "Epic", is_epic=True, status="blocked" if blocked_epic else None),
             "T1": Task(TaskRef("T1"), "Task T1", status="ready" if not blocked_epic else None,
                        epic=TaskRef("E1"))}
    world.backend.get_task_fresh = lambda ref: fresh[ref.key]
    return fresh


def test_claim_refused_when_a_fresh_read_shows_the_task_blocked(world):
    fresh = _setup(world)
    fresh["T1"].status = "blocked"
    a = world.machine("mac-a")
    rep = world.scheduler(a).cycle()
    assert rep.claimed == []


def test_claim_refused_when_a_fresh_read_shows_the_epic_blocked(world):
    _setup(world, blocked_epic=True)
    rep = world.scheduler(world.machine("mac-a")).cycle()
    assert rep.claimed == []


def test_claim_goes_ahead_when_the_fresh_read_agrees(world):
    _setup(world)
    rep = world.scheduler(world.machine("mac-a")).cycle()
    assert rep.claimed == ["T1"]


def test_fresh_read_failure_skips_the_claim_and_reports(world):
    _setup(world)

    def boom(ref):
        raise BackendError("jira is down")
    world.backend.get_task_fresh = boom
    rep = world.scheduler(world.machine("mac-a")).cycle()
    assert rep.claimed == [] and any("jira is down" in e for e in rep.errors)


def test_backends_with_consistent_reads_are_not_rechecked(world):
    world.backend.add("T1", title="Task T1", labels=["type:task"])

    def boom(ref):
        raise AssertionError("must not be read")
    world.backend.get_task_fresh = boom
    sched = world.scheduler(world.machine("mac-a"))
    assert sched.fresh_ready("T1", None) is True


def test_adopt_leaves_jira_epics_without_a_status(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.uses_type_labels = False
    world.backend.epics_carry_status = False
    world.backend.plan_scope = "labelled"
    a = world.machine("mac-a")
    # ledger tracks the epic although the tracker does not mark it
    from conftest import sh  # noqa: F401
    fixes, _ = plan.membership_fixes(a, world.backend)
    assert [f for f in fixes if f.task.ref.key == "E1"] == []
