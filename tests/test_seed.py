"""Plan seeding (`swarm.py backend seed`, Phase 9)."""
from pathlib import Path

import pytest

from backends.github import GitHubBackend
from conftest import FakeGh
from dags import gh, seed
from fakes import FakeGitHub

REPOS = ["acme/api", "acme/web"]
ROOT = Path(__file__).resolve().parent.parent

PLAN_YAML = """
repo: acme/plan
status: ready
epics:
  - id: BE
    title: Backend
    body: Backend work.
  - id: FE
    title: Frontend
tasks:
  - id: S1
    epic: BE
    title: Scaffold
    repo: acme/api
    body: Make a skeleton.
  - id: A
    epic: BE
    title: Feature A
    repo: acme/api
    autonomy: auto-pr
    depends_on: [S1]
  - id: B
    epic: FE
    title: Page
    repo: acme/web
    autonomy: human-must-scope
    depends_on: [A, S1]
"""


@pytest.fixture
def world(tmp_path):
    fake = FakeGitHub(repo="acme/plan", page_size=2)
    runner = FakeGh(fake)
    gh.set_runner(runner)
    path = tmp_path / "plan.yaml"
    path.write_text(PLAN_YAML)
    yield fake, runner, GitHubBackend("acme/plan", cache_seconds=0), path
    gh.set_runner(None)


def by_marker(fake):
    out = {}
    for n, i in fake.issues.items():
        m = seed.MARKER_RE.search(i["body"])
        if m:
            out[m.group(1)] = n
    return out


def writes(runner):
    return [c["args"][:2] for c in runner.calls
            if c["args"][:2] not in (["api", "graphql"], ["label", "list"])]


def test_dry_run_writes_nothing(world):
    fake, runner, b, path = world
    d = seed.diff(seed.load(path), b, REPOS)
    assert writes(runner) == []
    assert d.count("create") == 5 and d.count("parent") == 3 and d.count("depends") == 3
    assert d.count("label") == len(b.required_labels(REPOS))
    lines = d.lines(b.short_key)
    assert any(line.startswith("create  task A: Feature A") and "swarm:autonomy:auto-pr" in line
               for line in lines)
    assert "depends B (new) blocked by A (new)" in lines


def test_apply_creates_everything_in_order(world):
    fake, runner, b, path = world
    plan = seed.load(path)
    refs = seed.apply(seed.diff(plan, b, REPOS), b, echo=lambda _: None)
    ids = by_marker(fake)
    assert set(ids) == {"BE", "FE", "S1", "A", "B"}
    assert list(ids) == ["BE", "FE", "S1", "A", "B"]          # epics, then dependency order
    assert refs["A"].key == f"acme/plan#{ids['A']}"
    a, bb, be = fake.issues[ids["A"]], fake.issues[ids["B"]], fake.issues[ids["BE"]]
    assert set(a["labels"]) == {"swarm:status:ready", "swarm:autonomy:auto-pr", "repo:acme/api", "type:task"}
    assert "swarm:autonomy:human-must-review" in fake.issues[ids["S1"]]["labels"]   # default tier
    assert be["labels"] == ["type:epic"] and be["body"].startswith("Backend work.")
    assert a["parent"] == ids["BE"] and bb["parent"] == ids["FE"]
    assert sorted(bb["blockedBy"]) == sorted([ids["A"], ids["S1"]])
    assert seed.verify(plan, b, REPOS) == []

    # the backend now reads it back the way plan sync will
    task = b.get_task(refs["B"])
    assert task.autonomy == "human-must-scope" and task.repo == "acme/web"
    assert task.epic == refs["FE"] and set(task.dependencies) == {refs["A"], refs["S1"]}
    assert [r.key for r in b.ready_tasks()] == [refs["S1"].key, refs["A"].key, refs["B"].key]


def test_rerun_is_a_no_op(world):
    fake, runner, b, path = world
    plan = seed.load(path)
    seed.apply(seed.diff(plan, b, REPOS), b, echo=lambda _: None)
    before = len(runner.calls)
    d = seed.diff(plan, b, REPOS)
    assert d.empty and d.notes == []
    assert len(fake.issues) == 5
    assert all(c["args"][:2] in (["api", "graphql"], ["label", "list"]) for c in runner.calls[before:])


def test_resumes_after_a_partial_run(world):
    fake, runner, b, path = world
    plan = seed.load(path)
    handler = runner.handler
    state = {"edits": 0}

    def flaky(args, env, input):
        if args[:2] == ["issue", "edit"]:
            state["edits"] += 1
            if state["edits"] == 2:
                return ("", 1, "HTTP 502")
        return handler(args, env, input)

    runner.handler = flaky
    with pytest.raises(gh.GhError):
        seed.apply(seed.diff(plan, b, REPOS), b, echo=lambda _: None)
    runner.handler = handler
    d = seed.diff(plan, b, REPOS)
    assert d.count("create") == 0 and d.count("label") == 0
    assert d.count("parent") + d.count("depends") == 5         # one link made before the failure
    seed.apply(d, b, echo=lambda _: None)
    assert seed.verify(plan, b, REPOS) == []
    assert len(fake.issues) == 5


