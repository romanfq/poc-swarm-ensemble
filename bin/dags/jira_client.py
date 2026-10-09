"""The wire to Jira Cloud (GH-123): a narrow ``JiraClient`` interface and its one stdlib implementation.

The adapter (``backends/jira.py``) owns what the swarm means by an issue; this module owns the wire:
URL, authentication, timeouts, retries and rate limits. Every method takes and returns plain dicts,
lists and strings in Jira's documented JSON shapes. It is an internal seam, not a plug-in point:
nothing in any config file selects a different client.

Rules this module keeps (see the GH-123 spec, section 8):
* the credential only ever goes to Atlassian (``https``, ``*.atlassian.net`` or ``api.atlassian.com``,
  plus hosts the operator names in ``.swarm/local.yaml``); checked when the client is built, and
  redirects to another host are refused;
* every call has a per-attempt timeout (``DAGS_JIRA_TIMEOUT``, 20s) and a total deadline
  (``DAGS_JIRA_DEADLINE``, 60s);
* only safe-to-repeat requests are retried (429, or 503 with Retry-After); creating an issue, a link or
  a comment is never repeated;
* the token is read per call and appears in no error text, log line or file.
"""
from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol, runtime_checkable

from backends.base import BackendError

DEFAULT_TIMEOUT = 20.0
DEFAULT_DEADLINE = 60.0
MAX_ATTEMPTS = 4
KEYCHAIN_SERVICE = "dags-jira-token"
TRUNCATED_TOKEN_LEN = 128          # what `security add-generic-password ... -w` (prompt form) cuts a token to


class JiraError(BackendError):
    """The only exception that crosses the client seam."""

    def __init__(self, status: int, messages: list[str] | str, retry_after: float | None = None,
                 reason: str | None = None, path: str = ""):
        self.status = status
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        self.retry_after = retry_after
        self.reason = reason
        self.path = path
        text = "; ".join(m for m in self.messages if m) or "no details"
        where = f" {path}" if path else ""
        super().__init__(f"Jira{where} failed ({status}): {text}")


@runtime_checkable
class JiraClient(Protocol):
    """One method per REST operation the adapter needs. Errors are ``JiraError``."""

    def myself(self) -> dict: ...                                              # GET /myself
    def search(self, jql: str, fields: list[str], *, next_page_token: str | None = None,
               max_results: int = 100, reconcile_issues: list[int] | None = None) -> dict: ...
    def get_issue(self, key: str, fields: list[str] | None = None) -> dict: ...
    def create_issue(self, fields: dict) -> dict: ...                          # {id, key}
    def update_issue(self, key: str, body: dict) -> None: ...
    def get_transitions(self, key: str) -> list[dict]: ...
    def transition(self, key: str, transition_id: str) -> None: ...
    def create_issue_link(self, type_name: str, inward_key: str, outward_key: str) -> None: ...
    def add_comment(self, key: str, adf_body: dict, properties: list[dict] | None = None) -> dict: ...
    def list_comments(self, key: str) -> list[dict]: ...                       # oldest first
    def list_labels(self) -> list[str]: ...
    def list_issue_types(self, project_key: str) -> list[dict]: ...


# -- configuration ---------------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def timeout() -> float:
    """Seconds one attempt may take (``DAGS_JIRA_TIMEOUT``)."""
    return _env_float("DAGS_JIRA_TIMEOUT", DEFAULT_TIMEOUT)


def deadline() -> float:
    """Seconds one call may take in all, attempts and sleeps (``DAGS_JIRA_DEADLINE``)."""
    return _env_float("DAGS_JIRA_DEADLINE", DEFAULT_DEADLINE)


def site_origin(base_url: str) -> str:
    """Scheme and host of a pasted site or board URL; a trailing slash or a path is dropped."""
    parts = urllib.parse.urlsplit(str(base_url).strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"jira.base_url must be an https URL such as https://yourteam.atlassian.net (got {base_url!r})")
    return f"{parts.scheme}://{parts.netloc}"


