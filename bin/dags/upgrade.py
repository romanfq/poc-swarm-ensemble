"""Upgrade a coordination repo's pinned ``bin/`` and ``templates/`` from a checkout (D33).

The change set is a pure function of (source tree, target tree, recorded
``.dags-source``); the git calls and the daemon check are injected, so the tests
need no daemon and no remote. Only ``bin/`` and ``templates/`` are managed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dags.gitsync import git as _git

MANAGED = ("bin", "templates")
RECORD = ".dags-source"
WRITER = "swarm.py update-from-source"
GitFn = Callable[..., object]


@dataclass
class SourceInfo:
    path: Path
    ok: bool                      # bin/dags exists
    branch: str = ""
    sha: str = ""
    subject: str = ""
    dirty: bool = False
    behind: int | None = None     # commits behind the local origin/main ref; None = cannot tell


@dataclass
class Plan:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    pinned_sha: str | None = None
    local_edits: bool = False
    already_current: bool = False
    refusals: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.changed) + len(self.removed)


# -- trees ------------------------------------------------------------------------

def _skip(name: str) -> bool:
    return name == "__pycache__" or name.endswith(".pyc")


def snapshot_tree(root: Path) -> dict[str, str]:
    """{relative path: sha256} for the managed directories under ``root``."""
    out: dict[str, str] = {}
    for top in MANAGED:
        base = Path(root) / top
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not _skip(d))
            for name in sorted(filenames):
                if _skip(name):
                    continue
                p = Path(dirpath) / name
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def tree_digest(tree: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(f"{path}\0{tree[path]}\n".encode())
    return h.hexdigest()


def read_record(root: Path) -> dict[str, str]:
    try:
        text = (Path(root) / RECORD).read_text()
    except FileNotFoundError:
        return {}
    rec = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            rec[key.strip()] = value.strip()
    return rec


def render_record(info: SourceInfo, tree: dict[str, str], now: datetime) -> str:
    return (f"source: {info.sha}\nbranch: {info.branch}\nsubject: {info.subject}\n"
            f"upgraded: {now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"by: {WRITER}\ntree: {tree_digest(tree)}\n")


# -- the pure part ----------------------------------------------------------------

def plan_upgrade(source_tree: dict[str, str], target_tree: dict[str, str], recorded: dict[str, str], *,
                 info: SourceInfo, daemon_pid: int | None = None, same_repo: bool = False,
                 allow_branch: bool = False, allow_dirty: bool = False,
                 commit: bool = False, staged: list[str] | None = None) -> Plan:
    p = Plan(pinned_sha=recorded.get("source") or None)
    p.added = sorted(set(source_tree) - set(target_tree))
    p.removed = sorted(set(target_tree) - set(source_tree))
    p.changed = sorted(f for f in set(source_tree) & set(target_tree) if source_tree[f] != target_tree[f])
    if recorded.get("tree"):
        p.local_edits = tree_digest(target_tree) != recorded["tree"]
    p.already_current = p.total == 0 and p.pinned_sha == info.sha

    if daemon_pid:
        p.refusals.append(f"a daemon is running here (pid {daemon_pid}): stop this machine first: `swarm.py stop`")
    if same_repo:
        p.refusals.append("SOURCE is this repo; give a poc-swarm-ensemble checkout")
    elif not info.ok:
        p.refusals.append(f"SOURCE does not look right: {info.path}/bin/dags is missing")
    else:
        if not allow_branch:
            if info.branch != "main":
                p.refusals.append(f"SOURCE is on branch {info.branch or '(detached)'}, not main "
                                  "(--allow-branch to copy it anyway)")
            if info.behind is None:
                p.refusals.append("SOURCE has no origin/main ref to compare with; run `git fetch` in SOURCE "
                                  "(--allow-branch to skip the check)")
            elif info.behind > 0:
                p.refusals.append(f"SOURCE is {info.behind} commit(s) behind origin/main: run `git pull` in SOURCE "
                                  "(--allow-branch to copy it anyway)")
        if info.dirty and not allow_dirty:
            p.refusals.append("SOURCE has uncommitted changes (--allow-dirty to copy the working tree anyway)")
    if commit and staged:
        p.refusals.append("something is staged already (" + ", ".join(staged[:5]) +
                          "); --commit would include it. Commit or unstage it first")
    return p


# -- git-backed inputs (the git callable is injected) ------------------------------

def read_source(source: Path, git: GitFn = _git) -> SourceInfo:
    source = Path(source).resolve()
    info = SourceInfo(path=source, ok=(source / "bin" / "dags").is_dir())
    if not info.ok:
        return info

    def out(*args: str) -> str | None:
        r = git(list(args), source, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    info.branch = out("rev-parse", "--abbrev-ref", "HEAD") or ""
    info.branch = "" if info.branch == "HEAD" else info.branch
    info.sha = out("rev-parse", "HEAD") or ""
    info.subject = out("log", "-1", "--format=%s") or ""
    info.dirty = bool(out("status", "--porcelain", "--", *MANAGED))
    behind = out("rev-list", "--count", "HEAD..origin/main")
    info.behind = int(behind) if behind is not None and behind.isdigit() else None
    return info


def staged_files(root: Path, git: GitFn = _git) -> list[str]:
    r = git(["diff", "--cached", "--name-only"], root, check=False)
    return r.stdout.split() if r.returncode == 0 else []


# -- the side effects --------------------------------------------------------------

def apply(source: Path, root: Path, info: SourceInfo, now: datetime | None = None) -> dict[str, str]:
    """Copy into ``<name>.new`` beside the real directories, check, swap last, write .dags-source."""
    source, root = Path(source), Path(root)
    now = now or datetime.now(timezone.utc)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    staged = []
    try:
        for name in MANAGED:
            new = root / f"{name}.new"
            shutil.rmtree(new, ignore_errors=True)
            if (source / name).is_dir():
                shutil.copytree(source / name, new, ignore=ignore)
                staged.append(name)
        if not ((root / "bin.new" / "dags").is_dir() and (root / "bin.new" / "swarm.py").is_file()):
            raise RuntimeError("the copy of bin/ lacks dags/ or swarm.py; nothing was changed")
    except BaseException:
        for name in MANAGED:
            shutil.rmtree(root / f"{name}.new", ignore_errors=True)
        raise
    # Last step: swap. Anything this process still needs from bin/ was imported by now.
    for name in MANAGED:
        real, old = root / name, root / f"{name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if real.exists():
            os.rename(real, old)
        if name in staged:
            os.rename(root / f"{name}.new", real)
        shutil.rmtree(old, ignore_errors=True)
    tree = snapshot_tree(root)
    (root / RECORD).write_text(render_record(info, tree, now))
    return tree


def commit_message(info: SourceInfo) -> str:
    return f"Upgrade to poc-swarm-ensemble {info.sha[:7]}"


def commit(root: Path, info: SourceInfo, git: GitFn = _git) -> list[str]:
    """Commit only the upgrade, locally. Returns the other changed paths that were left alone."""
    root = Path(root)
    git(["add", "--", *MANAGED, RECORD], root)
    git(["commit", "-m", commit_message(info)], root)
    r = git(["status", "--porcelain"], root, check=False)
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]
