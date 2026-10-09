"""Jira adapter specifics beyond the shared contract (GH-123): lag, labels, links, workflow, comments."""
import json

import pytest

from backends.base import TaskRef, TrackerHelpers
from backends.jira import JiraBackend, chunk_markdown, utc_iso
from dags import seed
from dags.jira_client import JiraError
from fakes import FakeJira


def make(fake=None, tmp_path=None, **kw):
    fake = fake or FakeJira()
    kw.setdefault("cache_seconds", 0)
    if tmp_path is not None:
        kw.setdefault("recent_path", tmp_path / "jira-recent.json")
    return fake, JiraBackend(fake, "KAN", "https://acme.atlassian.net/", **kw)


def test_has_the_tracker_helpers_and_skips_type_labels():
    _, b = make()
    assert isinstance(b, TrackerHelpers)
    assert b.uses_type_labels is False and b.epics_carry_status is False and b.lagging_search is True
    assert b.init_commands(["org/app"]) == [] and b.required_labels(["org/app"]) == []


@pytest.mark.parametrize("text,key", [("KAN-7", "KAN-7"), ("kan-7", "KAN-7"), ("7", "KAN-7"), ("#7", "KAN-7"),
                                      ("https://acme.atlassian.net/browse/KAN-7", "KAN-7"),
                                      ("https://acme.atlassian.net/browse/KAN-7?focusedCommentId=9", "KAN-7")])
def test_parse_ref(text, key):
    assert make()[1].parse_ref(text) == TaskRef(key)


def test_parse_ref_rejects_other_projects_and_junk():
    for bad in ("OTHER-7", "org/repo#7", "seven"):
        with pytest.raises(ValueError):
            make()[1].parse_ref(bad)


def test_short_key_and_url():
    _, b = make()
    assert b.short_key(TaskRef("KAN-3")) == "KAN-3"
    assert b.web_url(TaskRef("KAN-3")) == "https://acme.atlassian.net/browse/KAN-3"
    assert b.coordination_ref(TaskRef("KAN-3")) == "KAN-3"


def test_epic_comes_from_the_hierarchy_level_not_the_name():
    fake, b = make()
    e = fake.add("E", "Epic", epic=True)
    fake.issues[e]["fields"]["issuetype"]["name"] = "Épico"          # localised
    t = fake.add("T", "Task named Epic", labels=["swarm:status:ready"])
    fake.issues[t]["fields"]["issuetype"]["name"] = "Epic"           # a name must not make it an epic
    assert b.get_task(TaskRef(e)).is_epic and not b.get_task(TaskRef(t)).is_epic


def test_a_task_with_a_swarm_labelled_subtask_is_a_container_never_claimed():
    fake, b = make()
    t = fake.add("T", "Issue 1", labels=["swarm:autonomy:auto-pr"])
    fake.add("S1", "sub", parent="T", subtask=True, labels=["swarm:status:ready"])
    assert b.get_task(TaskRef(t)).is_epic
    assert TaskRef(t) not in b.ready_tasks()


def test_unlabelled_subtask_does_not_turn_its_parent_into_an_epic():
    fake, b = make()
    t = fake.add("T", "A task", labels=["swarm:status:ready"])
    fake.add("S1", "sub", parent="T", subtask=True)
    assert not b.get_task(TaskRef(t)).is_epic and TaskRef(t) in b.ready_tasks()


def test_search_is_paged_by_token():
    fake, b = make()
    for i in range(8):
        fake.add(f"T{i}", f"t{i}", labels=["swarm:status:ready"])
    assert len(b.all_issues()) == 8
    assert len([c for c in fake.calls if c[0] == "search"]) == 3


def test_status_swap_removes_every_other_status_even_if_the_snapshot_is_stale():
    fake, b = make()
    k = fake.add("T", "t", labels=["swarm:status:ready"])
    b.all_issues()                                                    # snapshot says: ready
    fake.issues[k]["fields"]["labels"].append("swarm:status:blocked")   # a human just added blocked
    b.set_status(TaskRef(k), "claimed")
    statuses = [lb for lb in fake.issues[k]["fields"]["labels"] if lb.startswith("swarm:status:")]
    assert statuses == ["swarm:status:claimed"]
    ops = next(c for c in fake.calls if c[0] == "update_issue")[2]["update"]["labels"]
    assert len(ops) == 6 and {"add": "swarm:status:claimed"} in ops      # one request: add + 5 removes
    assert {"remove": "swarm:status:blocked"} in ops


