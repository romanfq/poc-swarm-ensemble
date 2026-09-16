"""Worker-side operations behind the injected skill (whitepaper Ch.7.3, Ch.8, Ch.9.3).

``.swarm-task/swarm-task`` is stdlib-only so it runs in any worktree; for
anything that touches the ledger, the backend or GitHub it calls
``swarm.py task ...``, which lands here.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path

import resolve
import workers
from backends.base import TaskRef
from dags import gh, repos, worktree
from dags import ledger as L
from dags import timeutil

log = logging.getLogger("dags.work")


class WorkError(RuntimeError):
    pass


def _ref(task_dir: Path) -> TaskRef:
    return TaskRef(str(resolve.read_meta(task_dir)["key"]))


def _try_backend(ctx, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 - the backend is a mirror; never fail the ledger step
        log.warning("backend update failed: %s", e)


def my_claim(ctx, task_dir: Path) -> str:
    res = resolve.resolve(task_dir, timeutil.now(), ctx.settings.lease_s, ctx.human_names)
    if res.winner is None or res.winner.machine != ctx.identity or res.winner not in res.valid:
        raise L.LostClaim(f"this machine ({ctx.identity}) does not own {resolve.label(task_dir)}")
    return res.winner.id


# ---------------------------------------------------------------------------
# dispatch (Ch.7.3 "Selecting a worker")
# ---------------------------------------------------------------------------

def prepare(ctx, task_dir: Path, claim_id: str, worker: str | None = None) -> Path:
    meta = resolve.read_meta(task_dir)
    repo = meta.get("repo")
    if not repo:
        raise WorkError(f"{resolve.label(task_dir)} has no target repo (add a repo: label)")
    repo_path = repos.ensure(ctx, repo)
    base = ctx.repo_config(repo).get("base", "main")
    wt = worktree.ensure(ctx, task_dir, repo_path, base)
    spec = ctx.backend.get_task(_ref(task_dir)).spec_markdown()
    worktree.inject(ctx, wt, spec, worktree.context_for(ctx, task_dir, claim_id, worker))
    return wt


def choose_worker(ctx, task_dir: Path, choice: str, launch=None, platform: str | None = None) -> str:
    """Record the worker in checkpoint.yaml, inject the skill and hand over."""
    name = workers.resolve_name(choice)
    meta = resolve.read_meta(task_dir)
    autonomy = str(meta.get("autonomy") or "")
    if name not in workers.allowed_for(autonomy):
        raise WorkError(f"{resolve.label(task_dir)} is {autonomy}: only a human worker may take it")
    ctx.coord.pull()
    claim_id = my_claim(ctx, task_dir)
    w = workers.get(name, ctx.local, launch=launch, platform=platform)
    L.require_mine(ctx, task_dir, claim_id)
    wt = prepare(ctx, task_dir, claim_id, name)
    L.update_checkpoint(ctx, task_dir, claim_id, worker=name, worker_label=w.label,
                        branch=worktree.branch_name(task_dir), dispatched_utc=timeutil.iso(),
                        needs_human=None)
    task = workers.ClaimedTask(key=str(meta["key"]), short=resolve.label(task_dir),
                               title=str(meta.get("title") or ""), claim_id=claim_id,
                               autonomy=autonomy, repo=meta.get("repo"),
                               branch=worktree.branch_name(task_dir))
    w.dispatch(task, wt)
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "in-progress")
    return w.label


# ---------------------------------------------------------------------------
# checkpoint notes, plan, feedback (Ch.8, Ch.9.3)
# ---------------------------------------------------------------------------

def note(ctx, task_dir: Path, *, summary=None, tried=(), remaining=None, questions=(), risks=()) -> dict:
    claim_id = my_claim(ctx, task_dir)
    fields = {}
    if summary is not None:
        fields["summary"] = summary
    if remaining is not None:
        fields["remaining"] = list(remaining)
    append = {k: list(v) for k, v in (("tried", tried), ("open_questions", questions), ("risks", risks)) if v}
    return L.update_checkpoint(ctx, task_dir, claim_id, append=append, **fields)


def plan_sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]


def submit_plan(ctx, task_dir: Path, plan_md: str) -> str:
    if not plan_md.strip():
        raise WorkError("plan.md is empty")
    claim_id = my_claim(ctx, task_dir)
    meta = resolve.read_meta(task_dir)
    sha = plan_sha(plan_md)
    fields = {"plan_md": plan_md, "plan_sha": sha, "needs_human": None}
    if meta.get("autonomy") == "auto-pr":
        fields["plan_self_approved"] = sha            # self-review allowed (plan §2.8)
    L.update_checkpoint(ctx, task_dir, claim_id, **fields)
    status = resolve.plan_status(task_dir, ctx.human_names)
    if status != "approved":
        _try_backend(ctx, ctx.backend.post_comment, _ref(task_dir),
                     f"DAGS: plan for review (approve on the Swarm Board or with "
                     f"`swarm.py task approve-plan {resolve.label(task_dir)}`)\n\n{plan_md}")
    return status


def approve_plan(ctx, task_dir: Path, decision: str = "approved", note_text: str = "") -> None:
    cp = L.read_checkpoint(task_dir)
    if not cp.get("plan_sha"):
        raise WorkError(f"{resolve.label(task_dir)} has no plan to review yet")
    L.review_plan(ctx, task_dir, cp["plan_sha"], decision, note_text)


def pr_for(ctx, task_dir: Path) -> tuple[str, str] | None:
    """(repo, number) of this task's PR, from the ledger or the branch."""
    outcome = resolve.read_outcome(task_dir)
    if outcome.pr_url:
        return gh.repo_from_pr_url(outcome.pr_url)
    repo = resolve.read_meta(task_dir).get("repo")
    if not repo:
        return None
    pr = gh.pr_for_branch(repo, worktree.branch_name(task_dir))
    return (repo, str(pr["number"])) if pr else None


