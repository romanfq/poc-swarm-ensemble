"""GH-100: choosing a worker launches it straight away; whatever talks to GitHub follows."""
import threading

import pytest

import resolve as rv
from conftest import sh
from dags import feed, work, worktree
from dags import records as R
from dags import ledger as L


@pytest.fixture
def awaiting(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll the feed", epic_of="E1", body="Poll it.",
                      labels=["repo:OWNER/app", "swarm:autonomy:self-approve"])
    a = world.machine("mac-a")
    world.scheduler(a).cycle()                       # claims T1; no default worker, so it awaits one
    work.drain()
    return world, a, rv.index(a.root)["T1"]


def _choose(world, a, d, worker="claude"):
    return work.choose_worker(a, d, worker, launch=world.launch, platform="darwin")


def test_launch_is_called_while_the_repo_lock_is_held(awaiting):
    world, a, d = awaiting
    held, release, launched = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with a.coord.lock:
            held.set()
            release.wait(10)

    def launch(cmd):
        world.launched.append(cmd)
        launched.set()

    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5)
    job = threading.Thread(target=lambda: work.choose_worker(a, d, "claude", launch=launch, platform="darwin"),
                           daemon=True)
    job.start()
    try:
        assert launched.wait(5), "the launcher waited for the repo lock"
    finally:
        release.set()
    job.join(10)
    work.drain()
    cp = L.read_checkpoint(d)                        # recorded once the lock came free
    assert cp["worker"] == "claude" and cp["dispatch_failed"] is None


def test_a_free_lock_records_the_checkpoint_before_the_launch(awaiting):
    world, a, d = awaiting
    seen = []

    def launch(cmd):
        seen.append(L.read_checkpoint(d).get("worker"))
        world.launched.append(cmd)

    work.choose_worker(a, d, "claude", launch=launch, platform="darwin")
    assert seen == ["claude"]


def test_no_pull_and_no_fetch_before_the_launch_and_the_push_follows(awaiting, monkeypatch):
    world, a, d = awaiting
    calls = []
    real = a.coord.__class__
    monkeypatch.setattr(real, "pull", lambda self: calls.append("pull"))
    monkeypatch.setattr(real, "push", lambda self, retries=6: calls.append("push"))
    monkeypatch.setattr("dags.repos.fetch_origin", lambda repo, path: calls.append("fetch") or True)

    def launch(cmd):
        calls.append("launch")
        world.launched.append(cmd)

    work.choose_worker(a, d, "claude", launch=launch, platform="darwin")
    work.drain()
    assert calls[0] == "launch" and sorted(calls[1:]) == ["fetch", "push"]


def test_github_unreachable_still_launches_and_the_commit_waits_for_a_later_push(awaiting, monkeypatch):
    world, a, d = awaiting
    sh(["git", "remote", "set-url", "origin", str(world.base / "nowhere.git")], a.root)
    monkeypatch.setattr("dags.repos.fetch_origin", lambda repo, path: False)
    label = _choose(world, a, d)
    work.drain()
    assert label == "claude" and len(world.launched) == 1
    assert L.read_checkpoint(d)["worker"] == "claude"


def test_the_spec_comes_from_the_cache_for_this_claim_when_the_issue_read_fails(awaiting, monkeypatch):
    world, a, d = awaiting
    cid = work.my_claim(a, d)
    assert "Poll it." in work._load_spec(a, d, cid)  # left by the claim-time prepare

    def down(ref):
        raise RuntimeError("GitHub is down")
    monkeypatch.setattr(world.backend, "get_task", down)
    _choose(world, a, d)
    wt = worktree.worktree_path(a, d)
    assert "Poll it." in (wt / ".swarm-task" / "spec.md").read_text()


def test_a_spec_cached_for_an_older_claim_is_never_used(awaiting, monkeypatch):
    world, a, d = awaiting
    work._store_spec(a, d, "some-older-claim", "# stale\n")
    assert work._load_spec(a, d, work.my_claim(a, d)) is None

    def down(ref):
        raise RuntimeError("GitHub is down")
    monkeypatch.setattr(world.backend, "get_task", down)
    with pytest.raises(RuntimeError, match="down"):
        _choose(world, a, d)
    assert world.launched == []


def test_an_edited_issue_body_wins_over_the_cache(awaiting):
    world, a, d = awaiting
    issues = world.backend._load()
    issues["T1"]["body"] = "Edited after the claim."
    world.backend._save(issues)
    _choose(world, a, d)
    assert "Edited after the claim." in (worktree.worktree_path(a, d) / ".swarm-task" / "spec.md").read_text()


def test_the_event_and_the_feed_carry_the_elapsed_time(awaiting):
    world, a, d = awaiting
    handed = _choose(world, a, d)
    assert handed.elapsed_s >= 0 and handed.message("T1").startswith("Handed T1 to claude (")
    events = sorted((d / "events").glob("*worker-dispatched*"))
    data = R.load_yaml(events[-1])
    assert data["elapsed_s"] >= 0 and data["slowest"] in {"repo", "worktree", "spec", "inject", "checkpoint"}
    text = feed._describe_event("T1", "Roman", "mac-a", {"kind": "worker-dispatched", "worker": "Claude",
                                                         "elapsed_s": 1.14, "slowest": "worktree",
                                                         "slowest_s": 1.02})
    assert text.endswith("(mac-a) in 1.1s, slowest: worktree 1.0s")
    old = feed._describe_event("T1", "Roman", "mac-a", {"kind": "worker-dispatched", "worker": "Claude"})
    assert old == "Roman's swarm handed T1 to Claude (mac-a)"


def test_a_launch_failure_still_ends_in_dispatch_failed(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll", epic_of="E1", labels=["repo:OWNER/app", "swarm:autonomy:self-approve"])
    a = world.machine("mac-a")

    def broken(cmd):
        raise RuntimeError("osascript exploded")
    world.launch = broken
    world.scheduler(a, worker="claude").cycle()
    work.drain()
    d = rv.index(a.root)["T1"]
    failed = L.read_checkpoint(d)["dispatch_failed"]
    assert failed["attempts"] == 1 and "osascript exploded" in failed["error"]
