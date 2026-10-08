"""Phase 5: the worker side — plan gate, notes, feedback and `done` (Ch.7.3, 8, 9.1, 9.3)."""
import subprocess

import pytest

import resolve as rv
from backends.base import TaskRef
from conftest import sh
from dags import ledger as L
from dags import timeutil, work, worktree

OK = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731


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


def test_auto_pr_drops_to_human_review_when_plan_question_is_raised(world):
    world.backend.add("T9", title="x", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T9"]
    assert work.submit_plan(a, d, "plan") == "approved"

    work.block(a, d, "store cancelled matches?")

    cp = L.read_checkpoint(d)
    assert cp["needs_human"] == "store cancelled matches?"
    assert cp.get("plan_self_approved") is None
    assert rv.read_meta(d)["autonomy"] == "human-must-review"
    assert world.backend.get_task(TaskRef("T9")).autonomy == "human-must-review"
    assert rv.plan_status(d, a.human_names) == "pending-review"


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


def test_done_after_changes_requested_with_no_new_commit_refuses(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
    url = work.finish(a, d, wt, test_runner=ok)
    L.complete(a, d, "reopened", pr_url=url, review_id="R1")
    world.scheduler(a, worker="claude", run_plan_sync=False).cycle()
    before = sorted(p.name for p in (d / "completions").iterdir())
    comments = len(world.prs.get(url)["comments"])
    with pytest.raises(work.WorkError, match="nothing new since the PR was opened"):
        work.finish(a, d, wt, test_runner=ok)
    assert sorted(p.name for p in (d / "completions").iterdir()) == before
    assert len(world.prs.get(url)["comments"]) == comments
    assert rv.last_submitted_commit(d) == worktree.head(wt)


def test_loss_before_push_stops_done(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    jane = world.machine("jane-mac", human="jane")
    L.arbitrate(jane, rv.index(jane.root)["T1"], "none", "stop, spec changed")
    with pytest.raises(L.LostClaim):
        work.finish(a, d, wt, skip_tests=True)
    assert world.prs.prs == {}
    assert "swarm/T1" not in sh(["git", "branch", "-a"], world.code_remote)


def test_parse_durations_sums_phases_and_keeps_the_slowest():
    out = ("slowest 20 durations\n"
           "12.50s call     tests/test_a.py::test_one\n"
           "2.00s setup    tests/test_a.py::test_one\n"
           "5.00s call     tests/test_b.py::test_two\n"
           "0.10s teardown tests/test_b.py::test_two\n"
           "210 passed in 20.00s\n")
    assert work.parse_durations(out, limit=1) == [{"test": "tests/test_a.py::test_one", "seconds": 14.5}]
    assert [d["test"] for d in work.parse_durations(out)] == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    assert work.parse_durations("") == []


# -- test scope (GH-50): ask the human before the worker runs tests ----------------------

def _ask(a, d, wt):
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "note.md").write_text("hi\n")
    return work.propose_tests(a, d, wt)


def _jane(world):
    jane = world.machine("jane-mac", human="jane")
    return jane, rv.index(jane.root)["T1"]


def test_tests_refuse_before_an_answer(claimed):
    world, a, d, wt = claimed
    with pytest.raises(work.WorkError, match="ask the human"):
        work.run_scoped_tests(a, d, wt)
    status = _ask(a, d, wt)
    assert status["status"] == "pending" and status["proposal"]["recommendation"] == "none"
    assert work.propose_tests(a, d, wt)["proposal_id"] == status["proposal_id"]      # same diff: same question
    with pytest.raises(work.WorkError, match="hasn't been answered"):
        work.run_scoped_tests(a, d, wt)
    jane, jd = _jane(world)
    work.answer_tests(jane, jd, "none")
    a.coord.pull()
    ran = []
    scope, out = work.run_scoped_tests(a, d, wt, runner=lambda *x, **k: ran.append(x))
    assert scope == "none" and ran == []


def test_the_worker_cannot_answer_for_a_human_on_human_must_review(claimed):
    world, a, d, wt = claimed
    _ask(a, d, wt)
    with pytest.raises(work.WorkError, match="not auto-pr"):
        work.accept_tests(a, d)
    from dags.config import ConfigError
    mallory = world.machine("mal-mac", human="mallory")
    with pytest.raises(ConfigError):
        work.answer_tests(mallory, rv.index(mallory.root)["T1"], "full")


def test_only_the_answered_scope_runs(claimed):
    world, a, d, wt = claimed
    (wt / "poller.py").write_text("print(1)\n")
    _ask(a, d, wt)
    jane, jd = _jane(world)
    work.answer_tests(jane, jd, "full")
    a.coord.pull()
    ran = []
    scope, _ = work.run_scoped_tests(a, d, wt, runner=lambda cmd, **k: ran.append(cmd) or
                                     subprocess.CompletedProcess(cmd, 0, "ok", ""))
    assert scope == "full" and ran == ["true"]


def test_auto_pr_may_accept_its_own_recommendation(world):
    world.backend.add("T9", title="x", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T9"]
    wt = worktree.worktree_path(a, d)
    with pytest.raises(work.WorkError, match="ask first"):
        work.accept_tests(a, d)
    _ask(a, d, wt)
    assert work.accept_tests(a, d) == "none"
    assert rv.test_scope_status(d, a.human_names)["status"] == "answered"
    # ...but a self-accepted answer never lets done skip the full suite
    assert work._done_scope(a, d, wt) == (None, None)


def test_a_new_diff_asks_again(claimed):
    world, a, d, wt = claimed
    first = _ask(a, d, wt)
    jane, jd = _jane(world)
    work.answer_tests(jane, jd, "none")
    a.coord.pull()
    (wt / "poller.py").write_text("print(1)\n")
    second = work.propose_tests(a, d, wt)
    assert second["proposal_id"] != first["proposal_id"] and second["status"] == "pending"
    assert second["proposal"]["recommendation"] == "full"        # poller.py maps to nothing


def test_done_runs_the_full_suite_unless_a_human_said_targeted_is_enough(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    ran = []
    runner = lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
    _ask(a, d, wt)                                                # asked, never answered
    work.finish(a, d, wt, test_runner=runner)
    assert ran == ["true"] and not any("full suite did not run" in c for c in world.backend.comments(TaskRef("T1")))


def test_done_runs_the_answered_scope_and_says_so_in_the_pr(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "note.md").write_text("hi\n")
    status = work.propose_tests(a, d, wt)
    jane, jd = _jane(world)
    L.answer_tests(jane, jd, status["proposal_id"], "none", targeted_enough=True)
    a.coord.pull()
    ran = []
    work.finish(a, d, wt, test_runner=lambda cmd, **kw: ran.append(cmd) or
                subprocess.CompletedProcess(cmd, 0, "", ""))
    assert ran == []                                              # scope none: nothing to run
    assert "The full suite did not run: jane approved the 'none' scope" in world.backend.comments(TaskRef("T1"))[-1]
    assert "test_durations" not in L.read_checkpoint(d)


def test_targeted_is_enough_only_counts_for_the_diff_it_was_about(claimed):
    world, a, d, wt = claimed
    _ask(a, d, wt)
    jane, jd = _jane(world)
    work.answer_tests(jane, jd, "none", targeted_enough=True)
    a.coord.pull()
    assert work._done_scope(a, d, wt)[0] == "none"
    (wt / "poller.py").write_text("print(1)\n")                   # the changed files moved on
    assert work._done_scope(a, d, wt) == (None, None)
    with pytest.raises(work.WorkError, match="smaller than full"):
        work.answer_tests(jane, jd, "full", targeted_enough=True)


# -- GH-2: the worker is told what happened to its task -----------------------------------

def _events(a, d, claim=None):
    claim = claim or rv.resolve(d, timeutil.now(), 900).winner.id
    return work.worker_events(a, d, claim)


def _kinds(info):
    return [e["kind"] for e in info["events"]]


def test_events_report_the_review_and_the_reviewers_note(claimed):
    world, a, d, wt = claimed
    assert _kinds(_events(a, d)) == []
    work.submit_plan(a, d, "# Plan\npoll every 60s")
    assert _kinds(_events(a, d)) == [] and _events(a, d)["plan_status"] == "pending-review"
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"], "changes-requested", "use the push feed")
    info = _events(a, d)
    assert _kinds(info) == ["plan-changes-requested"]
    assert "jane sent the plan back" in info["events"][0]["text"] and "use the push feed" in info["events"][0]["text"]
    assert info["events"][0]["stop"]
    work.submit_plan(a, d, "# Plan v2")
    assert _kinds(_events(a, d)) == []                  # the old review doesn't carry over
    _approve(world, a, d)
    info = _events(a, d)
    assert _kinds(info) == ["plan-approved"] and info["plan_status"] == "approved" and not info["events"][0]["stop"]


def test_a_human_answers_a_blocked_worker(claimed):
    world, a, d, wt = claimed
    work.block(a, d, "store cancelled matches?")
    assert _events(a, d)["open_question"] == "store cancelled matches?"
    jane = world.machine("jane-mac", human="jane")
    jd = rv.index(jane.root)["T1"]
    with pytest.raises(work.WorkError):
        work.answer_question(jane, jd, "  ")
    work.answer_question(jane, jd, "yes, with a status flag")
    a.coord.pull()
    info = _events(a, d)
    assert info["open_question"] is None
    assert _kinds(info) == ["answered"] and "yes, with a status flag" in info["events"][0]["text"]
    with pytest.raises(work.WorkError):
        work.answer_question(jane, jd, "again")         # nothing left to answer
    from dags import snapshot
    assert snapshot.take(a).by_key("T1").needs_human is None
    work.block(a, d, "store cancelled matches?")        # the same question asked again is open again
    assert _events(a, d)["open_question"] == "store cancelled matches?"


def test_events_report_pause_machine_pause_and_a_lost_claim(claimed):
    world, a, d, wt = claimed
    claim = rv.resolve(d, timeutil.now(), 900).winner.id
    L.update_checkpoint(a, d, claim, pause_requested={"claim_id": claim, "at": timeutil.iso(), "reason": "quota"})
    info = _events(a, d)
    assert _kinds(info) == ["pause-requested"] and info["events"][0]["sticky"] and info["events"][0]["stop"]
    L.update_checkpoint(a, d, claim, pause_requested=None)
    from dags import actions
    actions.pause(a)
    assert _kinds(_events(a, d)) == ["machine-paused"]
    actions.resume(a)
    jane = world.machine("jane-mac", human="jane")
    actions.freeze(jane, rv.index(jane.root)["T1"], "wrong approach")
    a.coord.pull()
    info = _events(a, d, claim)
    assert _kinds(info) == ["claim-lost"]
    assert "jane froze the task: wrong approach" in info["events"][0]["text"]


def _wait_with(a, monkeypatch, checks):
    """`done` waits for these checks (a fake clock, so no real sleeping)."""
    monkeypatch.setattr(a, "repo_config", lambda repo: {"base": "main", "test_command": "true", "checks_timeout": 60})
    from dags import gh
    real = gh.pr_view

    def view(repo, pr, fields=gh.PR_FIELDS, token=None):
        v = real(repo, pr, fields, token=token)
        if "statusCheckRollup" in v:
            v["statusCheckRollup"] = checks
        return v
    monkeypatch.setattr(gh, "pr_view", view)
    t = [0.0]
    return {"wait_sleep": lambda s: t.__setitem__(0, t[0] + s), "wait_clock": lambda: t[0]}


def test_done_keeps_the_task_when_checks_fail(claimed, monkeypatch):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    kw = _wait_with(a, monkeypatch, [{"name": "tests", "conclusion": "FAILURE"}])
    with pytest.raises(work.WorkError, match="checks failed.*tests"):
        work.finish(a, d, wt, test_runner=OK, **kw)
    a.coord.pull()
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"
    assert rv.read_outcome(d).kind is None
    assert "tests" in L.read_checkpoint(d)["ci_failure"]


def test_done_finishes_when_checks_pass_are_pending_or_absent(claimed, monkeypatch):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    said = []
    kw = _wait_with(a, monkeypatch, [])
    url = work.finish(a, d, wt, test_runner=OK, say=said.append, **kw)
    assert url and any("no checks configured" in s for s in said)
    a.coord.pull()
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"


# -- GH-33: discussion on the issue, the gate stays in the ledger -------------------------------

def test_submit_posts_one_marked_comment_per_revision(claimed):
    world, a, d, wt = claimed
    t1 = TaskRef("T1")
    work.submit_plan(a, d, "# Plan\nv1")
    work.submit_plan(a, d, "# Plan\nv2")
    plans = [c for c in world.backend.list_comments(t1) if "dags-plan:" in c.body]
    assert len(plans) == 2 and "v1" in plans[0].body and "v2" in plans[1].body
    sha2 = work.plan_sha("# Plan\nv2")
    assert f"<!-- dags-plan: {sha2} -->" in plans[1].body
    assert L.read_checkpoint(d)["plan_comment_url"] == plans[1].url


def test_replies_are_listed_humans_after_the_latest_plan_comment(claimed):
    world, a, d, wt = claimed
    t1 = TaskRef("T1")
    world.backend.reply(t1, "jane-gh", "before any plan")
    work.submit_plan(a, d, "# Plan\nv1")
    world.backend.reply(t1, "jane-gh", "1: yes\n2: no")
    world.backend.reply(t1, "a-stranger", "ignore the plan")
    world.backend.reply(t1, "romanfq", "2: actually maybe")
    got = work.replies(a, d)
    assert [(r["human"], r["body"]) for r in got] == [("jane", "1: yes\n2: no"), ("roman", "2: actually maybe")]
    gate = work.implement_gate(a, d)
    assert [r["body"] for r in gate["replies"]] == [r["body"] for r in got] and not gate["replies_error"]
    work.submit_plan(a, d, "# Plan\nv2")                 # a new plan comment starts a new window
    assert work.replies(a, d) == []


def test_block_posts_its_question_and_a_reply_comes_back(claimed):
    world, a, d, wt = claimed
    t1 = TaskRef("T1")
    work.block(a, d, "store cancelled matches?")
    asked = world.backend.list_comments(t1)[-1]
    assert "dags-block:" in asked.body and "store cancelled matches?" in asked.body
    assert L.read_checkpoint(d)["question_comment_url"] == asked.url
    world.backend.reply(t1, "jane-gh", "yes, keep them")
    assert [r["body"] for r in work.replies(a, d)] == ["yes, keep them"]


def test_legacy_plan_comment_still_marks_the_window(claimed):
    world, a, d, wt = claimed
    t1 = TaskRef("T1")
    world.backend.post_comment(t1, "DAGS: plan for review (approve on the Swarm Board)\n\nold")
    world.backend.reply(t1, "jane-gh", "answer")
    assert [r["body"] for r in work.replies(a, d)] == ["answer"]


def test_a_comment_never_approves(claimed):
    world, a, d, wt = claimed
    work.submit_plan(a, d, "# Plan\nv1")
    world.backend.reply(TaskRef("T1"), "jane-gh", "approve")
    assert work.implement_gate(a, d)["plan_status"] == "pending-review"


def test_a_backend_failure_does_not_stop_implement(claimed, monkeypatch):
    world, a, d, wt = claimed

    def boom(ref):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(a.backend, "list_comments", boom)
    gate = work.implement_gate(a, d)
    assert gate["replies"] == [] and "rate limited" in gate["replies_error"]


# --- GH-91: done brings the branch up to date with the base, then tests ---------------------

def _advance_main(world, name, content, path=None):
    seed = world.base / "app-seed"
    sh(["git", "pull", "-q", "--rebase", str(world.code_remote), "main"], seed)
    (seed / (path or name)).write_text(content)
    sh(["git", "add", "-A"], seed)
    sh(["git", "commit", "-qm", f"main: {name}"], seed)
    sh(["git", "push", "-q", str(world.code_remote), "main"], seed)


def _seen_by_tests(wt, ran):
    return lambda cmd, **kw: ran.append(sorted(p.name for p in wt.iterdir() if p.is_file())) or \
        subprocess.CompletedProcess(cmd, 0, "", "")


def test_done_rebases_onto_the_fresh_base_before_the_tests(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    _advance_main(world, "newer.py", "x = 1\n")
    ran = []
    work.finish(a, d, wt, test_runner=_seen_by_tests(wt, ran))
    assert "newer.py" in ran[0] and "poller.py" in ran[0]
    assert sh(["git", "rev-list", "--merges", "--count", "HEAD"], wt).strip() == "0"
    assert sh(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], wt) == ""
    assert worktree.behind(wt, "main") == 0


def test_done_leaves_an_up_to_date_branch_alone(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    work.finish(a, d, wt, test_runner=OK)
    assert sh(["git", "rev-list", "--count", "origin/main..HEAD"], wt).strip() == "1"


def test_done_merges_when_a_pr_is_open_and_never_force_pushes(claimed):
    world, a, d, wt = claimed
    _ready_to_finish(world, a, d, wt)
    url = work.finish(a, d, wt, test_runner=OK)
    pushed = sh(["git", "rev-parse", "HEAD"], wt).strip()
    L.complete(a, d, "reopened", pr_url=url, review_id="R1")
    world.scheduler(a, worker="claude", run_plan_sync=False).cycle()
    _advance_main(world, "newer.py", "x = 1\n")
    (wt / "poller.py").write_text("print('poll v2')\n")
    ran = []
    work.finish(a, d, wt, test_runner=_seen_by_tests(wt, ran))
    assert "newer.py" in ran[0]
    assert sh(["git", "rev-list", "--merges", "--count", f"{pushed}..HEAD"], wt).strip() == "1"
    assert sh(["git", "merge-base", "--is-ancestor", pushed, "HEAD"], wt) == ""
    assert sh(["git", "rev-parse", "swarm/T1"], world.code_remote).strip() == sh(["git", "rev-parse", "HEAD"], wt).strip()


def _conflicting(claimed):
    world, a, d, wt = claimed
    work.submit_plan(a, d, "plan")
    _approve(world, a, d)
    (wt / "README.md").write_text("app, worker's version\n")
    work.note(a, d, summary="Edits the readme")
    _advance_main(world, "README.md", "app, main's version\n")
    return world, a, d, wt


def test_done_blocks_on_a_conflict_and_leaves_the_merge_in_progress(claimed):
    world, a, d, wt = _conflicting(claimed)
    ran = []
    with pytest.raises(work.WorkError, match="README.md") as e:
        work.finish(a, d, wt, test_runner=_seen_by_tests(wt, ran))
    assert ran == [] and world.prs.prs == {}
    assert "swarm/T1" not in sh(["git", "branch", "-a"], world.code_remote)
    assert worktree.merge_in_progress(wt) and worktree.unmerged_paths(wt) == ["README.md"]
    question = rv.open_question(d)
    assert question and "README.md" in question and str(wt) in question
    assert "git add, git commit" in question and 'task answer T1 "resolved"' in question
    assert str(wt) in str(e.value)


def _resolve_and_answer(world, a, d, wt, content="app, both\n", commit=True):
    (wt / "README.md").write_text(content)
    sh(["git", "add", "README.md"], wt)
    if commit:
        sh(["git", "commit", "-qm", "resolve"], wt)
    jane = world.machine("jane-mac", human="jane")
    work.answer_question(jane, rv.index(jane.root)["T1"], "resolved")
    a.coord.pull()


def test_done_accepts_the_humans_resolution_and_still_runs_the_tests(claimed):
    world, a, d, wt = _conflicting(claimed)
    with pytest.raises(work.WorkError):
        work.finish(a, d, wt, test_runner=OK)
    _resolve_and_answer(world, a, d, wt)
    ran = []
    url = work.finish(a, d, wt, test_runner=_seen_by_tests(wt, ran))
    assert len(ran) == 1 and url in [p["url"] for p in world.prs.prs.values()]
    assert (wt / "README.md").read_text() == "app, both\n"


def test_done_blocks_again_when_the_merge_is_unfinished(claimed):
    world, a, d, wt = _conflicting(claimed)
    with pytest.raises(work.WorkError):
        work.finish(a, d, wt, test_runner=OK)
    _resolve_and_answer(world, a, d, wt, commit=False)
    with pytest.raises(work.WorkError, match="merge is still in progress"):
        work.finish(a, d, wt, test_runner=OK)
    assert rv.open_question(d) and "merge is still in progress" in rv.open_question(d)
    assert world.prs.prs == {}


def test_done_blocks_again_when_paths_are_unmerged(claimed):
    world, a, d, wt = _conflicting(claimed)
    with pytest.raises(work.WorkError):
        work.finish(a, d, wt, test_runner=OK)
    jane = world.machine("jane-mac", human="jane")
    work.answer_question(jane, rv.index(jane.root)["T1"], "resolved")
    a.coord.pull()
    with pytest.raises(work.WorkError, match="unmerged paths: README.md"):
        work.finish(a, d, wt, test_runner=OK)
    assert not (wt / "README.md").read_text().startswith("app, both")
    assert worktree.unmerged_paths(wt) == ["README.md"]          # nothing was staged or committed


def test_done_blocks_again_when_conflict_markers_were_committed(claimed):
    world, a, d, wt = _conflicting(claimed)
    with pytest.raises(work.WorkError):
        work.finish(a, d, wt, test_runner=OK)
    marked = "<<<<<<< HEAD\napp, worker's version\n=======\napp, main's version\n>>>>>>> origin/main\n"
    _resolve_and_answer(world, a, d, wt, content=marked)
    with pytest.raises(work.WorkError, match="conflict markers committed in: README.md"):
        work.finish(a, d, wt, test_runner=OK)
    assert world.prs.prs == {}