def test_autonomy_swap_removes_the_other_tiers():
    fake, b = make()
    k = fake.add("T", "t", labels=["swarm:autonomy:auto-pr", "swarm:autonomy:self-approve", "swarm:autonomy:human-must-scope"])
    b.set_autonomy(TaskRef(k), "human-must-review")
    assert [lb for lb in fake.issues[k]["fields"]["labels"] if lb.startswith("swarm:autonomy:")] == \
        ["swarm:autonomy:human-must-review"]


def test_unrelated_labels_are_left_alone():
    fake, b = make()
    k = fake.add("T", "t", labels=["bug", "repo:org/app"])
    b.set_status(TaskRef(k), "ready")
    assert {"bug", "repo:org/app"} <= set(fake.issues[k]["fields"]["labels"])


def test_overlay_means_our_own_write_is_visible_while_search_lags(tmp_path):
    fake, b = make(tmp_path=tmp_path)
    k = fake.add("T", "t", labels=["swarm:status:ready"])
    b.invalidate()
    fake.lag = True
    b.set_status(TaskRef(k), "claimed")
    assert b.get_task(TaskRef(k)).status == "claimed"
    b.invalidate()                                                    # re-search: index still says ready
    assert b.get_task(TaskRef(k)).status == "claimed"                 # ...but reconcileIssues fixes that
    assert any(c[0] == "search" and int(fake.issues[k]["id"]) in c[2] for c in fake.calls)


def test_reconcile_ids_are_batched_by_50(tmp_path):
    fake, b = make(tmp_path=tmp_path)
    fake.add("T", "t")
    for i in range(120):
        b._note_written(20000 + i)
    b.all_issues()
    sent = [c[2] for c in fake.calls if c[0] == "search" and c[2]]
    assert sent and all(len(s) <= 50 for s in sent) and len({i for s in sent for i in s}) == 120


def test_recent_writes_are_persisted_and_expire(tmp_path):
    now = [1000.0]
    fake, b = make(tmp_path=tmp_path, clock=lambda: now[0])
    b._note_written(77)
    assert json.loads((tmp_path / "jira-recent.json").read_text()) == {"77": 1000.0}
    _, again = make(tmp_path=tmp_path, clock=lambda: now[0] + 10)
    assert again._recent_ids() == ["77"]
    _, later = make(tmp_path=tmp_path, clock=lambda: now[0] + 601)
    assert later._recent_ids() == []


def test_seed_rerun_inside_the_lag_window_finds_what_it_created(tmp_path):
    fake, b = make(tmp_path=tmp_path)
    fake.lag = True
    ref = b.create_issue("Poll", "Body\n\n<!-- dags-seed: T1 -->", ["swarm:status:ready"])
    assert fake.index.get(ref.key) is None                            # the index has not caught up
    _, other_process = make(fake, tmp_path=tmp_path)                  # a new process, same recent file
    assert set(seed.seeded(other_process)) == {"T1"}


def test_create_issue_uses_configured_types_and_returns_the_key():
    fake, b = make(issue_types={"epic": "Epic", "task": "Story"})
    b.create_issue("x", "body", ["a"], epic=False)
    k = b.create_issue("e", "", [], epic=True)
    assert fake.issues[k.key]["fields"]["issuetype"]["name"] == "Epic"
    assert [c for c in fake.calls if c[0] == "create_issue"]


def test_dependency_direction_round_trips_and_is_not_duplicated():
    fake, b = make()
    a = fake.add("A", "blocker")
    c = fake.add("B", "blocked")
    b.add_dependency(TaskRef(c), TaskRef(a))
    b.add_dependency(TaskRef(c), TaskRef(a))                          # second call: already linked
    assert [x for x in fake.calls if x[0] == "link"] == [("link", "Blocks", c, a)]   # inward = the blocked one
    assert b.dependencies(TaskRef(c)) == [TaskRef(a)]
    assert b.dependencies(TaskRef(a)) == []