def test_existing_issues_are_never_rewritten(world):
    fake, runner, b, path = world
    plan = seed.load(path)
    seed.apply(seed.diff(plan, b, REPOS), b, echo=lambda _: None)
    ids = by_marker(fake)
    fake.issues[ids["A"]]["title"] = "Feature A (renamed by a human)"
    fake.issues[ids["A"]]["blockedBy"].append(ids["BE"])
    fake.issues[ids["B"]]["blockedBy"] = [ids["A"]]            # a human removed B's link to S1
    d = seed.diff(plan, b, REPOS)
    assert [(s.kind, s.item, s.target) for s in d.steps] == [("depends", "B", "S1")]
    assert any("title on the tracker differs" in n for n in d.notes)
    assert any("also blocked by" in n for n in d.notes)
    seed.apply(d, b, echo=lambda _: None)
    assert fake.issues[ids["A"]]["title"] == "Feature A (renamed by a human)"


def test_only_missing_labels_are_created(world):
    fake, runner, b, path = world
    fake.labels.update({"type:epic", "type:task", "swarm:status:ready"})
    d = seed.diff(seed.load(path), b, REPOS)
    names = [s.label[0] for s in d.steps if s.kind == "label"]
    assert "type:epic" not in names and "swarm:status:blocked" in names and "repo:acme/web" in names


def _task(data, tid):
    return next(t for t in data["tasks"] if t["id"] == tid)


@pytest.mark.parametrize("edit, message", [
    (lambda d: None, None),
    (lambda d: d.update(repo="other/plan"), "differs from backend.yaml"),
    (lambda d: d.update(status="maybe"), "status 'maybe'"),
    (lambda d: _task(d, "A").update(depends_on=["NOPE"]), "unknown id NOPE"),
    (lambda d: _task(d, "A").update(depends_on=["BE"]), "depend on tasks instead"),
    (lambda d: _task(d, "A").update(depends_on=["A"]), "depends on itself"),
    (lambda d: _task(d, "A").update(repo="acme/elsewhere"), "not listed under repos"),
    (lambda d: _task(d, "A").pop("repo"), "missing repo"),
    (lambda d: _task(d, "A").update(autonomy="yolo"), "autonomy 'yolo'"),
    (lambda d: _task(d, "A").update(epic="S1"), "is not an epic"),
    (lambda d: _task(d, "A").update(title=""), "missing title"),
    (lambda d: _task(d, "A").update(id="a b"), "id must be"),
])
def test_validation(world, edit, message):
    import yaml
    fake, runner, b, path = world
    data = yaml.safe_load(PLAN_YAML)
    edit(data)
    path.write_text(yaml.safe_dump(data))
    errors = seed.validate(seed.load(path), "acme/plan", REPOS)
    if message is None:
        assert errors == []
        return
    assert any(message in e for e in errors), errors
    with pytest.raises(seed.SeedError, match="plan file problems"):
        seed.diff(seed.load(path), b, REPOS)


def test_cycles_and_duplicates(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("tasks:\n"
                 "  - {id: X, title: x, repo: acme/api, depends_on: [Y]}\n"
                 "  - {id: Y, title: y, repo: acme/api, depends_on: [X]}\n"
                 "  - {id: Y, title: again, repo: acme/api}\n")
    errors = seed.validate(seed.load(p), None, REPOS)
    assert any("duplicate id" in e for e in errors)
    p.write_text("tasks:\n"
                 "  - {id: X, title: x, repo: acme/api, depends_on: [Y]}\n"
                 "  - {id: Y, title: y, repo: acme/api, depends_on: [X]}\n")
    assert any("dependency cycle" in e for e in seed.validate(seed.load(p), None, REPOS))


def test_duplicate_markers_are_refused(world):
    fake, runner, b, path = world
    plan = seed.load(path)
    seed.apply(seed.diff(plan, b, REPOS), b, echo=lambda _: None)
    ids = by_marker(fake)
    fake.issues[ids["FE"]]["body"] = "Copied by hand.\n<!-- dags-seed: A -->"
    with pytest.raises(seed.SeedError, match="two issues carry seed id A"):
        seed.diff(plan, b, REPOS)


def test_backend_without_seeding_is_refused(tmp_path, world):
    from backends.fake import FakeBackend
    fake, runner, b, path = world
    with pytest.raises(seed.SeedError, match="can't seed"):
        seed.diff(seed.load(path), FakeBackend(tmp_path / "b.yaml"), REPOS)


def test_matchwire_plan_is_valid():
    plan = seed.load(ROOT / "poc" / "matchwire" / "plan.yaml")
    repos = ["romanfq/matchwire-backend", "romanfq/matchwire-frontend"]
    assert seed.validate(plan, "romanfq/matchwire-spec", repos) == []
    order = [i.id for i in seed.topo_order(plan)]
    assert order[:3] == ["BE", "FE", "E2E"]
    for item in plan.items:
        for dep in item.depends_on:
            assert order.index(dep) < order.index(item.id)
    tiers = {i.id: i.autonomy for i in plan.items if not i.is_epic}
    assert tiers["E7"] == "human-must-scope" and tiers["E12"] == "auto-pr"
    assert len(tiers) == 18


def test_matchwire_plan_matches_backend_yaml():
    import yaml
    cfg = yaml.safe_load((ROOT / "backend.yaml").read_text())
    plan = seed.load(ROOT / "poc" / "matchwire" / "plan.yaml")
    assert plan.repo == cfg["github"]["repo"]
    assert seed.validate(plan, cfg["github"]["repo"], sorted(cfg["repos"])) == []
