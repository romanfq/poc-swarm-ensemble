"""typer CLI (Phase 4). Skipped where typer/rich aren't installed; run on the Mac
via bin/dev-setup.sh."""
import json

import pytest

typer = pytest.importorskip("typer")
pytest.importorskip("rich")
from typer.testing import CliRunner  # noqa: E402

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
