"""typer CLI (Phase 4). Skipped where typer/rich aren't installed; run on the Mac
via bin/dev-setup.sh."""
import json

import pytest

typer = pytest.importorskip("typer")
pytest.importorskip("rich")
from typer.testing import CliRunner  # noqa: E402

from backends.base import TaskRef  # noqa: E402
from backends.fake import FakeBackend  # noqa: E402
from dags import cli  # noqa: E402

runner = CliRunner()


@pytest.fixture
def machine(swarm, tmp_path, monkeypatch):
    ctx = swarm.clone("mac-a")
    backend = FakeBackend(tmp_path / "b.yaml")
    backend.add("E1", title="Epic", epic=True)
    backend.add("T1", title="Poll", epic_of="E1", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    ctx.set_backend(backend)
    monkeypatch.setattr(cli, "ctx", lambda: ctx)
    return ctx


def invoke(*args):
    return runner.invoke(cli.app, list(args), catch_exceptions=False)


def test_help_lists_commands():
    r = invoke("--help")
    assert r.exit_code == 0
    for word in ("start", "stop", "pause", "resume", "status", "board", "protect", "task", "backend"):
        assert word in r.output


def test_protect_is_a_dry_run_by_default(machine):
    r = invoke("protect", "OWNER/app")
    assert r.exit_code == 0
    assert "repos/OWNER/app/branches/main/protection" in r.output
    assert '"enforce_admins": true' in r.output
    assert "dry run" in r.output


def test_protect_yes_alone_is_still_a_dry_run(machine, fake_gh):
    r = invoke("protect", "OWNER/app", "--yes")
    assert r.exit_code == 0 and "dry run" in r.output
    assert fake_gh.calls == []


def test_protect_apply_yes_calls_gh_without_prompting(machine, fake_gh):
    r = invoke("protect", "OWNER/app", "--apply", "--yes")
    assert r.exit_code == 0, r.output
    assert "Set branch protection" not in r.output
    assert '"enforce_admins": true' in r.output and "branch protection set" in r.output
    [call] = fake_gh.calls
    assert call["args"][:2] == ["api", "repos/OWNER/app/branches/main/protection"]
    assert json.loads(call["input"])["enforce_admins"] is True


def test_protect_apply_alone_still_prompts(machine, fake_gh):
    r = runner.invoke(cli.app, ["protect", "OWNER/app", "--apply"], input="n\n")
    assert r.exit_code == 1 and "Set branch protection on OWNER/app:main?" in r.output
    assert fake_gh.calls == []
    r = runner.invoke(cli.app, ["protect", "OWNER/app", "--apply"], input="y\n")
    assert r.exit_code == 0 and "branch protection set" in r.output
    assert len(fake_gh.calls) == 1


@pytest.mark.parametrize("flags", [["--apply"], ["--apply", "--yes"]])
def test_protect_refuses_a_non_human_operator(machine, fake_gh, flags):
    (machine.swarm_dir / "local.yaml").write_text("human: nobody\nworker_token: none\n")
    r = runner.invoke(cli.app, ["protect", "OWNER/app", *flags], input="y\n")
    assert r.exit_code == 1 and "not listed in humans.yaml" in r.output
    assert fake_gh.calls == []


def test_plan_sync_and_task_list(machine):
    r = invoke("plan", "sync")
    assert r.exit_code == 0 and "imported" in r.output
    r = invoke("task", "list")
    assert "T1" in r.output and "(ready)" in r.output
    r = invoke("task", "show", "T1")
    data = json.loads(r.stdout)
    assert data["state"] == "open" and data["ready"] is True


def test_backend_commands(machine):
    invoke("plan", "sync")
    assert invoke("backend", "ready").output.split() == ["T1"]
    assert "# T1: Poll" in invoke("backend", "get-task", "T1").output
    assert invoke("backend", "set-status", "T1", "blocked").exit_code == 0
    assert invoke("backend", "ready").output.split() == []
    r = runner.invoke(cli.app, ["backend", "set-status", "T1", "bogus"])
    assert r.exit_code == 1


def test_pause_resume_throttle_and_status(machine):
    assert invoke("pause").exit_code == 0
    r = invoke("status", "--json")
    rows = {x["label"]: x for x in json.loads(r.stdout)}
    assert "stopped" in rows["scheduler"]["value"]
    assert invoke("throttle", "2").exit_code == 0
    assert invoke("resume").exit_code == 0
    r = invoke("status")
    assert "swarm" in r.output and "mac-a" in r.output


def test_human_levers_need_humans_yaml(machine):
    invoke("plan", "sync")
    assert invoke("quota", "set", "5", "--reason", "budget").exit_code == 0
    assert "5" in invoke("quota", "show").output
    assert invoke("task", "freeze", "T1", "--reason", "hold").exit_code == 0
    assert "frozen" in invoke("task", "show", "T1").output
    assert invoke("task", "unfreeze", "T1").exit_code == 0
    (machine.swarm_dir / "local.yaml").write_text("human: mallory\n")
    r = runner.invoke(cli.app, ["quota", "set", "9"])
    assert r.exit_code == 1 and "humans.yaml" in r.output


def test_errors_are_one_line(machine):
    r = runner.invoke(cli.app, ["task", "freeze", "NOPE", "--reason", "x"])
    assert r.exit_code == 1
    assert "Traceback" not in r.output


def test_panel_renders(machine):
    from rich.console import Console

    from dags import panel
    console = Console(record=True, width=100)
    console.print(panel.render(panel.status_rows(machine, share=3)))
    text = console.export_text()
    assert "identity" in text and "scheduler" in text and "quota-share 3" in text


def test_identity_commands(machine):
    assert invoke("identity", "show").output.strip() == machine.identity
    invoke("plan", "sync")
    invoke("task", "list")
    from dags import ledger as L
    import resolve as rv
    L.claim(machine, rv.index(machine.root)["T1"])
    (machine.swarm_dir / "identity").write_text("mac-a\n")
    r = runner.invoke(cli.app, ["identity", "set", "mac-z"])
    assert r.exit_code == 1 and "still holds T1" in r.output
    assert invoke("identity", "set", "mac-z", "--force").exit_code == 0
    assert (machine.swarm_dir / "identity").read_text().strip() == "mac-z"


def test_backend_seed_dry_run_then_apply(machine, tmp_path):
    from backends.github import GitHubBackend
    from conftest import FakeGh
    from dags import gh
    from fakes import FakeGitHub
    fake = FakeGitHub(repo="acme/plan")
    gh.set_runner(FakeGh(fake))
    try:
        machine.set_backend(GitHubBackend("acme/plan", cache_seconds=0))
        plan = tmp_path / "plan.yaml"
        plan.write_text("repo: acme/plan\n"
                        "epics:\n  - {id: EP, title: Epic}\n"
                        "tasks:\n"
                        "  - {id: T1, epic: EP, title: One, repo: OWNER/app}\n"
                        "  - {id: T2, epic: EP, title: Two, repo: OWNER/app, depends_on: [T1]}\n")
        r = invoke("backend", "seed", str(plan))
        assert r.exit_code == 0, r.output
        assert "create  task T2: Two" in r.output and "dry run" in r.output
        assert fake.issues == {}
        r = invoke("backend", "seed", str(plan), "--apply", "--yes")
        assert r.exit_code == 0, r.output
        assert "plan seeded" in r.output and len(fake.issues) == 3
        r = invoke("backend", "seed", str(plan))
        assert "already matches" in r.output
        r = invoke("plan", "sync")
        assert r.exit_code == 0 and "imported" in r.output
        bad = tmp_path / "bad.yaml"
        bad.write_text("repo: other/plan\ntasks: []\n")
        r = runner.invoke(cli.app, ["backend", "seed", str(bad)])
        assert r.exit_code == 1 and "differs from backend.yaml" in r.output
    finally:
        gh.set_runner(None)


def test_backend_init_labels_what_the_plan_is_missing(machine):
    b = machine.backend
    b.add("D1", title="Blocker", closed=True)
    b.add("T2", title="Needs D1", epic_of="E1", blocked_by=["D1"], labels=["type:task"])
    b.add("U1", title="Just a thought")
    r = invoke("backend", "init")
    assert r.exit_code == 0 and "dry run" in r.output
    assert "label D1 type:task swarm:status:done (dependency of T2)" in r.output
    assert "U1" not in r.output                                    # never guessed into the plan
    assert "type:task" not in b.get_task(TaskRef("D1")).labels
    r = runner.invoke(cli.app, ["backend", "init", "--apply"], input="y\n", catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert b.in_plan(TaskRef("D1")) and not b.in_plan(TaskRef("U1"))
    assert "nothing to do" in invoke("backend", "init").output


def test_backend_adopt(machine):
    b = machine.backend
    b.add("U1", title="File it, then hand it to the swarm")
    r = invoke("backend", "adopt", "U1", "--autonomy", "auto-pr")
    assert r.exit_code == 0 and "is in the plan" in r.output
    t = b.get_task(TaskRef("U1"))
    assert t.status == "ready" and t.autonomy == "auto-pr" and "type:task" in t.labels
    assert TaskRef("U1") in b.ready_tasks()
    r = invoke("backend", "adopt", "T1")
    assert r.exit_code != 0 and "already in the plan" in r.output
    b.add("U2", title="Closed", closed=True)
    assert "reopen it first" in invoke("backend", "adopt", "U2").output
