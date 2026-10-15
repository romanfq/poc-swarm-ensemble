"""GH-81: one task, several PRs. The Nth `done` is the last one; the others keep the claim and the slot."""
import subprocess

import pytest

import resolve as rv
from backends.base import TaskRef
from dags import actions, snapshot, timeutil, work, worktree
from dags import ledger as L

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


def _start(world, a, d, wt, parts):
    work.submit_plan(a, d, f"plan\nparts: {parts}\n")
    jane = world.machine("jane-mac", human="jane")
    work.approve_plan(jane, rv.index(jane.root)["T1"])
    a.coord.pull()
    work.note(a, d, summary="Adds a poller")


def _change(wt, text):
    (wt / "poller.py").write_text(text)


def _merge(world, a, d, url):
    world.prs.get(url)["state"] = "MERGED"
    actions.record_merged(a, d, url)


def test_parse_parts():
    assert work.parse_parts("no declaration") == 1
    assert work.parse_parts("# Plan\nparts: 3\n") == 3
    assert work.parse_parts("parts: 3 # three PRs") == 3          # a trailing comment is ignored
    assert work.parse_parts("parts: 03") == 3
    for bad in ("parts: 0", "parts: many", "parts: -2"):
        with pytest.raises(work.WorkError, match="positive"):
            work.parse_parts(bad)


