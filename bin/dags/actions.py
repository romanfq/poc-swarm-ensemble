"""Human levers (whitepaper Ch.6.6, Ch.9, Ch.10.2), shared by the CLI and the
Swarm Board. Everything except Approve & merge is an append-only ledger
record carried by the normal sync; merge runs the real gh commands."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import resolve
from backends.base import TaskRef
from dags import gh
from dags import ledger as L
from dags import timeutil
from dags.config import ConfigError

log = logging.getLogger("dags.actions")


class ActionError(RuntimeError):
    pass


# -- machine (Ch.10.2) ---------------------------------------------------------------

def pause(ctx, machine: str | None = None) -> None:
    L.control(ctx, "pause", machine)


def resume(ctx, machine: str | None = None) -> None:
    L.control(ctx, "resume", machine)


def throttle(ctx, share: int, machine: str | None = None) -> None:
    if share < 0:
        raise ActionError("quota share must be >= 0")
    L.control(ctx, "throttle", machine, quota_share=int(share))


def set_quota(ctx, n: int, reason: str = "") -> None:
    if n < 0:
        raise ActionError("global quota must be >= 0")
    L.set_quota(ctx, n, reason)


# -- tasks (Ch.6.6) -----------------------------------------------------------------------

def freeze(ctx, task_dir: Path, reason: str) -> None:
    L.arbitrate(ctx, task_dir, None, reason)


def unfreeze(ctx, task_dir: Path, reason: str = "unfrozen") -> None:
    if resolve.active_arbitration(task_dir, ctx.human_names) is None:
        raise ActionError(f"{resolve.label(task_dir)} has no arbitration to lift")
    L.arbitrate(ctx, task_dir, "none", reason, action="withdraw")


def reassign(ctx, task_dir: Path, winner: str, reason: str) -> None:
    claims = {c.id: c for c in resolve.read_claims(task_dir)}
    if winner not in claims:
        # allow picking by machine name: the machine's latest claim
        by_machine = [c for c in claims.values() if c.machine == winner]
        if not by_machine:
            raise ActionError(f"{winner} is not a claimant of {resolve.label(task_dir)} "
                              f"(claimants: {', '.join(claims) or 'none'})")
        winner = max(by_machine, key=lambda c: c.sort_key).id
    L.arbitrate(ctx, task_dir, winner, reason)


def takeover(ctx, epic: str) -> None:
    L.priority(ctx, epic, "takeover")


def release_epic(ctx, epic: str) -> None:
    L.priority(ctx, epic, "release")


# -- merge gate (Ch.9.1) ---------------------------------------------------------------------

def merge(ctx, task_dir: Path, allow_failing_checks: bool = False) -> str:
    human = ctx.require_human()
    outcome = resolve.read_outcome(task_dir)
    if outcome.kind != "pr-opened" or not outcome.pr_url:
        raise ActionError(f"{resolve.label(task_dir)} has no PR awaiting review")
    found = gh.repo_from_pr_url(outcome.pr_url)
    if not found:
        raise ActionError(f"can't parse PR url {outcome.pr_url}")
    repo, number = found
    pr = gh.pr_view(repo, number)
    if pr.get("state") != "OPEN":
        raise ActionError(f"PR {outcome.pr_url} is {pr.get('state')}, not open")
    checks = gh.checks_summary(pr)
    if checks == "failing" and not allow_failing_checks:
        raise ActionError(f"checks are failing on {outcome.pr_url}")
    gh.approve_and_merge(repo, number, body=f"Approved by {human} via DAGS")
    record_merged(ctx, task_dir, outcome.pr_url, merged_by=human)
    return outcome.pr_url


def record_merged(ctx, task_dir: Path, pr_url: str, merged_by: str | None = None) -> None:
    """Done = PR merged (plan §2.7). Idempotent."""
    if resolve.is_done(task_dir):
        return
    L.complete(ctx, task_dir, "done", pr_url=pr_url, merged_by=merged_by, merged_utc=timeutil.iso())
    ref = TaskRef(str(resolve.read_meta(task_dir)["key"]))
    try:
        ctx.backend.set_status(ref, "done")
    except Exception as e:  # noqa: BLE001
        log.warning("backend set_status(done) failed for %s: %s", ref.key, e)


# -- links (Ch.10.2 "Open ticket / PR") --------------------------------------------------------

def ticket_url(ctx, task_dir: Path) -> str | None:
    meta = resolve.read_meta(task_dir)
    if meta.get("issue_url"):
        return str(meta["issue_url"])
    try:
        return ctx.backend.web_url(TaskRef(str(meta["key"])))
    except Exception:  # noqa: BLE001
        return None


def pr_url(task_dir: Path) -> str | None:
    return resolve.read_outcome(task_dir).pr_url


def open_url(url: str, run=subprocess.run, platform: str | None = None) -> None:
    platform = platform or sys.platform
    if platform != "darwin":
        raise ActionError(f"open this link yourself: {url}")
    run(["open", url], check=False)


# -- branch protection (Ch.5.5, plan §2.11) — printed, and only applied by a human ---------

def protection_body(approvals: int = 1) -> dict:
    return {
        "required_status_checks": None,
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "required_approving_review_count": approvals,
            "dismiss_stale_reviews": True,
        },
        "restrictions": None,
    }


def protection_command(repo: str, branch: str = "main") -> list[str]:
    return ["api", f"repos/{repo}/branches/{branch}/protection", "--method", "PUT", "--input", "-"]


def require_known_human(ctx) -> str:
    try:
        return ctx.require_human()
    except ConfigError as e:
        raise ActionError(str(e)) from e
