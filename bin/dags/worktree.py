"""Per-task git worktrees and the injected skill (whitepaper Ch.7.3, plan §2.13).

Every dispatch gets its own worktree under ``.worktrees/<TASK>`` on branch
``swarm/<TASK>``. A resumed task reuses the local or remote branch, so the
next worker continues from whatever the last one pushed.
"""
from __future__ import annotations

import json
import logging
import shutil
import stat
import sys
from pathlib import Path

import resolve
from dags import records as R
from dags.gitsync import git, git_out

log = logging.getLogger("dags.worktree")

SKILL_DIR = ".swarm-task"
SKILL_FILES = ("README.md", "swarm-task")


def task_slug(task_dir: Path) -> str:
    return R.slug(resolve.label(task_dir))


def branch_name(task_dir: Path) -> str:
    return f"swarm/{task_slug(task_dir)}"


def worktree_path(ctx, task_dir: Path) -> Path:
    return ctx.worktrees_dir / task_slug(task_dir)


def _has_ref(repo: Path, ref: str) -> bool:
    return git(["rev-parse", "--verify", "--quiet", ref], repo, check=False).returncode == 0


def ensure(ctx, task_dir: Path, repo_path: Path, base: str = "main") -> Path:
    wt = worktree_path(ctx, task_dir)
    branch = branch_name(task_dir)
    if (wt / ".git").exists():
        return wt
    git(["worktree", "prune"], repo_path, check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _has_ref(repo_path, f"refs/heads/{branch}"):
        git(["worktree", "add", "-q", str(wt), branch], repo_path)
    elif _has_ref(repo_path, f"refs/remotes/origin/{branch}"):
        git(["worktree", "add", "-q", "--track", "-b", branch, str(wt), f"origin/{branch}"], repo_path)
    else:
        start = f"origin/{base}" if _has_ref(repo_path, f"refs/remotes/origin/{base}") else base
        git(["worktree", "add", "-q", "--no-track", "-b", branch, str(wt), start], repo_path)
    return wt


def remove(repo_path: Path, wt: Path) -> None:
    if wt.exists():
        git(["worktree", "remove", "--force", str(wt)], repo_path, check=False)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    git(["worktree", "prune"], repo_path, check=False)


def context_for(ctx, task_dir: Path, claim_id: str, worker: str | None = None) -> dict:
    meta = resolve.read_meta(task_dir)
    repo = meta.get("repo")
    rcfg = ctx.repo_config(repo) if repo else {}
    return {
        "task_key": meta.get("key"),
        "short": resolve.label(task_dir),
        "title": meta.get("title"),
        "issue_url": meta.get("issue_url"),
        "autonomy": meta.get("autonomy"),
        "claim_id": claim_id,
        "machine": ctx.identity,
        "worker": worker,
        "repo": repo,
        "base": rcfg.get("base", "main"),
        "branch": branch_name(task_dir),
        "test_command": rcfg.get("test_command"),
        "coordination_root": str(ctx.root),
        "task_dir": str(task_dir.relative_to(ctx.root)),
        "swarm_py": str(ctx.bin_dir / "swarm.py"),
        "python": sys.executable,
    }


def inject(ctx, wt: Path, spec_md: str, context: dict) -> Path:
    """cp -r bin/skill .swarm-task; spec.md; conventions.md (Ch.7.3)."""
    dest = wt / SKILL_DIR
    dest.mkdir(parents=True, exist_ok=True)
    for name in SKILL_FILES:
        shutil.copyfile(ctx.bin_dir / "skill" / name, dest / name)
    tool = dest / "swarm-task"
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (dest / "spec.md").write_text(spec_md, encoding="utf-8")
    if ctx.conventions.exists():
        shutil.copyfile(ctx.conventions, dest / "conventions.md")
    (dest / "context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    return dest


def strip_skill(wt: Path) -> bool:
    """rm -rf .swarm-task/ — done by the poller/Board, never by the worker."""
    dest = wt / SKILL_DIR
    if dest.exists():
        shutil.rmtree(dest)
        return True
    return False


def is_excluded(wt: Path) -> bool:
    r = git(["check-ignore", "-q", f"{SKILL_DIR}/README.md"], wt, check=False)
    return r.returncode == 0


def last_commit_time(wt: Path) -> str | None:
    if not (wt / ".git").exists():
        return None
    r = git(["log", "-1", "--format=%cI"], wt, check=False)
    return r.stdout.strip() or None


def commits_ahead(wt: Path, base: str) -> int:
    ref = f"origin/{base}" if _has_ref(wt, f"refs/remotes/origin/{base}") else base
    r = git(["rev-list", "--count", f"{ref}..HEAD"], wt, check=False)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def head(wt: Path) -> str:
    return git_out(["rev-parse", "HEAD"], wt)


def fetch_base(wt: Path, base: str) -> None:
    git(["fetch", "-q", "origin", base], wt)


def behind(wt: Path, base: str) -> int:
    r = git(["rev-list", "--count", f"HEAD..origin/{base}"], wt, check=False)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def merge_in_progress(wt: Path) -> bool:
    return git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, check=False).returncode == 0


def unmerged_paths(wt: Path) -> list[str]:
    out = git_out(["ls-files", "-u"], wt)
    return sorted({line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line})


def conflict_markers(wt: Path, base: str) -> list[str]:
    """Files whose lines this branch adds (against the base) start a conflict marker."""
    out = git(["diff", "-U0", f"origin/{base}...HEAD"], wt, check=False).stdout
    files, current = set(), None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith(("+<<<<<<< ", "+>>>>>>> ")) and current:
            files.add(current)
    return sorted(files)


def update_to_base(wt: Path, base: str, *, rebase: bool) -> list[str]:
    """Bring the branch up to date with ``origin/<base>`` (fetched already). Returns the
    conflicted files, with the merge left in progress, or [] when the update is done.
    A conflicting rebase is aborted and redone as a merge: one conflict set to resolve."""
    ref = f"origin/{base}"
    if rebase and git(["rebase", "-q", ref], wt, check=False).returncode == 0:
        return []
    if rebase:
        git(["rebase", "--abort"], wt, check=False)
    r = git(["merge", "-q", "--no-edit", ref], wt, check=False)
    if r.returncode == 0:
        return []
    files = git_out(["diff", "--name-only", "--diff-filter=U"], wt, check=False).splitlines()
    if not files:
        git(["merge", "--abort"], wt, check=False)
        raise RuntimeError(f"git merge {ref} failed: {r.stderr.strip() or r.stdout.strip()}")
    return sorted(files)


def remote_has_branch(wt: Path, branch: str) -> bool:
    return git(["ls-remote", "--exit-code", "--heads", "origin", branch], wt, check=False).returncode == 0



def cleanup(ctx, task_dir: Path) -> bool:
    """Remove this machine's worktree for a finished task (merged or rejected).
    Returns True when something was removed. Safe to call repeatedly."""
    wt = worktree_path(ctx, task_dir)
    if not wt.exists():
        return False
    repo = resolve.read_meta(task_dir).get("repo")
    if repo:
        remove(ctx.repo_path(str(repo)), wt)
    else:
        shutil.rmtree(wt, ignore_errors=True)
    return not wt.exists()