def feedback(ctx, task_dir: Path) -> list[dict]:
    found = pr_for(ctx, task_dir)
    if not found:
        return []
    return gh.parse_feedback(gh.pr_view(*found))


def implement_gate(ctx, task_dir: Path) -> dict:
    """What `swarm-task implement` needs: may we proceed, and the context."""
    my_claim(ctx, task_dir)
    status = resolve.plan_status(task_dir, ctx.human_names)
    cp = L.read_checkpoint(task_dir)
    try:
        fb = feedback(ctx, task_dir)
    except (gh.GhError, FileNotFoundError) as e:
        fb = [{"tag": "error", "text": f"could not read PR feedback: {e}"}]
    return {"allowed": status == "approved", "plan_status": status, "checkpoint": cp, "feedback": fb}


def block(ctx, task_dir: Path, question: str) -> None:
    claim_id = my_claim(ctx, task_dir)
    L.update_checkpoint(ctx, task_dir, claim_id, needs_human=question, append={"open_questions": [question]})
    _try_backend(ctx, ctx.backend.post_comment, _ref(task_dir), f"DAGS: worker needs a human decision:\n\n{question}")


def still_working(ctx, task_dir: Path) -> None:
    claim_id = my_claim(ctx, task_dir)
    L.update_checkpoint(ctx, task_dir, claim_id, human_confirmed_utc=timeutil.iso())


def release(ctx, task_dir: Path, reason: str = "released") -> None:
    claim_id = my_claim(ctx, task_dir)
    L.withdraw(ctx, task_dir, claim_id, reason)
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "ready")


# ---------------------------------------------------------------------------
# done — the output contract (Ch.7.3, Ch.4.1, Ch.9.1)
# ---------------------------------------------------------------------------

