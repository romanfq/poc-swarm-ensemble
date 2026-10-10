"""update-from-source (GH-129): the pure change set, each refusal, dry run, swap, commit."""
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import BIN, GIT_ENV, sh
from dags import upgrade
from dags.upgrade import SourceInfo, plan_upgrade

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def info(**kw):
    base = dict(path=Path("/src"), ok=True, branch="main", sha="a" * 40, subject="Fix", dirty=False, behind=0)
    return SourceInfo(**{**base, **kw})


# -- pure change set ----------------------------------------------------------------

def test_added_changed_removed():
    src = {"bin/a": "1", "bin/b": "2new", "templates/t": "3"}
    dst = {"bin/b": "2", "bin/gone": "9", "templates/t": "3"}
    p = plan_upgrade(src, dst, {}, info=info())
    assert (p.added, p.changed, p.removed) == (["bin/a"], ["bin/b"], ["bin/gone"])
    assert p.pinned_sha is None and not p.refusals and not p.already_current


def test_local_edits_and_pinned_version():
    tree = {"bin/a": "1"}
    rec = {"source": "b" * 40, "tree": upgrade.tree_digest(tree)}
    assert not plan_upgrade(tree, tree, rec, info=info()).local_edits
    p = plan_upgrade(tree, {"bin/a": "edited"}, rec, info=info())
    assert p.local_edits and p.pinned_sha == "b" * 40


def test_already_current():
    tree = {"bin/a": "1"}
    assert plan_upgrade(tree, tree, {"source": "a" * 40}, info=info()).already_current
    assert not plan_upgrade(tree, tree, {"source": "c" * 40}, info=info()).already_current


@pytest.mark.parametrize("kw, flag, word", [
    (dict(branch="feature"), "allow_branch", "not main"),
    (dict(behind=3), "allow_branch", "behind origin/main"),
    (dict(behind=None), "allow_branch", "origin/main"),
    (dict(dirty=True), "allow_dirty", "uncommitted"),
])
def test_source_refusals_each_overridden_by_own_flag(kw, flag, word):
    p = plan_upgrade({}, {}, {}, info=info(**kw))
    assert any(word in r for r in p.refusals)
    other = "allow_dirty" if flag == "allow_branch" else "allow_branch"
    assert plan_upgrade({}, {}, {}, info=info(**kw), **{other: True}).refusals
    assert not plan_upgrade({}, {}, {}, info=info(**kw), **{flag: True}).refusals


def test_daemon_bad_source_same_repo_and_staged_refusals():
    assert "swarm.py stop" in plan_upgrade({}, {}, {}, info=info(), daemon_pid=42).refusals[0]
    assert plan_upgrade({}, {}, {}, info=info(ok=False)).refusals
    assert "this repo" in plan_upgrade({}, {}, {}, info=info(), same_repo=True).refusals[0]
    assert plan_upgrade({}, {}, {}, info=info(), commit=True, staged=["x"]).refusals
    assert not plan_upgrade({}, {}, {}, info=info(), commit=False, staged=["x"]).refusals


# -- real directories and git --------------------------------------------------------

def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def commit_all(repo, msg="c"):
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "-m", msg], repo)


