import json

import pytest

from dags import gh


def test_token_is_passed_per_call_only(fake_gh, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    gh.gh(["pr", "list"], token="bot-token")
    gh.gh(["pr", "list"])
    assert [c["token"] for c in fake_gh.calls] == ["bot-token", None]


def test_errors_raise_with_stderr(fake_gh):
    fake_gh.handler = lambda a, e, i: ("", 1)
    with pytest.raises(gh.GhError) as exc:
        gh.gh(["issue", "view", "7"])
    assert "boom" in str(exc.value)
    assert gh.gh(["x"], check=False) == ""


def test_json_and_login(fake_gh):
    fake_gh.handler = lambda a, e, i: json.dumps({"a": 1}) if a[0] == "pr" else "romanfq\n"
    assert gh.gh_json(["pr", "view"]) == {"a": 1}
    assert gh.login() == "romanfq"


def test_server_date_from_headers(fake_gh):
    fake_gh.handler = lambda a, e, i: ("HTTP/2.0 200 OK\nContent-Type: application/json\n"
                                       "Date: Tue, 15 Sep 2026 22:00:00 GMT\n\n{}")
    assert gh.server_date() == "Tue, 15 Sep 2026 22:00:00 GMT"


def test_missing_features(fake_gh):
    fake_gh.handler = lambda a, e, i: "--parent --blocked-by" if a[1] == "create" else "--parent"
    assert gh.missing_features() == ["gh issue edit --add-blocked-by"]


def test_checks_summary():
    assert gh.checks_summary({}) == "no checks"
    assert gh.checks_summary({"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "SKIPPED"}]}) == "passing"
    assert gh.checks_summary({"statusCheckRollup": [{"conclusion": "FAILURE"}, {"state": "PENDING"}]}) == "failing"
    assert gh.checks_summary({"statusCheckRollup": [{"status": "IN_PROGRESS"}]}) == "pending"


def test_repo_from_pr_url():
    assert gh.repo_from_pr_url("https://github.com/org/matchwire-backend/pull/42") == ("org/matchwire-backend", "42")
    assert gh.repo_from_pr_url("nope") is None


def test_parse_feedback_tags():
    pr = {"reviews": [{"author": {"login": "jane"}, "state": "CHANGES_REQUESTED", "submittedAt": "t1",
                       "body": "Looks close.\nfix: handle null scores\nexplain: why a new cache?"}],
          "comments": [{"author": {"login": "roman"}, "createdAt": "t2",
                        "body": "Reject-Approach: polling is wrong, use the push feed"}]}
    items = gh.parse_feedback(pr)
    assert [(i["tag"], i["text"]) for i in items] == [
        ("fix", "handle null scores"), ("explain", "why a new cache?"),
        ("reject-approach", "polling is wrong, use the push feed")]
    assert items[0]["author"] == "jane" and items[2]["state"] == "COMMENT"


def test_approve_and_merge_runs_both_commands_in_order(fake_gh, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    gh.approve_and_merge("org/app", 42)
    assert [c["args"][:2] for c in fake_gh.calls] == [["pr", "review"], ["pr", "merge"]]
    assert "--approve" in fake_gh.calls[0]["args"] and "--squash" in fake_gh.calls[1]["args"]
    assert all(c["token"] is None for c in fake_gh.calls), "merge runs under the human's own auth"


def test_pr_for_branch_prefers_open(fake_gh):
    fake_gh.handler = lambda a, e, i: json.dumps([
        {"number": 1, "url": "u1", "state": "CLOSED"}, {"number": 2, "url": "u2", "state": "OPEN"}])
    assert gh.pr_for_branch("o/r", "swarm/x")["number"] == 2
    fake_gh.handler = lambda a, e, i: "[]"
    assert gh.pr_for_branch("o/r", "swarm/x") is None