def test_set_parent():
    fake, b = make()
    e = fake.add("E", "epic", epic=True)
    t = fake.add("T", "task", labels=["swarm:status:ready"])
    b.set_parent(TaskRef(t), TaskRef(e))
    assert b.get_task(TaskRef(t)).epic == TaskRef(e)


# -- done / workflow ----------------------------------------------------------------------------
def test_done_closes_through_the_one_done_transition():
    fake, b = make()
    k = fake.add("T", "t", labels=["swarm:status:ready"])
    b.set_status(TaskRef(k), "done")
    assert fake.issues[k]["fields"]["status"]["statusCategory"]["key"] == "done"
    assert ("transition", k, "31") in fake.calls and b.get_task(TaskRef(k)).closed


def test_done_is_idempotent_when_a_human_already_closed_it():
    fake, b = make()
    k = fake.add("T", "t", closed=True, labels=["swarm:status:in-progress"])
    b.set_status(TaskRef(k), "done")
    b.set_status(TaskRef(k), "done")
    assert not [c for c in fake.calls if c[0] == "transition"]
    assert "swarm:status:done" in fake.issues[k]["fields"]["labels"]


def test_done_with_several_done_transitions_names_them_and_the_setting():
    fake, b = make()
    k = fake.add("T", "t")
    fake.extra_transitions = [{"id": "41", "name": "Won't do",
                               "to": {"name": "Won't do", "statusCategory": {"key": "done"}}}]
    with pytest.raises(JiraError) as e:
        b.set_status(TaskRef(k), "done")
    assert "Finish" in str(e.value) and "Won't do" in str(e.value) and "jira.transitions.done" in str(e.value)


def test_transitions_done_setting_picks_one():
    fake, b = make(transitions={"done": "Finish"})
    k = fake.add("T", "t")
    fake.extra_transitions = [{"id": "41", "name": "Won't do",
                               "to": {"name": "Won't do", "statusCategory": {"key": "done"}}}]
    b.set_status(TaskRef(k), "done")
    assert ("transition", k, "31") in fake.calls