@pytest.fixture
def source(tmp_path):
    origin = tmp_path / "origin.git"
    sh(["git", "init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    src = tmp_path / "src"
    sh(["git", "clone", "-q", str(origin), str(src)], tmp_path)
    sh(["git", "checkout", "-q", "-b", "main"], src)
    write(src, "bin/dags/__init__.py", "")
    write(src, "bin/swarm.py", "print('new')\n")
    write(src, "bin/new.py", "x\n")
    write(src, "templates/t.txt", "t\n")
    commit_all(src, "Fix the thing")
    sh(["git", "push", "-q", "-u", "origin", "main"], src)
    return src


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "coord"
    t.mkdir()
    sh(["git", "init", "-q", "-b", "main"], t)
    write(t, "bin/dags/__init__.py", "")
    write(t, "bin/swarm.py", "print('old')\n")
    write(t, "bin/stale.py", "stale and unlike anything else\n")
    write(t, "bin/dags/__pycache__/junk.pyc", "x")
    write(t, "README.md", "r\n")
    commit_all(t, "init")
    return t


def test_read_source(source):
    i = upgrade.read_source(source)
    assert i.ok and i.branch == "main" and i.behind == 0 and not i.dirty
    assert i.subject == "Fix the thing" and len(i.sha) == 40
    write(source, "bin/new.py", "changed\n")
    assert upgrade.read_source(source).dirty


def test_apply_swaps_and_records(source, target):
    i = upgrade.read_source(source)
    upgrade.apply(source, target, i, NOW)
    assert upgrade.snapshot_tree(target) == upgrade.snapshot_tree(source)
    assert not (target / "bin" / "stale.py").exists()
    assert not list(target.rglob("__pycache__"))
    assert not (target / "bin.new").exists() and not (target / "bin.old").exists()
    rec = upgrade.read_record(target)
    assert rec["source"] == i.sha and rec["branch"] == "main" and rec["subject"] == "Fix the thing"
    assert rec["upgraded"] == "2026-10-10T12:00:00Z" and "update-from-source" in rec["by"]


def test_apply_with_bad_copy_changes_nothing(source, target):
    shutil.rmtree(source / "bin" / "dags")
    (source / "bin" / "dags").write_text("not a dir")
    with pytest.raises(RuntimeError):
        upgrade.apply(source, target, upgrade.read_source(source), NOW)
    assert (target / "bin" / "stale.py").exists() and not (target / "bin.new").exists()


def test_commit_only_the_upgrade(source, target):
    i = upgrade.read_source(source)
    write(target, "README.md", "dirty\n")
    write(target, "drafts/x.md", "untracked\n")
    upgrade.apply(source, target, i, NOW)
    left = upgrade.commit(target, i)
    assert sh(["git", "log", "-1", "--format=%s"], target).strip() == f"Upgrade to poc-swarm-ensemble {i.sha[:7]}"
    assert sh(["git", "rev-list", "--count", "HEAD"], target).strip() == "2"
    files = set(sh(["git", "show", "--name-only", "--format=", "HEAD"], target).split())
    assert ".dags-source" in files and "bin/new.py" in files and "bin/stale.py" in files
    assert "README.md" not in files and "drafts/x.md" not in files
    assert set(left) == {"README.md", "drafts/"} or set(left) == {"README.md", "drafts/x.md"}


# -- the command --------------------------------------------------------------------

typer = pytest.importorskip("typer")
pytest.importorskip("rich")
from typer.testing import CliRunner  # noqa: E402

from dags import cli, daemon  # noqa: E402
from dags.config import Context  # noqa: E402

runner = CliRunner()


@pytest.fixture
def run(target, monkeypatch):
    monkeypatch.setattr(cli, "ctx", lambda: Context(target))

    def go(src, *args):
        return runner.invoke(cli.app, ["update-from-source", str(src), *args], catch_exceptions=False)
    return go


def snap(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git/" not in p.as_posix()}


def test_dry_run_lists_and_changes_nothing(run, source, target):
    before = snap(target)
    r = run(source)
    assert r.exit_code == 0
    assert "pinned:  unknown" in r.output and "added: 2" in r.output
    assert "bin/new.py" in r.output and "bin/stale.py" in r.output and "dry run" in r.output
    assert snap(target) == before


def test_dry_run_reports_refusals_with_exit_1(run, source, target, monkeypatch):
    monkeypatch.setattr(daemon, "running_pid", lambda c: 4242)
    r = run(source)
    assert r.exit_code == 1 and "--apply would refuse" in r.output and "swarm.py stop" in r.output


def test_apply_refuses_while_daemon_runs(run, source, target, monkeypatch):
    monkeypatch.setattr(daemon, "running_pid", lambda c: 4242)
    before = snap(target)
    r = run(source, "--apply")
    assert r.exit_code == 1 and "swarm.py stop" in r.output and snap(target) == before


def test_apply_overrides_and_second_run_is_noop(run, source, target):
    sh(["git", "checkout", "-q", "-b", "feature"], source)
    assert run(source, "--apply").exit_code == 1
    r = run(source, "--apply", "--allow-branch")
    assert r.exit_code == 0 and "nothing upgrades itself" in r.output
    sha = upgrade.read_source(source).sha
    assert upgrade.read_record(target)["source"] == sha
    after = snap(target)
    r = run(source, "--apply", "--allow-branch")
    assert r.exit_code == 0 and f"already at {sha[:7]}" in r.output and snap(target) == after


def test_apply_refuses_behind_and_dirty(run, source, target, tmp_path):
    write(source, "bin/new.py", "dirty\n")
    assert run(source, "--apply").exit_code == 1
    assert run(source, "--apply", "--allow-branch").exit_code == 1
    commit_all(source, "local only")
    assert run(source, "--apply").exit_code == 0     # ahead of origin/main is fine


def test_commit_flag(run, source, target):
    write(target, "drafts/x.md", "untracked\n")
    r = run(source, "--apply", "--commit")
    assert r.exit_code == 0 and "committed locally" in r.output and "not pushed" in r.output
    assert sh(["git", "rev-list", "--count", "HEAD"], target).strip() == "2"
    assert (target / "drafts" / "x.md").exists()
    assert "drafts/x.md" not in sh(["git", "show", "--name-only", "--format=", "HEAD"], target)


def test_commit_refuses_with_staged_changes(run, source, target):
    write(target, "README.md", "staged\n")
    sh(["git", "add", "README.md"], target)
    before = snap(target)
    r = run(source, "--apply", "--commit")
    assert r.exit_code == 1 and "staged" in r.output and snap(target) == before


def test_runs_from_the_pinned_copy_it_replaces(tmp_path, source):
    """End to end: the command lives in the target's own bin/, which it swaps out under itself."""
    pytest.importorskip("yaml")
    coord = tmp_path / "pinned"
    coord.mkdir()
    shutil.copytree(BIN, coord / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    sh(["git", "init", "-q", "-b", "main"], coord)
    commit_all(coord, "pin")
    env = dict(os.environ, DAGS_NO_VENV="1", **GIT_ENV)
    r = subprocess.run([sys.executable, str(coord / "bin" / "swarm.py"), "--root", str(coord),
                        "update-from-source", str(source), "--apply"],
                       cwd=str(coord), capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (coord / "bin" / "swarm.py").read_text() == "print('new')\n"
    assert upgrade.read_record(coord)["source"] == upgrade.read_source(source).sha
