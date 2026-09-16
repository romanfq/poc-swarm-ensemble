"""Phase 5: the worker side — plan gate, notes, feedback and `done` (Ch.7.3, 8, 9.1, 9.3)."""
import subprocess

import pytest

import resolve as rv
from backends.base import TaskRef
from conftest import sh
from dags import ledger as L
from dags import timeutil, work, worktree


@pytest.fixture
def claimed(world):
    world.backend.add("E1", title="Epic", epic=True)
    world.backend.add("T1", title="Poll the feed", epic_of="E1", body="Poll it.",
                      labels=["repo:OWNER/app", "swarm:autonomy:human-must-review"])
    a = world.machine("mac-a", extra="bot:\n  login: dags-bot\n  email: 1+dags-bot@users.noreply.github.com\n")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T1"]
    return world, a, d, worktree.worktree_path(a, d)


def _approve(world, a, d):
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"])
    a.coord.pull()


def test_plan_gate_for_human_must_review(claimed):
    world, a, d, wt = claimed
    assert work.implement_gate(a, d)["plan_status"] is None
    with pytest.raises(work.WorkError):
        work.submit_plan(a, d, "   ")
    assert work.submit_plan(a, d, "# Plan\npoll every 60s") == "pending-review"
    gate = work.implement_gate(a, d)
    assert not gate["allowed"] and gate["plan_status"] == "pending-review"
    assert "plan for review" in world.backend.comments(TaskRef("T1"))[-1]
    # a human on another machine sends it back, then approves a revised plan
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"], "changes-requested", "use the push feed")
    a.coord.pull()
    assert rv.plan_status(d, a.human_names) == "changes-requested"
    work.submit_plan(a, d, "# Plan v2\nuse the push feed")
    assert rv.plan_status(d, a.human_names) == "pending-review"     # old review doesn't carry over
    _approve(world, a, d)
    assert work.implement_gate(a, d)["allowed"]


