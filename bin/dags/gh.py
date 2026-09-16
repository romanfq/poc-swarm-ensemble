"""Thin wrapper over the official gh CLI (whitepaper Ch.2, Ch.9).

Everything that touches GitHub goes through ``gh()`` so tests can swap the
runner, and so a worker-only token (the bot account) can be passed per call
without touching the human's own ``gh auth``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable


class GhError(RuntimeError):
    def __init__(self, args, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"gh {' '.join(args)} failed ({returncode}): {stderr.strip()}")


Runner = Callable[..., subprocess.CompletedProcess]


def _default_runner(args: list[str], *, env: dict, cwd=None, input: str | None = None):
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=env, cwd=cwd, input=input)


_runner: Runner = _default_runner


def set_runner(runner: Runner | None) -> None:
    global _runner
    _runner = runner or _default_runner


def gh(args: list[str], *, token: str | None = None, cwd=None, input: str | None = None,
       check: bool = True) -> str:
    env = dict(os.environ)
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("NO_COLOR", "1")
    if token:
        env["GH_TOKEN"] = token
    result = _runner(list(args), env=env, cwd=cwd, input=input)
    if check and result.returncode != 0:
        raise GhError(args, result.returncode, result.stderr or "")
    return result.stdout


def gh_json(args: list[str], **kw) -> Any:
    out = gh(args, **kw)
    return json.loads(out) if out.strip() else None


def available() -> bool:
    return shutil.which("gh") is not None


def auth_ok() -> bool:
    try:
        gh(["auth", "status"])
        return True
    except (GhError, FileNotFoundError):
        return False


def login(token: str | None = None) -> str | None:
    try:
        return gh(["api", "user", "--jq", ".login"], token=token).strip() or None
    except (GhError, FileNotFoundError):
        return None


def server_date() -> str | None:
    """The Date header from api.github.com, for clock-skew calibration."""
    try:
        out = gh(["api", "-i", "/meta"])
    except (GhError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if line.lower().startswith("date:"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break
    return None


REQUIRED_FEATURES = {
    ("issue", "edit"): ["--add-blocked-by", "--parent"],
    ("issue", "create"): ["--blocked-by", "--parent"],
}


def missing_features() -> list[str]:
    """gh subcommand flags DAGS relies on that this gh install lacks."""
    missing = []
    for cmd, flags in REQUIRED_FEATURES.items():
        try:
            text = gh([*cmd, "--help"], check=False)
        except FileNotFoundError:
            return ["gh not installed"]
        for flag in flags:
            if flag not in text:
                missing.append(f"gh {' '.join(cmd)} {flag}")
    return missing


def version() -> str | None:
    try:
        m = re.search(r"(\d+\.\d+\.\d+)", gh(["--version"]))
    except (GhError, FileNotFoundError):
        return None
    return m.group(1) if m else None


# --- pull requests ------------------------------------------------------------

PR_FIELDS = "number,url,state,isDraft,reviewDecision,mergedAt,closedAt,headRefName,statusCheckRollup,reviews,comments,title"


def pr_view(repo: str, pr: str | int, fields: str = PR_FIELDS) -> dict:
    return gh_json(["pr", "view", str(pr), "--repo", repo, "--json", fields]) or {}


def pr_for_branch(repo: str, branch: str) -> dict | None:
    rows = gh_json(["pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                    "--json", "number,url,state"]) or []
    open_rows = [r for r in rows if r.get("state") == "OPEN"]
    return (open_rows or rows or [None])[0]


def pr_files(repo: str, pr: str | int) -> list[str]:
    out = gh(["pr", "diff", str(pr), "--repo", repo, "--name-only"], check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def checks_summary(pr: dict) -> str:
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "no checks"
    states = []
    for c in rollup:
        states.append((c.get("conclusion") or c.get("state") or c.get("status") or "").upper())
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in states):
        return "failing"
    if all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
        return "passing"
    return "pending"


def repo_from_pr_url(url: str) -> tuple[str, str] | None:
    m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)", url or "")
    return (m.group(1), m.group(2)) if m else None


def approve_and_merge(repo: str, pr: str | int, body: str = "Approved via Swarm Board") -> None:
    """The human merge gate (Ch.9.1). Runs under the human's own gh auth."""
    gh(["pr", "review", str(pr), "--repo", repo, "--approve", "--body", body])
    gh(["pr", "merge", str(pr), "--repo", repo, "--squash"])


REVIEW_TAGS = ("fix", "explain", "reject-approach")


def parse_feedback(pr: dict) -> list[dict]:
    """Tagged human feedback (Ch.9.3): lines starting with fix:/explain:/reject-approach:."""
    items = []
    sources = []
    for r in pr.get("reviews") or []:
        sources.append((r.get("author", {}).get("login"), r.get("body") or "", r.get("submittedAt"), r.get("state")))
    for c in pr.get("comments") or []:
        sources.append((c.get("author", {}).get("login"), c.get("body") or "", c.get("createdAt"), "COMMENT"))
    for author, body, when, state in sources:
        for line in body.splitlines():
            m = re.match(r"\s*(fix|explain|reject-approach)\s*:\s*(.+)", line, re.I)
            if m:
                items.append({"tag": m.group(1).lower(), "text": m.group(2).strip(),
                              "author": author, "at": when, "state": state})
    return items