def check_host(origin: str, allowed_hosts: list[str] | None = None) -> None:
    """Refuse any host the credential must not go to. ``base_url`` comes from a committed file that anyone
    with push access can edit, so this runs when the client is built."""
    parts = urllib.parse.urlsplit(origin)
    host = (parts.hostname or "").lower()
    extra = {str(h).lower() for h in allowed_hosts or []}
    if parts.scheme != "https":
        raise ValueError(f"jira.base_url must use https (got {origin!r})")
    if host.endswith(".atlassian.net") or host == "api.atlassian.com" or host in extra:
        return
    raise ValueError(f"jira.base_url host {host!r} is not an Atlassian host; the Jira credential is only sent "
                     f"to *.atlassian.net or api.atlassian.com. To use another host, list it under "
                     f"jira.allowed_hosts in .swarm/local.yaml (never in a committed file).")


class Credentials:
    """Email and token, resolved per call so a rotated Keychain item is picked up without a restart."""

    def __init__(self, local: dict | None = None, env: dict | None = None,
                 keychain: Callable[[str], str | None] | None = None):
        self.local = local or {}
        self.env = os.environ if env is None else env
        self.keychain = keychain or _keychain_token

    @property
    def email(self) -> str:
        email = self.env.get("JIRA_EMAIL") or str(self.local.get("email") or "")
        if not email:
            raise JiraError(401, "no Jira email: set JIRA_EMAIL or jira.email in .swarm/local.yaml")
        return email

    @property
    def token(self) -> str:
        tok = self.env.get("JIRA_API_TOKEN")
        if not tok:
            service = str((self.local.get("token") or {}).get("keychain_service") or KEYCHAIN_SERVICE)
            tok = self.keychain(service)
        if not tok:
            raise JiraError(401, f"no Jira API token: set JIRA_API_TOKEN or store it in the macOS Keychain "
                                 f"(service {KEYCHAIN_SERVICE}); see the Jira section of the getting-started guide")
        return tok.strip()

    def header(self) -> tuple[str, str]:
        """(Authorization header value, token): the token is returned only so callers can redact it."""
        tok = self.token
        raw = base64.b64encode(f"{self.email}:{tok}".encode()).decode()
        return f"Basic {raw}", tok


def _keychain_token(service: str) -> str | None:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


# -- transport ---------------------------------------------------------------------------------------
Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict, bytes]"]
_transport: Transport | None = None