def test_the_plan_records_parts_in_the_ledger(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    assert L.read_checkpoint(d)["parts"] == 3 and rv.parts_declared(d) == 3
    work.submit_plan(a, d, "a plan with no declaration")
    assert rv.parts_declared(d) == 1


def test_the_constants_that_decide_the_slot_are_untouched():
    assert rv.TERMINAL_HOLD == {"pr-opened", "done", "rejected"}
    assert rv.ACTIVE_STATES == {"claimed", "in-progress"}


def test_a_part_keeps_the_claim_the_slot_and_in_progress(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    said = []
    url = work.finish(a, d, wt, test_runner=OK, say=said.append)
    a.coord.pull()
    out = rv.read_outcome(d)
    assert out.kind == "pr-part-opened" and out.pr_url == url
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"
    assert world.backend.get_task(TaskRef("T1")).status == "in-progress"
    assert not rv.ledger_ready(a.root, d, timeutil.now(), 900)
    assert rv.part_label(d) == "part 1 of 3"
    assert any("PR 1 of 3 opened" in s for s in said)
    snap = snapshot.take(a)
    assert snap.quota_used == 1 and [t.short for t in snap.in_review] == ["T1"]
    assert snap.by_key("T1").part == "part 1 of 3"
    assert L.read_checkpoint(d)["pr_urls"] == [url]


def test_the_next_part_waits_for_the_previous_one_to_merge(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    url1 = work.finish(a, d, wt, test_runner=OK)
    world.prs.get(url1)["state"] = "MERGED"                      # merged on GitHub, not yet recorded
    _change(wt, "two\n")
    with pytest.raises(work.WorkError, match="not been merged"):
        work.finish(a, d, wt, test_runner=OK)
    assert work.part_pr_state(d)["merged"] is False
    actions.record_merged(a, d, url1)
    assert work.part_pr_state(d)["merged"] is True
    assert not rv.is_done(d) and world.backend.get_task(TaskRef("T1")).status != "done"
    actions.record_merged(a, d, url1)                            # idempotent
    assert len([p for p in (d / "completions").iterdir() if "part-merged" in p.name]) == 1


def test_the_nth_done_is_the_last_and_its_merge_ends_the_task(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 2)
    _change(wt, "one\n")
    url1 = work.finish(a, d, wt, test_runner=OK)
    _merge(world, a, d, url1)
    _change(wt, "two\n")
    url2 = work.finish(a, d, wt, test_runner=OK)
    assert url2 != url1
    a.coord.pull()
    assert rv.read_outcome(d).kind == "pr-opened"
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"
    assert world.backend.get_task(TaskRef("T1")).status == "awaiting-review"
    assert [r["pr_url"] for r in rv.pr_records(d)] == [url1, url2]
    assert L.read_checkpoint(d)["pr_urls"] == [url1, url2]
    assert not rv.is_done(d)
    _merge(world, a, d, url2)
    assert rv.is_done(d)


def test_one_part_default_is_unchanged(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 1)
    _change(wt, "one\n")
    work.finish(a, d, wt, test_runner=OK)
    assert rv.read_outcome(d).kind == "pr-opened"
    assert rv.task_state(d, timeutil.now(), 900) == "awaiting-review"


def test_an_empty_round_is_refused_across_parts(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    url1 = work.finish(a, d, wt, test_runner=OK)
    with pytest.raises(work.WorkError, match="nothing new since the PR was opened"):
        work.finish(a, d, wt, test_runner=OK)
    _merge(world, a, d, url1)
    with pytest.raises(work.WorkError, match="no changes|nothing new"):
        work.finish(a, d, wt, test_runner=OK)


def test_changes_requested_on_a_part_updates_that_part(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    url = work.finish(a, d, wt, test_runner=OK)
    _change(wt, "one, fixed\n")
    assert work.finish(a, d, wt, test_runner=OK) == url
    assert len(world.prs.prs) == 1
    assert rv.parts_opened(d) == 1 and rv.part_label(d) == "part 1 of 3"
    assert rv.read_outcome(d).kind == "pr-part-opened"


def test_a_rejected_part_sends_the_worker_back_to_planning(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    url = work.finish(a, d, wt, test_runner=OK)
    L.complete(a, d, "rejected", pr_url=url, part="part 1 of 3")
    a.coord.pull()
    assert rv.read_outcome(d).kind == "replanned"
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"
    assert rv.read_outcome(d).kind not in rv.TERMINAL_HOLD
    info = work.worker_events(a, d, rv.resolve(d, timeutil.now(), 900).winner.id)
    rejected = [e for e in info["events"] if e["kind"] == "part-rejected"]
    assert rejected and rejected[0]["stop"] and "Re-plan" in rejected[0]["text"]


def test_the_worker_hears_about_a_merge_and_about_feedback(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 3)
    _change(wt, "one\n")
    url = work.finish(a, d, wt, test_runner=OK)
    L.complete(a, d, "part-changes-requested", pr_url=url, review_id="R1")
    claim = rv.resolve(d, timeutil.now(), 900).winner.id
    assert "part-feedback" in [e["kind"] for e in work.worker_events(a, d, claim)["events"]]
    _merge(world, a, d, url)
    info = work.worker_events(a, d, claim)
    assert "part-merged" in [e["kind"] for e in info["events"]]
    assert info["part_pr"] == {"url": url, "merged": True, "part": "part 1 of 3"}


def test_part_pr_state_is_none_for_a_single_pr_task(claimed):
    world, a, d, wt = claimed
    assert work.part_pr_state(d) is None


def test_the_feed_describes_a_part_pr(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 2)
    _change(wt, "one\n")
    work.finish(a, d, wt, test_runner=OK)
    assert rv.read_outcome(d).kind == "pr-part-opened"
    from dags import feed
    texts = [e.text for e in feed.all_events(a.root)]
    assert any("opened PR 1 of 2" in t for t in texts)


def test_pr_records_has_one_dict_per_url_in_the_order_opened(claimed):
    world, a, d, wt = claimed
    A, B = "https://github.com/OWNER/app/pull/1", "https://github.com/OWNER/app/pull/2"
    L.complete(a, d, "pr-part-opened", pr_url=A, commit="c1")
    L.complete(a, d, "pr-part-opened", pr_url=B, commit="c2")
    L.complete(a, d, "pr-part-opened", pr_url=A, commit="c3")      # an update to the first part
    records = rv.pr_records(d)
    assert [r["pr_url"] for r in records] == [A, B]
    assert records[0]["commit"] == "c3"                             # the newest record of that PR
    assert rv.parts_opened(d) == 2


def test_only_a_plan_submission_changes_parts(claimed):
    world, a, d, wt = claimed
    work.submit_plan(a, d, "plan\nparts: 3\n")
    work.note(a, d, summary="progress")
    assert rv.parts_declared(d) == 3
    work.submit_plan(a, d, "plan\nparts: 2\n")                   # a re-plan
    assert rv.parts_declared(d) == 2 and L.read_checkpoint(d)["parts"] == 2
    work.note(a, d, summary="more progress", tried=["x"])
    assert rv.parts_declared(d) == 2


def test_ledger_readers_tolerate_an_unknown_completion_kind(claimed):
    world, a, d, wt = claimed
    _start(world, a, d, wt, 2)
    _change(wt, "one\n")
    url = work.finish(a, d, wt, test_runner=OK)
    L.complete(a, d, "a-kind-from-the-future", pr_url=url)
    a.coord.pull()
    assert rv.read_outcome(d).kind == "pr-part-opened"
    assert rv.task_state(d, timeutil.now(), 900) == "in-progress"
    assert [r["pr_url"] for r in rv.pr_records(d)] == [url]
    assert rv.last_submitted_commit(d) == worktree.head(wt)
    snapshot.take(a)                                               # the Board's reader copes too
