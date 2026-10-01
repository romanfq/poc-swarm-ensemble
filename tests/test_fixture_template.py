"""The shared-template fixture must give the same repo state as building from scratch."""
from conftest import CodeWorld, Swarm, sh


def _state(repo):
    refs = sh(["git", "for-each-ref", "--format=%(refname) %(objectname)"], repo)
    files = sh(["git", "ls-tree", "-r", "main"], repo)
    return refs, files


def test_swarm_template_matches_a_fresh_build(tmp_path, swarm_template):
    fresh, copy = tmp_path / "fresh", tmp_path / "copy"
    fresh.mkdir()
    copy.mkdir()
    Swarm.build(fresh)
    Swarm.copy_template(swarm_template, copy)
    # same tree and files (commit ids differ only by timestamp)
    assert _state(fresh / "remote.git")[1] == _state(copy / "remote.git")[1]
    assert [r.split()[0] for r in _state(copy / "remote.git")[0].splitlines()] == \
           [r.split()[0] for r in _state(fresh / "remote.git")[0].splitlines()]
    assert sh(["git", "remote", "get-url", "origin"], copy / "seed").strip() == str(copy / "remote.git")
    # the copied remote is usable from the copied seed (it has no upstream, so fetch, don't pull)
    sh(["git", "fetch", "-q", "origin"], copy / "seed")
    assert sh(["git", "rev-parse", "origin/main"], copy / "seed") == sh(["git", "rev-parse", "main"], copy / "seed")


def test_code_template_matches_a_fresh_build(tmp_path, code_template):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    CodeWorld.build(fresh)
    assert _state(fresh / "app.git")[1] == _state(code_template / "app.git")[1]
