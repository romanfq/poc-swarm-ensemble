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
    world.backend.add("T1", title="Poll the feed", labels=["repo:OWNER/app", "swarm:autonomy:self-approve"])
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
    refused = subprocess.run([str(wt / ".swarm-task" / "swarm-task"), "test"], cwd=wt, capture_output=True, text=True)
    assert refused.returncode != 0 and "ask the human" in refused.stderr
    proposal = skill("test", "--propose", "--accept")
    assert "[full]" in proposal and "Accepted the recommendation: full" in proposal
    assert "tests passed (scope: full)" in skill("test")
    assert "https://github.com/OWNER/app/pull/7" in skill("done")
    a.coord.pull()
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert any(c["args"][:2] == ["pr", "create"] for c in calls)


def test_wait_and_news_via_the_skill(world, monkeypatch):
    pytest.importorskip("typer")
    pytest.importorskip("rich")
    from dags import work
    world.backend.add("T1", title="Poll the feed", labels=["repo:OWNER/app", "swarm:autonomy:human-must-review"])
    a = world.machine("mac-a")
    (a.root / "fake-backend.yaml").symlink_to(world.backend.path)
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    wt = worktree.worktree_path(a, d)

    def skill(*args):
        return subprocess.run([str(wt / ".swarm-task" / "swarm-task"), *args], cwd=wt, capture_output=True, text=True)

    (wt / ".swarm-task" / "plan.md").write_text("# Plan\nAdd poller.py.\n")
    assert skill("plan", "--submit").returncode == 0
    r = skill("wait", "--for", "approved", "--timeout", "1", "--interval", "0.2")
    assert r.returncode == 3 and "nothing yet" in r.stdout
    jane = world.machine("jane-mac", human="jane")
    jd = rv.index(jane.root)["T1"]
    work.approve_plan(jane, jd, "changes-requested", "smaller steps please")
    r = skill("wait", "--for", "approved", "--timeout", "5", "--interval", "0.2")
    assert r.returncode == 2 and "smaller steps please" in r.stdout
    assert "smaller steps please" not in skill("status").stdout       # one-off news is shown once
    assert skill("plan", "--submit").returncode == 0
    work.approve_plan(jane, jd)
    r = skill("wait", "--for", "approved", "--timeout", "5", "--interval", "0.2")
    assert r.returncode == 0 and "approved the plan" in r.stdout
    assert skill("block", "keep cancelled matches?").returncode == 0
    work.answer_question(jane, jd, "yes")
    r = skill("wait", "--for", "answer", "--timeout", "5", "--interval", "0.2")
    assert r.returncode == 0 and "answered" in r.stdout and "yes" in r.stdout
    from dags import actions
    actions.freeze(jane, jd, "stop")
    a.coord.pull()                                   # `note` reads the ledger as the daemon last synced it (GH-113)
    r = skill("note", "--summary", "x")
    assert r.returncode != 0 and "no longer yours" in r.stderr and "froze the task" in r.stdout


def test_status_and_implement_print_what_the_reviewer_said(world):
    pytest.importorskip("typer")
    pytest.importorskip("rich")
    from dags import work
    world.backend.add("T1", title="Poll the feed", labels=["repo:OWNER/app", "swarm:autonomy:human-must-review"])
    a = world.machine("mac-a")
    (a.root / "fake-backend.yaml").symlink_to(world.backend.path)
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    wt = worktree.worktree_path(a, d)

    def skill(*args):
        return subprocess.run([str(wt / ".swarm-task" / "swarm-task"), *args], cwd=wt, capture_output=True, text=True)

    (wt / ".swarm-task" / "plan.md").write_text("# Plan\nAdd poller.py.\n")
    assert skill("plan", "--submit").returncode == 0
    assert "Reviewer says" not in skill("status").stdout
    from backends.base import TaskRef
    from backends.fake import FakeBackend
    issue = FakeBackend(a.root / "fake-backend.yaml")      # the file the skill's backend writes (not a symlink any more)
    issue.reply(TaskRef("T1"), "jane-gh", "1: use the push feed")
    issue.reply(TaskRef("T1"), "a-stranger", "spam")
    out = skill("status").stdout
    assert "Reviewer says" in out and "1: use the push feed" in out and "spam" not in out
    refused = skill("implement")                          # not approved yet: the answers still print
    assert refused.returncode != 0 and "1: use the push feed" in refused.stdout
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"])
    out = skill("implement").stdout
    assert "# Reviewer says" in out and "1: use the push feed" in out
    assert skill("block", "keep cancelled matches?").returncode == 0
    issue.reply(TaskRef("T1"), "jane-gh", "yes, keep them")
    out = skill("status").stdout
    assert "yes, keep them" in out and "1: use the push feed" not in out    # the window moved to the question


def test_wait_covers_the_test_scope_gate(world):
    pytest.importorskip("typer")
    pytest.importorskip("rich")
    from dags import work
    world.backend.add("T1", title="Poll the feed", labels=["repo:OWNER/app", "swarm:autonomy:human-must-review"])
    a = world.machine("mac-a")
    (a.root / "fake-backend.yaml").symlink_to(world.backend.path)
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    wt = worktree.worktree_path(a, d)

    def skill(*args):
        return subprocess.run([str(wt / ".swarm-task" / "swarm-task"), *args], cwd=wt, capture_output=True, text=True)

    def wait(mode):
        return skill("wait", "--for", mode, "--timeout", "1.5", "--interval", "0.2")

    (wt / ".swarm-task" / "plan.md").write_text("# Plan\nAdd poller.py.\n")
    assert skill("plan", "--submit").returncode == 0
    jane = world.machine("jane-mac", human="jane")
    jd = rv.index(jane.root)["T1"]
    work.approve_plan(jane, jd)
    assert skill("wait", "--for", "approved", "--timeout", "5", "--interval", "0.2").returncode == 0
    (wt / "poller.py").write_text("print(1)\n")
    assert skill("test", "--propose").returncode == 0
    # an unanswered proposal is outstanding: nothing returns 0, nothing claims "no open question"
    for mode in ("answer", "tests", "any"):
        r = wait(mode)
        assert r.returncode == 3, (mode, r.stdout, r.stderr)
        assert "no open" not in r.stdout.lower()
    jane.coord.pull()
    work.answer_tests(jane, jd, "targeted", targeted_enough=True, note_text="just the poller")
    r = wait("answer")
    assert r.returncode == 0 and "targeted" in r.stdout and "just the poller" in r.stdout
    # the answer is one-off news; `tests` and `any` pick it up in a fresh worker session
    for mode in ("tests", "any"):
        (wt / ".swarm-task" / "seen.json").unlink(missing_ok=True)
        r = wait(mode)
        assert r.returncode == 0 and "targeted" in r.stdout, (mode, r.stdout, r.stderr)