def test_no_done_transition_is_an_error_not_a_silent_skip():
    fake, b = make()
    k = fake.add("T", "t")
    fake.get_transitions = lambda key: [{"id": "11", "name": "Start",
                                         "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}]
    with pytest.raises(JiraError, match="no transition reaches Done"):
        b.set_status(TaskRef(k), "done")


def test_optional_board_transitions_follow_the_swarm():
    fake, b = make(transitions={"in-progress": "In Progress"})
    k = fake.add("T", "t")
    b.set_status(TaskRef(k), "in-progress")
    assert fake.issues[k]["fields"]["status"]["name"] == "In Progress"
    b.set_status(TaskRef(k), "in-progress")                            # already there: no second transition
    assert len([c for c in fake.calls if c[0] == "transition"]) == 1


def test_a_missing_board_transition_is_reported():
    fake, b = make(transitions={"claimed": "Nowhere"})
    k = fake.add("T", "t")
    with pytest.raises(JiraError, match="Nowhere"):
        b.set_status(TaskRef(k), "claimed")


# -- comments ----------------------------------------------------------------------------------------
def test_comment_round_trip_is_exact_and_timestamps_are_utc():
    fake, b = make()
    k = fake.add("T", "t")
    text = "<!-- dags-plan: abc -->\n## Plan\nline one\nline two"
    url = b.post_comment(TaskRef(k), text)
    got = b.list_comments(TaskRef(k))
    assert got[0].body == text and got[0].author == "bot-account" and got[0].url == url
    assert got[0].created_at == "2026-10-09T09:01:00Z"


def test_comments_by_people_come_from_adf():
    from dags.adf import to_adf
    fake, b = make()
    k = fake.add("T", "t")
    fake.human_comment(k, "acct-1", to_adf("Looks good, but **check** `x`"))
    c = b.list_comments(TaskRef(k))[0]
    assert c.author == "acct-1" and c.body == "Looks good, but **check** `x`"


def test_marker_survives_a_comment_stored_without_the_property():
    import re
    from dags.adf import to_adf
    fake, b = make()
    k = fake.add("T", "t")
    fake.human_comment(k, "bot", to_adf("<!-- dags-block: claim-1 -->\nWhat now?"))
    assert re.search(r"<!-- dags-block: [^>]*-->", b.list_comments(TaskRef(k))[0].body)


def test_long_comments_are_split_with_the_marker_on_the_first():
    fake, b = make()
    k = fake.add("T", "t")
    text = "<!-- dags-plan: sha -->\n" + "\n".join(f"line {i} " + "x" * 90 for i in range(800))
    b.post_comment(TaskRef(k), text)
    got = b.list_comments(TaskRef(k))
    assert len(got) >= 2 and "<!-- dags-plan: sha -->" in got[0].body
    assert all("<!-- dags-plan" not in c.body for c in got[1:])
    assert all(len(c.body.encode()) < 32000 for c in got)
    assert "line 799" in got[-1].body


def test_chunking_cuts_an_overlong_single_line():
    parts = chunk_markdown("é" * 50, limit=40)
    assert len(parts) > 1 and all(len(p.encode()) <= 40 for p in parts) and "".join(parts) == "é" * 50


def test_comments_fall_back_to_adf_when_properties_are_refused():
    fake, b = make()
    fake.properties_ok = False
    k = fake.add("T", "t")
    b.post_comment(TaskRef(k), "plain words")
    assert b.list_comments(TaskRef(k))[0].body == "plain words"


@pytest.mark.parametrize("stamp,out", [("2026-10-09T10:00:00.000+0100", "2026-10-09T09:00:00Z"),
                                       ("2026-10-09T10:00:00.000-0500", "2026-10-09T15:00:00Z"),
                                       ("", ""), ("garbage", "garbage")])
def test_utc_iso(stamp, out):
    assert utc_iso(stamp) == out


# -- labels ---------------------------------------------------------------------------------------------
def test_existing_labels_lists_jira_plus_everything_the_swarm_would_create():
    fake, b = make(code_repos=["org/app"])
    k = fake.add("T", "t", labels=["bug"])
    have = b.existing_labels()
    assert "bug" in have and "swarm:status:ready" in have and "swarm:autonomy:self-approve" in have
    assert "repo:org/app" in have
    b.create_label("anything", "ffffff")                                # no-op, no call
    assert not [c for c in fake.calls if c[0] not in ("search", "get_issue")]


def test_seed_labels_have_no_type_label():
    _, b = make()
    assert b.seed_labels(epic=True, status="ready", autonomy="self-approve", repo="org/app") == \
        ["swarm:status:ready", "swarm:autonomy:self-approve", "repo:org/app"]


def test_label_codec_is_one_place():
    from backends.jira import LabelCodec

    class Safe(LabelCodec):
        SUBSTITUTIONS = (("/", "__"),)
    c = Safe()
    assert c.encode("repo:org/app") == "repo:org__app" and c.decode("repo:org__app") == "repo:org/app"
    fake, b = make()
    b.codec = c
    k = fake.add("T", "t")
    b.set_status(TaskRef(k), "ready")
    b.create_issue("x", "", ["repo:org/app"])
    assert "repo:org__app" in fake.issues["KAN-2"]["fields"]["labels"]
    assert b.get_task(TaskRef("KAN-2")).repo == "org/app"


def test_get_task_fresh_reads_the_issue_directly():
    fake, b = make()
    k = fake.add("T", "t", labels=["swarm:status:ready"])
    b.all_issues()
    fake.issues[k]["fields"]["labels"].append("swarm:status:blocked")
    assert b.get_task_fresh(TaskRef(k)).labels.count("swarm:status:blocked") == 1
    with pytest.raises(KeyError):
        b.get_task_fresh(TaskRef("KAN-999"))


def test_lagging_claim_candidate_is_caught_by_the_fresh_read():
    """The search still lists the task ready; a direct read shows a human blocked it."""
    fake, b = make()
    k = fake.add("T", "t", labels=["swarm:status:ready"])
    fake.lag = True
    fake.issues[k]["fields"]["labels"].append("swarm:status:blocked")   # not in the index
    b.invalidate()
    assert TaskRef(k) in b.ready_tasks()                                # stale list
    assert b.get_task_fresh(TaskRef(k)).blocked                         # what the scheduler re-checks