def _bullets(items) -> str:
    items = [str(i) for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else "None."


def render(ctx, task_dir: Path, cp: dict) -> tuple[str, str]:
    meta = resolve.read_meta(task_dir)
    values = {
        "task_ref": resolve.label(task_dir),
        "summary": (cp.get("summary") or "").strip(),
        "risks": _bullets(cp.get("risks")),
        "open_questions": _bullets(cp.get("open_questions")),
        "issue_ref": meta.get("issue_url") or meta.get("key"),
        "coordination_ref": task_dir.relative_to(ctx.root).as_posix(),
    }
    commit = (ctx.templates_dir / "commit-message.txt").read_text(encoding="utf-8").format(**values)
    body = (ctx.templates_dir / "pr-description.md").read_text(encoding="utf-8").format(**values)
    return commit, body


def run_tests(command: str | None, wt: Path, runner=subprocess.run) -> None:
    if not command:
        return
    r = runner(command, shell=True, cwd=str(wt), capture_output=True, text=True)
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or ""))[-3000:]
        raise WorkError(f"tests failed (`{command}`), not opening a PR (plan §2.10):\n{tail}")


def finish(ctx, task_dir: Path, wt: Path, *, skip_tests: bool = False, test_runner=subprocess.run) -> str:
    from dags.gitsync import git
    wt = Path(wt)
    ctx.coord.pull()
    claim_id = my_claim(ctx, task_dir)
    meta = resolve.read_meta(task_dir)
    label = resolve.label(task_dir)
    if meta.get("autonomy") == "human-must-scope":
        cp0 = L.read_checkpoint(task_dir)
        if not workers.WORKERS.get(cp0.get("worker") or "", workers.WORKERS["claude"]).human:
            raise WorkError(f"{label} is human-must-scope; an AI worker may not open its PR")
    if resolve.plan_status(task_dir, ctx.human_names) != "approved":
        raise WorkError(f"{label}: the plan isn't approved yet (run `swarm-task plan` and get it reviewed)")
    cp = L.read_checkpoint(task_dir)
    if not (cp.get("summary") or "").strip():
        raise WorkError('record a summary first: swarm-task note --summary "..."')
    repo = str(meta["repo"])
    rcfg = ctx.repo_config(repo)
    base = rcfg.get("base", "main")
    branch = worktree.branch_name(task_dir)
    if not skip_tests:
        run_tests(rcfg.get("test_command"), wt, test_runner)

    commit_msg, body = render(ctx, task_dir, cp)
    git(["add", "-A"], wt)
    if git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
        cmd = ["commit", "-q", "-F", "-"]
        bot = ctx.bot_identity()
        if bot:
            cmd.insert(1, f"--author={bot[0]} <{bot[1]}>")
        git(cmd, wt, input=commit_msg)
    if worktree.commits_ahead(wt, base) == 0:
        raise WorkError(f"{label}: no changes to submit on {branch}")

    L.require_mine(ctx, task_dir, claim_id)              # last check before the irreversible steps
    git(["push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}"], wt)
    token = ctx.worker_token()
    existing = gh.pr_for_branch(repo, branch)
    if existing and existing.get("state") == "OPEN":
        url = existing["url"]
        gh.gh(["pr", "comment", str(existing["number"]), "--repo", repo, "--body-file", "-"],
              token=token, input="Updated by the swarm worker.\n\n" + body)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body)
            body_file = f.name
        try:
            out = gh.gh(["pr", "create", "--repo", repo, "--base", base, "--head", branch,
                         "--title", f"[{label}] {meta.get('title') or label}", "--body-file", body_file],
                        token=token)
        finally:
            Path(body_file).unlink(missing_ok=True)
        url = next((ln.strip() for ln in out.splitlines() if "/pull/" in ln), out.strip())

    worker_label = cp.get("worker_label") or cp.get("worker") or "worker"
    L.update_checkpoint(ctx, task_dir, claim_id, pr_url=url, finished_utc=timeutil.iso(), needs_human=None)
    L.complete(ctx, task_dir, "pr-opened", claim_id=claim_id, pr_url=url, worker=worker_label,
               commit=worktree.head(wt))
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "awaiting-review")
    _try_backend(ctx, ctx.backend.post_comment, _ref(task_dir), f"{body}\nPR: {url}\n")
    return url