def set_transport(fn: Transport | None) -> None:
    """Test seam, like ``gh.set_runner``: ``fn(method, url, headers, body, timeout) -> (status, headers, body)``.
    Header names are lower-cased. Raise ``OSError`` for a network failure."""
    global _transport
    _transport = fn


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """urllib may forward ``Authorization`` across a redirect: follow only redirects that stay on the host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc.lower() != urllib.parse.urlsplit(req.full_url).netloc.lower():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout_s: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(_SameHostRedirect)
    try:
        with opener.open(req, timeout=timeout_s) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


# -- the client --------------------------------------------------------------------------------------
class UrllibJiraClient:
    def __init__(self, base_url: str, *, local: dict | None = None, cloud_id: str = "",
                 credentials: Credentials | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[], float] = random.random):
        local = local or {}
        self.origin = site_origin(base_url)
        check_host(self.origin, local.get("allowed_hosts") or [])
        cloud_id = cloud_id or str(local.get("cloud_id") or "")
        self.api = f"https://api.atlassian.com/ex/jira/{cloud_id}" if cloud_id else self.origin
        self.credentials = credentials or Credentials(local)
        self._sleep, self._clock, self._rand = sleep, clock, rand
        self._verified = False
        self._blocked_until: dict[str, float] = {}      # "*" = whole quota; else endpoint or issue
        self._secrets: list[str] = []

    # -- plumbing
    def _redact(self, text: str) -> str:
        for s in self._secrets:
            if s:
                text = text.replace(s, "[redacted]")
        return text

    def _bucket(self, method: str, path: str) -> str:
        parts = path.split("?", 1)[0].split("/")
        if method != "GET" and len(parts) > 5 and parts[4] == "issue":
            return f"issue:{parts[4]}"
        return path.split("?", 1)[0]

    def _request(self, method: str, path: str, body: Any = None, *, safe: bool = False) -> Any:
        """One logical call: retries a 429 (or a 503 carrying Retry-After) only when ``safe``."""
        started = self._clock()
        limit = deadline()
        data = None if body is None else json.dumps(body).encode()
        bucket = self._bucket(method, path)
        attempt = 0
        delay = 2.0
        while True:
            attempt += 1
            left = limit - (self._clock() - started)
            self._check_breaker(bucket, path)
            if left <= 0:
                raise JiraError(0, f"gave up after {limit:g}s", path=path)
            auth, tok = self.credentials.header()
            self._secrets = [tok, auth.split(" ", 1)[1]]
            headers = {"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"}
            try:
                status, hdrs, raw = (_transport or _urllib_transport)(method, self.api + path, headers, data,
                                                                      min(timeout(), left))
            except OSError as e:
                raise JiraError(0, self._redact(f"cannot reach Jira: {e}"), path=path) from None
            if status < 400:
                return json.loads(raw.decode() or "null") if raw else None
            err = self._error(status, hdrs, raw, path)
            self._note_limit(err, bucket)
            retryable = status == 429 or (status == 503 and err.retry_after is not None)
            if not (safe and retryable) or attempt >= MAX_ATTEMPTS:
                raise err
            wait = max(err.retry_after or 0.0, delay * (0.7 + 0.6 * self._rand()))
            if wait > limit - (self._clock() - started):
                raise err                                # never sleep past the deadline; say when to retry
            self._sleep(wait)
            delay = min(delay * 2, 30.0)

    def _check_breaker(self, bucket: str, path: str) -> None:
        now = self._clock()
        for key in ("*", bucket):
            until = self._blocked_until.get(key)
            if until and until > now:
                raise JiraError(429, "Jira's rate limit is in force; not calling until it resets",
                                retry_after=until - now, reason="circuit-open", path=path)

    def _note_limit(self, err: JiraError, bucket: str) -> None:
        if err.status != 429 and err.retry_after is None:
            return
        wait = err.retry_after
        reason = err.reason or ""
        if reason.startswith("jira-quota-"):
            self._blocked_until["*"] = self._clock() + (wait or 60.0)
        elif reason.startswith("jira-burst-") or reason.startswith("jira-per-issue-"):
            self._blocked_until[bucket] = self._clock() + (wait or 5.0)

    def _error(self, status: int, hdrs: dict, raw: bytes, path: str) -> JiraError:
        msgs: list[str] = []
        try:
            j = json.loads(raw.decode() or "{}")
            msgs += [str(m) for m in j.get("errorMessages") or []]
            msgs += [f"{k}: {v}" for k, v in (j.get("errors") or {}).items()]
        except (ValueError, AttributeError):
            msgs.append(raw.decode(errors="replace")[:200])
        retry_after = None
        try:
            retry_after = float(hdrs["retry-after"]) if "retry-after" in hdrs else None
        except ValueError:
            pass
        login = hdrs.get("x-seraph-loginreason")
        reason = hdrs.get("ratelimit-reason") or login
        text = " ".join(msgs)
        if status == 401 or (status == 403 and login == "AUTHENTICATED_FAILED"):
            hint = "the Jira token was rejected or has expired; create a new one and update the Keychain item"
            if len(self._secrets[0] if self._secrets else "") == TRUNCATED_TOKEN_LEN:
                hint += (f" (the stored token is exactly {TRUNCATED_TOKEN_LEN} characters, which suggests it "
                         f"was truncated when it was stored)")
            msgs = [hint]
        elif status == 403:
            msgs = [*msgs, "authenticated, but this Jira account is not allowed to do that"]
        elif status == 404 and "No project could be found" in text:
            msgs = [*msgs, "check the key, and that the Jira account is a member of the project"]
        return JiraError(status, [self._redact(m) for m in msgs], retry_after, reason, path)

    def _verify(self) -> None:
        """First thing on startup: 'not authenticated' must never be mistaken for 'no such project'."""
        if not self._verified:
            self._request("GET", "/rest/api/3/myself", safe=True)
            self._verified = True

    def _call(self, method: str, path: str, body: Any = None, *, safe: bool = False) -> Any:
        self._verify()
        return self._request(method, path, body, safe=safe)

    # -- the interface
    def myself(self) -> dict:
        out = self._request("GET", "/rest/api/3/myself", safe=True)
        self._verified = True
        return out

    def search(self, jql, fields, *, next_page_token=None, max_results=100, reconcile_issues=None) -> dict:
        body: dict = {"jql": jql, "fields": list(fields), "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        if reconcile_issues:
            body["reconcileIssues"] = [int(i) for i in reconcile_issues][:50]
        return self._call("POST", "/rest/api/3/search/jql", body, safe=True)

    def get_issue(self, key, fields=None) -> dict:
        q = "?" + urllib.parse.urlencode({"fields": ",".join(fields)}) if fields else ""
        return self._call("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}{q}", safe=True)

    def create_issue(self, fields) -> dict:
        return self._call("POST", "/rest/api/3/issue", {"fields": fields})            # never retried

    def update_issue(self, key, body) -> None:
        # Label add/remove and parent are idempotent, so a 429 may be retried.
        self._call("PUT", f"/rest/api/3/issue/{urllib.parse.quote(key)}", body, safe=True)

    def get_transitions(self, key) -> list[dict]:
        out = self._call("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions", safe=True)
        return (out or {}).get("transitions") or []

    def transition(self, key, transition_id) -> None:
        self._call("POST", f"/rest/api/3/issue/{urllib.parse.quote(key)}/transitions",
                   {"transition": {"id": str(transition_id)}})

    def create_issue_link(self, type_name, inward_key, outward_key) -> None:
        # For `Blocks`, inwardIssue is the issue that IS BLOCKED and outwardIssue is the blocker.
        self._call("POST", "/rest/api/3/issueLink",
                   {"type": {"name": type_name}, "inwardIssue": {"key": inward_key},
                    "outwardIssue": {"key": outward_key}})

    def add_comment(self, key, adf_body, properties=None) -> dict:
        body: dict = {"body": adf_body}
        if properties:
            body["properties"] = properties
        return self._call("POST", f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment", body)

    def list_comments(self, key) -> list[dict]:
        out, start = [], 0
        while True:
            q = urllib.parse.urlencode({"startAt": start, "maxResults": 100, "orderBy": "created",
                                        "expand": "properties"})
            page = self._call("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment?{q}", safe=True) or {}
            got = page.get("comments") or []
            out += got
            start += len(got)
            if not got or start >= int(page.get("total", start)):
                return out

    def list_labels(self) -> list[str]:
        out, start = [], 0
        while True:
            q = urllib.parse.urlencode({"startAt": start, "maxResults": 1000})
            page = self._call("GET", f"/rest/api/3/label?{q}", safe=True) or {}
            vals = page.get("values") or []
            out += [str(v) for v in vals]
            start += len(vals)
            if page.get("isLast", True) or not vals:
                return out

    def list_issue_types(self, project_key) -> list[dict]:
        proj = self._call("GET", f"/rest/api/3/project/{urllib.parse.quote(project_key)}", safe=True) or {}
        q = urllib.parse.urlencode({"projectId": proj.get("id", "")})
        return self._call("GET", f"/rest/api/3/issuetype/project?{q}", safe=True) or []


def build_client(cfg: dict, local: dict | None = None) -> JiraClient:
    """The single factory the adapter uses. ``cfg`` is ``backend.yaml``'s ``jira:`` map, ``local`` is
    ``.swarm/local.yaml``'s; per-machine settings (credentials, cloud_id, allowed_hosts) come from ``local`` only."""
    base = str(cfg.get("base_url") or "")
    if not base or "example" in base:
        raise ValueError("backend.yaml jira.base_url must be your site, e.g. https://yourteam.atlassian.net")
    return UrllibJiraClient(base, local=(local or {}).get("jira") or {})
