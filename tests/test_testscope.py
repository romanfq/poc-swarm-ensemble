"""Which tests for which diff (GH-50): testscope.py is pure, so these build a tiny repo on disk."""
from pathlib import Path

import pytest

from conftest import sh
from dags import testscope as T


@pytest.fixture
def repo(tmp_path):
    def put(rel, text=""):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    put("bin/resolve.py", "")
    put("bin/dags/__init__.py")
    put("bin/dags/plan.py", "import resolve\n")
    put("bin/dags/scheduler.py", "from dags import plan\n")
    put("bin/dags/board_stuff.py", "import resolve\n")
    put("bin/dags/lonely.py")
    put("bin/board.py", "from dags import board_stuff\n")
    put("tests/test_plan_sync.py", "from dags import plan\n")
    put("tests/test_scheduler.py", "from dags import scheduler\n")
    put("tests/test_board.py", "import resolve\nfrom dags import board_stuff\n")
    put("tests/test_resolve.py", "import resolve\n")
    put("tests/conftest.py")
    put("tests/map.yaml", "map:\n  bin/dags/plan.py: [tests/test_plan_sync.py]\n  bin/resolve.py: tests/test_resolve.py\n")
    put("docs/SPEC.md", "x")
    return tmp_path


def files(proposal, scope):
    return T.scope_files(proposal, scope)


def test_a_mapped_file_gets_its_tests_plus_neighbours_but_not_unrelated_ones(repo):
    p = T.propose(["bin/dags/plan.py"], repo)
    assert files(p, "targeted") == ["tests/test_plan_sync.py"]
    assert files(p, "neighbours") == ["tests/test_plan_sync.py", "tests/test_scheduler.py"]
    assert p["recommendation"] == "neighbours"
    assert "tests/test_board.py" not in files(p, "neighbours")
    reasons = {t["file"]: t["reason"] for t in p["options"]["neighbours"]["tests"]}
    assert "scheduler" in reasons["tests/test_scheduler.py"]          # one line of why per test file


def test_docs_only_recommends_no_tests(repo):
    p = T.propose(["docs/SPEC.md", "README.md"], repo)
    assert p["recommendation"] == "none" and files(p, "none") == []
    assert T.command_for("./run-tests", files(p, "none")) is None


def test_unmapped_file_falls_back_to_full(repo):
    p = T.propose(["bin/dags/lonely.py"], repo)
    assert p["recommendation"] == "full" and p["unmapped"] == ["bin/dags/lonely.py"]
    assert files(p, "full") is None
    # one unmapped file among mapped ones is enough
    assert T.propose(["bin/dags/plan.py", "bin/dags/lonely.py"], repo)["recommendation"] == "full"
    assert T.propose(["pyproject.toml"], repo)["recommendation"] == "full"


def test_shared_test_files_mean_full(repo):
    assert T.propose(["tests/conftest.py"], repo)["recommendation"] == "full"


def test_import_fallback_when_the_table_has_no_entry(repo):
    p = T.propose(["bin/dags/board_stuff.py"], repo)
    assert files(p, "targeted") == ["tests/test_board.py"]
    assert "imports dags.board_stuff" in p["options"]["targeted"]["tests"][0]["reason"]


def test_a_changed_test_file_is_its_own_scope(repo):
    p = T.propose(["tests/test_resolve.py"], repo)
    assert files(p, "targeted") == ["tests/test_resolve.py"] and p["recommendation"] == "targeted"


def test_command_narrowed_to_files():
    assert T.command_for("./bin/dev-setup.sh", ["tests/a b.py", "tests/c.py"]) == "./bin/dev-setup.sh 'tests/a b.py' tests/c.py"
    assert T.command_for("./bin/dev-setup.sh", None) == "./bin/dev-setup.sh"
    assert T.command_for(None, ["tests/c.py"]) is None
    assert T.command_for("./bin/dev-setup.sh", ["tests/c.py"], "pytest -q") == "pytest -q tests/c.py"
    assert T.command_for("./bin/dev-setup.sh", ["tests/c.py"], "pytest -q") != T.command_for("x", None)
    with pytest.raises(ValueError):
        T.scope_files({"options": {}}, "bogus")


def test_estimate_comes_from_recorded_durations(repo):
    durations = [{"test": "tests/test_scheduler.py::test_a", "seconds": 4.0},
                 {"test": "tests/test_board.py::test_b", "seconds": 9.0}]
    p = T.propose(["bin/dags/plan.py"], repo, durations)
    assert p["options"]["targeted"]["seconds"] is None            # nothing recorded for that file
    assert p["options"]["neighbours"]["seconds"] == 4.0
    assert p["options"]["full"]["seconds"] == 13.0
    assert "~4s" in T.render_text(p)


def test_changed_files_sees_committed_staged_and_new_files(tmp_path):
    sh(["git", "init", "-q", "-b", "main"], tmp_path)
    (tmp_path / "a.py").write_text("1\n")
    sh(["git", "add", "-A"], tmp_path)
    sh(["git", "commit", "-q", "-m", "base"], tmp_path)
    sh(["git", "checkout", "-q", "-b", "swarm/T"], tmp_path)
    (tmp_path / "a.py").write_text("2\n")
    sh(["git", "commit", "-qam", "edit"], tmp_path)
    (tmp_path / "b.py").write_text("new\n")
    (tmp_path / ".swarm-task").mkdir()
    (tmp_path / ".swarm-task" / "plan.md").write_text("x")
    assert T.changed_files(tmp_path, "main") == ["a.py", "b.py"]
    assert T.diff_sha(["b.py", "a.py"]) == T.diff_sha(["a.py", "b.py"])


def test_the_repos_own_table_is_valid():
    root = Path(__file__).resolve().parent.parent
    for glob, tests in T.load_map(root):
        for t in tests:
            assert (root / t).is_file(), f"{glob} maps to missing {t}"
    p = T.propose(["bin/dags/plan.py"], root)
    assert files(p, "targeted") == ["tests/test_plan_sync.py"]
    assert "tests/test_board.py" not in files(p, "neighbours")
