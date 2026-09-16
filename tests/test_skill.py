"""The injected skill, end to end (Ch.7.3). `swarm-task` itself is stdlib-only,
but it delegates to `swarm.py task ...`, so this needs typer/rich (Mac venv)."""
import json
import os
import stat
import subprocess
import sys

import pytest

import resolve as rv
from dags import timeutil, worktree

FAKE_GH = r'''#!/usr/bin/env python3
import json, sys, os
log = os.environ["FAKE_GH_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "token": os.environ.get("GH_TOKEN")}) + "\n")
if args[:2] == ["pr", "list"]:
    print("[]")
elif args[:2] == ["pr", "create"]:
    print("https://github.com/OWNER/app/pull/7")
elif args[:2] == ["api", "user"]:
    print("romanfq")
else:
    print("{}")
'''


def test_skill_is_stdlib_only():
    src = (rv.Path(__file__).resolve().parent.parent / "bin" / "skill" / "swarm-task").read_text()
    for banned in ("import yaml", "import typer", "import rich", "from dags", "import resolve"):
        assert banned not in src


def test_skill_help_runs_without_context(tmp_path):
    skill = rv.Path(__file__).resolve().parent.parent / "bin" / "skill" / "swarm-task"
    r = subprocess.run([sys.executable, str(skill), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and "context.json is missing" in (r.stderr + r.stdout)


def test_plan_implement_done_via_the_skill(world, tmp_path, monkeypatch):
    pytest.importorskip("typer")
    pytest.importorskip("rich")
    world.backend.add("T1", title="Poll the feed", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    a = world.machine("mac-a")
    (a.root / "fake-backend.yaml").symlink_to(world.backend.path)
    ghbin = tmp_path / "ghbin"
    ghbin.mkdir()
    (ghbin / "gh").write_text(FAKE_GH)
    (ghbin / "gh").chmod(0o755 | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{ghbin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    wt = worktree.worktree_path(a, d)

    def skill(*args):
        r = subprocess.run([str(wt / ".swarm-task" / "swarm-task"), *args], cwd=wt,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    assert "Wrote" in skill("plan")
    (wt / ".swarm-task" / "plan.md").write_text("# Plan\nAdd poller.py and a test.\n")
    assert "self-approved" in skill("plan", "--submit")
    assert "approved" in skill("status")
    skill("note", "--tried", "cron", "--summary", "Adds a poller")
    out = skill("implement")
    assert "Add poller.py" in out and "- cron" in out
    (wt / "poller.py").write_text("print(1)\n")
    assert "https://github.com/OWNER/app/pull/7" in skill("done")
    a.coord.pull()
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert any(c["args"][:2] == ["pr", "create"] for c in calls)
