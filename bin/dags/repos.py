"""Code repositories a machine works in (whitepaper Ch.5.3 step 2, plan §2.13).

Per clone, never per repo on GitHub: idempotently point ``commit.template`` at
the coordination repo's template and keep ``.swarm-task/`` in the shared
``info/exclude`` (which every linked worktree resolves back to).
"""
from __future__ import annotations

import logging
from pathlib import Path

import resolve
from dags import gh
from dags.gitsync import git, git_out

log = logging.getLogger("dags.repos")

EXCLUDE_LINE = ".swarm-task/"


class RepoError(RuntimeError):
    pass


def referenced_repos(ctx) -> list[str]:
    """Repos named by tasks that aren't finished yet."""
    out = set()
    for d in resolve.task_dirs(ctx.root):
        meta = resolve.read_meta(d)
        if meta.get("repo") and not meta.get("is_epic") and not resolve.is_done(d):
            out.add(str(meta["repo"]))
    return sorted(out)


def configure(ctx, path: Path) -> None:
    template = (ctx.templates_dir / "commit-message.txt").resolve()
    current = git(["config", "--local", "--get", "commit.template"], path, check=False).stdout.strip()
    if current != str(template):
        git(["config", "--local", "commit.template", str(template)], path)
    common = Path(git_out(["rev-parse", "--git-common-dir"], path))
    if not common.is_absolute():
        common = (path / common).resolve()
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if EXCLUDE_LINE not in [ln.strip() for ln in lines]:
        with open(exclude, "a", encoding="utf-8") as f:
            if lines and lines[-1] != "":
                f.write("\n")
            f.write(EXCLUDE_LINE + "\n")


def ensure(ctx, repo: str, fetch: bool = True) -> Path:
    path = ctx.repo_path(repo)
    mapped = repo in (ctx.local.get("repos") or {})
    if not (path / ".git").exists():
        if mapped:
            raise RepoError(f"{repo}: .swarm/local.yaml points at {path}, which is not a git checkout")
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s into %s", repo, path)
        gh.gh(["repo", "clone", repo, str(path)])
    elif fetch:
        r = git(["fetch", "--prune", "-q", "origin"], path, check=False)
        if r.returncode != 0:
            log.warning("%s: fetch failed: %s", repo, r.stderr.strip())
    configure(ctx, path)
    return path


def ensure_all(ctx, fetch: bool = True) -> dict[str, Path | Exception]:
    out: dict[str, Path | Exception] = {}
    for repo in referenced_repos(ctx):
        try:
            out[repo] = ensure(ctx, repo, fetch=fetch)
        except Exception as e:  # noqa: BLE001 - reported in the status panel
            out[repo] = e
    return out
