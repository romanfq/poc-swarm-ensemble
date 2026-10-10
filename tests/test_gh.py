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


class _Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


def _waiter(monkeypatch, views, **kw):
    clock = _Clock()
    seq = iter(views)
    monkeypatch.setattr(gh, "pr_view", lambda repo, pr, fields=None, token=None: next(seq))
    return gh.wait_for_checks("o/r", 1, 60, 10, sleep=clock.sleep, clock=clock.now, **kw), clock


def test_wait_for_checks_settles_green_and_red(monkeypatch):
    run = {"status": "IN_PROGRESS"}
    (summary, _), clock = _waiter(monkeypatch, [{"statusCheckRollup": [run]},
                                                 {"statusCheckRollup": [{"conclusion": "SUCCESS"}]}])
    assert summary == "passing" and clock.t == 10
    red = {"name": "tests", "conclusion": "FAILURE"}
    (summary, view), _ = _waiter(monkeypatch, [{"statusCheckRollup": [red]}])
    assert summary == "failing" and gh.failing_checks(view) == ["tests"]


def test_wait_for_checks_times_out_pending(monkeypatch):
    (summary, _), clock = _waiter(monkeypatch, [{"statusCheckRollup": [{"status": "QUEUED"}]}] * 10)
    assert summary == "pending" and clock.t == 60


def test_wait_for_checks_empty_rollup_gets_a_grace_period(monkeypatch):
    views = [{"statusCheckRollup": []}, {"statusCheckRollup": []},
             {"statusCheckRollup": [{"conclusion": "SUCCESS"}]}]
    (summary, _), _ = _waiter(monkeypatch, views)
    assert summary == "passing"
    (summary, _), clock = _waiter(monkeypatch, [{"statusCheckRollup": []}] * 10)
    assert summary == "no checks" and clock.t == 30


def test_default_runner_timeout_names_the_call(monkeypatch):
    import subprocess

    def slow(cmd, **kw):
        assert kw["timeout"] == 3.0
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setenv("DAGS_GH_TIMEOUT", "3")
    monkeypatch.setattr(subprocess, "run", slow)
    gh.set_runner(None)
    with pytest.raises(gh.GhError) as exc:
        gh.gh(["pr", "view", "55"])
    assert "gh pr view 55" in str(exc.value) and "timed out after 3s" in str(exc.value)
    assert exc.value.returncode == 124


def test_wait_for_checks_reports_each_state_change(monkeypatch):
    q = {"name": "tests", "status": "QUEUED"}
    run = {"name": "tests", "status": "IN_PROGRESS"}
    ok = {"name": "tests", "conclusion": "SUCCESS"}
    seen = []
    (summary, _), _ = _waiter(monkeypatch, [{"statusCheckRollup": v} for v in ([q], [q], [run], [ok])],
                              on_change=lambda *a: seen.append(a))
    assert summary == "passing"
    assert seen == [("tests", None, "queued", 0), ("tests", "queued", "in progress", 20),
                    ("tests", "in progress", "passed", 30)]


def test_tail_failed_steps_keeps_the_end_and_strips_control_text():
    body = "\n".join(f"tests\tRun pytest\t2026-01-01T00:00:0{i % 10}.0Z line {i}" for i in range(200))
    evil = "tests\tRun pytest\t\x1b[31mE   [red]boom[/red]\x07\x00 ok\x1b[0m"
    out = gh.tail_failed_steps(body + "\n" + evil, lines=5)
    assert out.startswith("== tests / Run pytest ==")
    assert "line 195" not in out and "line 199" in out
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert "E   [red]boom[/red] ok" in out          # markup-like text is kept as plain text
    assert len(gh.tail_failed_steps("a\tb\t" + "x" * 50000)) <= 6010
