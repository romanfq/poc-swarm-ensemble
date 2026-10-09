"""UrllibJiraClient against a fake HTTP layer (GH-123). Nothing here touches the network."""
import json

import pytest

from backends.base import BackendError
from dags import jira_client as jc

TOKEN = "tok-" + "x" * 40


class Wire:
    """A scripted transport: each call pops the next (status, headers, body) for the matching route."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        status, hdrs, payload = self.responses.pop(0)
        return status, hdrs, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(wire, clock=None, **kw):
    clock = clock or Clock()
    jc.set_transport(wire)
    creds = jc.Credentials({"email": "bot@example.com"}, env={"JIRA_API_TOKEN": kw.pop("token", TOKEN)})
    c = jc.UrllibJiraClient("https://acme.atlassian.net/", credentials=creds, sleep=clock.sleep, clock=clock,
                            rand=lambda: 0.5, **kw)
    return c, clock


@pytest.fixture(autouse=True)
def _reset():
    yield
    jc.set_transport(None)


ME = (200, {}, {"accountId": "abc"})


def test_myself_runs_first_then_the_call(monkeypatch):
    w = Wire(ME, (200, {}, {"id": "1", "key": "KAN-1"}))
    c, _ = make(w)
    assert c.create_issue({"summary": "x"})["key"] == "KAN-1"
    assert [x[1].rsplit("/", 1)[-1] for x in w.calls] == ["myself", "issue"]
    assert w.calls[0][2]["Authorization"].startswith("Basic ")


def test_errors_are_jira_errors_with_both_message_kinds():
    w = Wire(ME, (400, {}, {"errorMessages": ["bad"], "errors": {"summary": "required"}}))
    c, _ = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.create_issue({})
    assert e.value.status == 400 and "bad" in str(e.value) and "summary: required" in str(e.value)
    assert isinstance(e.value, BackendError)


def test_network_failure_is_a_jira_error():
    def down(*a):
        raise OSError("connection refused")
    jc.set_transport(down)
    c, _ = make(down)
    with pytest.raises(jc.JiraError):
        c.myself()


def test_429_on_a_safe_request_is_retried_honouring_retry_after():
    w = Wire(ME, (429, {"retry-after": "7"}, {}), (200, {}, {"issues": [], "isLast": True}))
    c, clock = make(w)
    assert c.search("project = KAN", ["summary"])["isLast"]
    assert clock.slept == [7.0]


def test_429_is_retried_at_most_four_attempts():
    w = Wire(ME, *[(429, {}, {})] * 4)
    c, clock = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.get_issue("KAN-1")
    assert e.value.status == 429 and len(w.calls) == 5 and len(clock.slept) == 3


def test_a_duplicate_risk_write_is_never_retried():
    for call in (lambda c: c.create_issue({"summary": "x"}), lambda c: c.add_comment("KAN-1", {}),
                 lambda c: c.create_issue_link("Blocks", "KAN-2", "KAN-1"), lambda c: c.transition("KAN-1", "31")):
        w = Wire(ME, (429, {"retry-after": "1"}, {}))
        c, clock = make(w)
        with pytest.raises(jc.JiraError):
            call(c)
        assert len(w.calls) == 2 and clock.slept == []


def test_retry_after_longer_than_the_deadline_raises_instead_of_sleeping(monkeypatch):
    monkeypatch.setenv("DAGS_JIRA_DEADLINE", "10")
    w = Wire(ME, (429, {"retry-after": "300"}, {}))
    c, clock = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.get_issue("KAN-1")
    assert e.value.retry_after == 300 and clock.slept == []


def test_quota_reason_opens_the_circuit():
    w = Wire(ME, (429, {"retry-after": "120", "ratelimit-reason": "jira-quota-global-based"}, {}))
    c, clock = make(w)
    with pytest.raises(jc.JiraError):
        c.create_issue({})
    n = len(w.calls)
    with pytest.raises(jc.JiraError) as e:
        c.get_issue("KAN-2")
    assert len(w.calls) == n and e.value.reason == "circuit-open"
    clock.t += 121
    w.responses.append((200, {}, {"key": "KAN-2"}))
    assert c.get_issue("KAN-2")["key"] == "KAN-2"


def test_deadline_bounds_the_attempt_timeout(monkeypatch):
    monkeypatch.setenv("DAGS_JIRA_TIMEOUT", "20")
    monkeypatch.setenv("DAGS_JIRA_DEADLINE", "5")
    w = Wire(ME, (200, {}, {}))
    c, _ = make(w)
    c.get_issue("KAN-1")
    assert all(call[4] <= 5 for call in w.calls)


@pytest.mark.parametrize("url", ["http://acme.atlassian.net", "https://evil.example.com",
                                 "https://acme.atlassian.net.evil.com", "https://atlassian.net.evil.io"])
def test_hosts_outside_atlassian_are_refused(url):
    with pytest.raises(ValueError):
        jc.UrllibJiraClient(url, credentials=jc.Credentials({}, env={}))


def test_allowed_hosts_from_local_yaml_and_pasted_board_urls():
    c = jc.UrllibJiraClient("https://proxy.corp.example/jira/boards/1",
                            local={"allowed_hosts": ["proxy.corp.example"]},
                            credentials=jc.Credentials({}, env={}))
    assert c.origin == "https://proxy.corp.example"
    c = jc.UrllibJiraClient("https://acme.atlassian.net/jira/software/projects/KAN/boards/1",
                            credentials=jc.Credentials({}, env={}))
    assert c.origin == "https://acme.atlassian.net"


def test_scoped_tokens_use_the_api_gateway():
    w = Wire(ME)
    jc.set_transport(w)
    c = jc.UrllibJiraClient("https://acme.atlassian.net", local={"cloud_id": "cid-1"},
                            credentials=jc.Credentials({"email": "b@x"}, env={"JIRA_API_TOKEN": "t"}))
    c.myself()
    assert w.calls[0][1] == "https://api.atlassian.com/ex/jira/cid-1/rest/api/3/myself"


def test_cross_host_redirect_is_refused():
    h = jc._SameHostRedirect()
    req = jc.urllib.request.Request("https://acme.atlassian.net/rest/api/3/myself", headers={"Authorization": "x"})
    assert h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/steal") is None
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://acme.atlassian.net/other")
    assert ok is not None


def test_401_message_is_plain_and_has_no_credential():
    w = Wire((401, {"x-seraph-loginreason": "AUTHENTICATED_FAILED"}, {"errorMessages": [f"bad {TOKEN}"]}))
    c, _ = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.myself()
    msg = str(e.value)
    assert "rejected or has expired" in msg and TOKEN not in msg and e.value.reason == "AUTHENTICATED_FAILED"
    assert "truncated" not in msg


def test_a_128_character_token_gets_the_truncation_hint():
    w = Wire((401, {"x-seraph-loginreason": "AUTHENTICATED_FAILED"}, {}))
    c, _ = make(w, token="t" * 128)
    with pytest.raises(jc.JiraError) as e:
        c.myself()
    assert "exactly 128 characters" in str(e.value)


def test_403_is_told_apart_from_a_rejected_credential():
    w = Wire(ME, (403, {}, {"errorMessages": ["no"]}))
    c, _ = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.update_issue("KAN-1", {})
    assert "not allowed" in str(e.value) and "expired" not in str(e.value)


def test_missing_project_message_names_both_causes():
    w = Wire(ME, (404, {}, {"errorMessages": ["No project could be found with key 'ZZZ'."]}))
    c, _ = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.list_issue_types("ZZZ")
    assert "member of the project" in str(e.value)


def test_search_sends_token_and_at_most_50_reconcile_ids():
    w = Wire(ME, (200, {}, {"issues": [], "isLast": True}))
    c, _ = make(w)
    c.search("project = \"KAN\"", ["summary"], next_page_token="tk", reconcile_issues=list(range(80)))
    body = json.loads(w.calls[1][3])
    assert body["nextPageToken"] == "tk" and len(body["reconcileIssues"]) == 50


def test_issue_link_direction_is_blocked_inward_blocker_outward():
    w = Wire(ME, (201, {}, {}))
    c, _ = make(w)
    c.create_issue_link("Blocks", "KAN-2", "KAN-1")      # KAN-2 is blocked by KAN-1
    body = json.loads(w.calls[1][3])
    assert body["inwardIssue"]["key"] == "KAN-2" and body["outwardIssue"]["key"] == "KAN-1"


def test_comments_and_labels_paginate():
    w = Wire(ME,
             (200, {}, {"comments": [{"id": "1"}, {"id": "2"}], "total": 3}),
             (200, {}, {"comments": [{"id": "3"}], "total": 3}),
             (200, {}, {"values": ["a", "b"], "isLast": False}),
             (200, {}, {"values": ["c"], "isLast": True}))
    c, _ = make(w)
    assert [x["id"] for x in c.list_comments("KAN-1")] == ["1", "2", "3"]
    assert c.list_labels() == ["a", "b", "c"]


def test_credentials_resolve_per_call_and_prefer_env(monkeypatch):
    seen = []
    cr = jc.Credentials({"email": "me@x", "token": {"keychain_service": "svc"}}, env={},
                        keychain=lambda s: seen.append(s) or "from-keychain")
    assert cr.token == "from-keychain" and seen == ["svc"]
    cr.env = {"JIRA_API_TOKEN": "env-tok", "JIRA_EMAIL": "env@x"}
    assert cr.token == "env-tok" and cr.email == "env@x"
    with pytest.raises(jc.JiraError):
        _ = jc.Credentials({}, env={}, keychain=lambda s: None).token


def test_no_secret_in_error_text_for_any_failure():
    w = Wire(ME, (500, {}, {"errorMessages": [f"boom {TOKEN}"]}))
    c, _ = make(w)
    with pytest.raises(jc.JiraError) as e:
        c.create_issue({})
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value.messages)
