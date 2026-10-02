"""venv.find_reusable: pure functions on temp dirs, no real venv builds."""
import json

import pytest

from dags import venv


def _repo(path, reqs="typer\n", dev_reqs="pytest\n"):
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "requirements.txt").write_text(reqs)
    (path / "bin" / "requirements-dev.txt").write_text(dev_reqs)
    return path


def _fake_venv(src, stamp_overrides=None, dev=True, stamp=True, interpreter=True):
    if interpreter:
        py = venv.venv_python(src)
        py.parent.mkdir(parents=True)
        py.write_text("")
    if stamp:
        s = venv.wanted_stamp(src, dev)
        s.update(stamp_overrides or {})
        venv.venv_dir(src).mkdir(parents=True, exist_ok=True)
        (venv.venv_dir(src) / venv.STAMP).write_text(json.dumps(s))


@pytest.fixture
def pair(tmp_path):
    return _repo(tmp_path / "coord"), _repo(tmp_path / "wt")


def test_match_returns_interpreter(pair):
    src, wt = pair
    _fake_venv(src)
    assert venv.find_reusable(wt, source=src) == venv.venv_python(src)


def test_source_from_env(pair, monkeypatch):
    src, wt = pair
    _fake_venv(src)
    monkeypatch.setenv("DAGS_VENV_FROM", str(src))
    assert venv.find_reusable(wt) == venv.venv_python(src)


def test_source_from_context_json(pair, monkeypatch):
    src, wt = pair
    monkeypatch.delenv("DAGS_VENV_FROM", raising=False)
    _fake_venv(src)
    (wt / ".swarm-task").mkdir()
    (wt / ".swarm-task" / "context.json").write_text(json.dumps({"coordination_root": str(src)}))
    assert venv.find_reusable(wt) == venv.venv_python(src)


@pytest.mark.parametrize("key,value", [("platform", "other"), ("machine", "other"),
                                       ("python", "2.7"), ("requirements", "0" * 64)])
def test_mismatch_builds(pair, key, value):
    src, wt = pair
    _fake_venv(src, {key: value})
    assert venv.find_reusable(wt, source=src) is None


def test_different_requirements_in_worktree_builds(pair):
    src, wt = pair
    _fake_venv(src)
    (wt / "bin" / "requirements.txt").write_text("typer\nrich\n")
    assert venv.find_reusable(wt, source=src) is None


def test_non_dev_stamp_when_dev_wanted_builds(pair):
    src, wt = pair
    _fake_venv(src, dev=False)
    assert venv.find_reusable(wt, dev=True, source=src) is None


def test_missing_stamp_builds(pair):
    src, wt = pair
    _fake_venv(src, stamp=False)
    assert venv.find_reusable(wt, source=src) is None


def test_corrupt_stamp_builds(pair):
    src, wt = pair
    _fake_venv(src)
    (venv.venv_dir(src) / venv.STAMP).write_text("{not json")
    assert venv.find_reusable(wt, source=src) is None


def test_missing_interpreter_builds(pair):
    src, wt = pair
    _fake_venv(src, interpreter=False)
    assert venv.find_reusable(wt, source=src) is None


def test_no_source_builds(pair, monkeypatch):
    _, wt = pair
    monkeypatch.delenv("DAGS_VENV_FROM", raising=False)
    assert venv.find_reusable(wt) is None


def test_source_is_self_builds(pair):
    src, _ = pair
    _fake_venv(src)
    assert venv.find_reusable(src, source=src) is None