def test_auto_pr_self_approves(world):
    world.backend.add("T9", title="x", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T9"]
    assert work.submit_plan(a, d, "plan") == "approved"
    assert world.backend.comments(TaskRef("T9")) == []


def test_unknown_human_cannot_approve(claimed):
    world, a, d, wt = claimed
    work.submit_plan(a, d, "plan")
    from dags.config import ConfigError
    mallory = world.machine("mal-mac", human="mallory")
    with pytest.raises(ConfigError):
        work.approve_plan(mallory, rv.index(mallory.root)["T1"])


def test_notes_accumulate(claimed):
    world, a, d, wt = claimed
    work.note(a, d, tried=["a"], risks=["r1"])
    work.note(a, d, tried=["b", "a"], remaining=["x", "y"], questions=["q?"])
    cp = work.note(a, d, summary="does the thing", remaining=["y"])
    assert cp["tried"] == ["a", "b"] and cp["remaining"] == ["y"]
    assert cp["open_questions"] == ["q?"] and cp["risks"] == ["r1"] and cp["summary"] == "does the thing"


def test_block_asks_a_human(claimed):
    world, a, d, wt = claimed
    work.block(a, d, "store cancelled matches?")
    cp = L.read_checkpoint(d)
    assert cp["needs_human"] == "store cancelled matches?"
    assert "store cancelled matches?" in cp["open_questions"]
    assert "needs a human decision" in world.backend.comments(TaskRef("T1"))[-1]


def test_non_owner_cannot_write(claimed):
    world, a, d, wt = claimed
    b = world.machine("mac-b")
    with pytest.raises(L.LostClaim):
        work.note(b, rv.index(b.root)["T1"], summary="hijack")


def test_render_templates(claimed):
    world, a, d, wt = claimed
    commit, body = work.render(a, d, {"summary": "Adds a poller", "risks": ["rate limits"], "open_questions": []})
    assert commit.splitlines()[0] == "[T1] Adds a poller"
    assert "Issue: fake://T1" in commit and "Coordination-ref: tasks/E1/T1" in commit
    assert "## Risks\n- rate limits" in body and "## Open questions\nNone." in body


def _ready_to_finish(world, a, d, wt):
    work.submit_plan(a, d, "plan")
    _approve(world, a, d)
    (wt / "poller.py").write_text("print('poll')\n")
    work.note(a, d, summary="Adds a 60s poller", risks=["rate limits"])


def test_done_refuses_without_approval_summary_or_changes(claimed):
    world, a, d, wt = claimed
    with pytest.raises(work.WorkError, match="plan"):
        work.finish(a, d, wt)
    work.submit_plan(a, d, "plan")
    _approve(world, a, d)
    with pytest.raises(work.WorkError, match="summary"):
        work.finish(a, d, wt)
    work.note(a, d, summary="nothing yet")
    with pytest.raises(work.WorkError, match="no changes"):
        work.finish(a, d, wt)


def test_done_refuses_on_failing_tests(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    failing = lambda *a_, **k: subprocess.CompletedProcess("x", 1, "3 tests failed", "")  # noqa: E731
    with pytest.raises(work.WorkError, match="tests failed"):
        work.finish(a, d, wt, test_runner=failing)
    assert world.prs.prs == {}
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"


def test_done_fulfils_the_output_contract(claimed, monkeypatch):
    world, a, d, wt = claimed
    monkeypatch.setenv("DAGS_WORKER_GH_TOKEN", "bot-token")
    (a.swarm_dir / "local.yaml").write_text((a.swarm_dir / "local.yaml").read_text().replace(
        "worker_token: none", "worker_token:\n  env: DAGS_WORKER_GH_TOKEN"))
    _ready_to_finish(world, a, d, wt)
    ran = []
    url = work.finish(a, d, wt, test_runner=lambda cmd, **kw: ran.append(cmd) or
                      subprocess.CompletedProcess(cmd, 0, "", ""))
    assert ran == ["true"]
    assert url == "https://github.com/OWNER/app/pull/1"
    # commit: house template, bot author, .swarm-task never committed
    log = sh(["git", "log", "-1", "--format=%an <%ae>%n%B"], wt)
    assert log.startswith("dags-bot <1+dags-bot@users.noreply.github.com>\n[T1] Adds a 60s poller")
    files = sh(["git", "show", "--name-only", "--format=", "HEAD"], wt).split()
    assert files == ["poller.py"]
    # pushed to the code remote
    assert "swarm/T1" in sh(["git", "branch", "-a"], world.code_remote)
    # PR opened with the bot token and the rendered template
    create = [c for c in world.prs.calls if c["args"][:2] == ["pr", "create"]][0]
    assert create["token"] == "bot-token"
    pr = world.prs.get(url)
    assert pr["title"] == "[T1] Poll the feed" and pr["base"] == "main"
    assert pr["body"].startswith("## Summary\nAdds a 60s poller")
    # ledger + backend
    a.coord.pull()
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"
    out = rv.read_outcome(d)
    assert out.kind == "pr-opened" and out.pr_url == url and out.record["worker"] == "claude"
    assert L.read_checkpoint(d)["pr_url"] == url
    assert world.backend.get_task(TaskRef("T1")).status == "awaiting-review"
    assert world.backend.comments(TaskRef("T1"))[-1].endswith(f"PR: {url}\n")


def test_done_after_changes_requested_updates_the_same_pr(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
    url = work.finish(a, d, wt, test_runner=ok)
    L.complete(a, d, "reopened", pr_url=url, review_id="R1")
    world.scheduler(a, worker="claude", run_plan_sync=False).cycle()     # re-claims and resumes
    cid = rv.resolve(d, timeutil.now(), 900).winner.id
    assert L.read_checkpoint(d)["claim_id"] == cid
    world.prs.get(url)["reviews"].append({"author": {"login": "jane"}, "state": "CHANGES_REQUESTED",
                                         "body": "fix: handle null scores", "submittedAt": "t"})
    fb = work.implement_gate(a, d)["feedback"]
    assert fb == [{"tag": "fix", "text": "handle null scores", "author": "jane", "at": "t",
                   "state": "CHANGES_REQUESTED"}]
    (wt / "poller.py").write_text("print('poll, null-safe')\n")
    url2 = work.finish(a, d, wt, test_runner=ok)
    assert url2 == url and len(world.prs.prs) == 1
    assert "Updated by the swarm worker" in world.prs.get(url)["comments"][-1]["body"]
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"


def test_loss_before_push_stops_done(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    jane = world.machine("jane-mac", human="jane")
    L.arbitrate(jane, rv.index(jane.root)["T1"], "none", "stop, spec changed")
    with pytest.raises(L.LostClaim):
        work.finish(a, d, wt, skip_tests=True)
    assert world.prs.prs == {}
    assert "swarm/T1" not in sh(["git", "branch", "-a"], world.code_remote)
