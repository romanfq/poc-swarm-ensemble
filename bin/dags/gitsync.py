"""Plain-git sync for the coordination repo (whitepaper Ch.4, Ch.6.2).

All reads and writes against the ledger are pull / commit / push. Several
threads (scheduler, poller) and processes (daemon, Board, a worker's
``swarm-task done``) share one clone, so every transaction holds both an
in-process lock and an OS file lock on ``.swarm/git.lock``.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterable


class GitError(RuntimeError):
    def __init__(self, args, result):
        self.args_ = args
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.returncode = result.returncode
        super().__init__(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")


def git(args: list[str], cwd, *, check: bool = True, env: dict | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        full_env.update(env)
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            env=full_env, input=input)
    if check and result.returncode != 0:
        raise GitError(args, result)
    return result


def git_out(args: list[str], cwd, **kw) -> str:
    return git(args, cwd, **kw).stdout.strip()


_thread_locks: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    with _registry_lock:
        return _thread_locks.setdefault(str(path), threading.RLock())


class RepoLock:
    """Re-entrant within a thread; exclusive across threads and processes."""

    def __init__(self, lock_file: Path):
        self.lock_file = Path(lock_file)
        self._tlock = _thread_lock(self.lock_file)
        self._local = threading.local()

    def __enter__(self):
        self._tlock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_file, "a+")
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._local.fh = fh
        self._local.depth = depth + 1
        return self

    def __exit__(self, *exc):
        self._local.depth -= 1
        if self._local.depth == 0:
            fh = self._local.fh
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            self._local.fh = None
        self._tlock.release()
        return False


class Coord:
    """The coordination repo clone."""

    def __init__(self, root, lock_file=None, remote: str = "origin"):
        self.root = Path(root)
        self.remote = remote
        self.lock = RepoLock(lock_file or self.root / ".swarm" / "git.lock")

    # -- info ---------------------------------------------------------------
    def branch(self) -> str:
        return git_out(["rev-parse", "--abbrev-ref", "HEAD"], self.root)

    def has_remote(self) -> bool:
        return self.remote in git_out(["remote"], self.root).split()

    def _remote_branch_exists(self, branch: str) -> bool:
        r = git(["ls-remote", "--exit-code", "--heads", self.remote, branch], self.root, check=False)
        return r.returncode == 0

    # -- sync ---------------------------------------------------------------
    def pull(self) -> None:
        if not self.has_remote():
            return
        with self.lock:
            branch = self.branch()
            if not self._remote_branch_exists(branch):
                return
            r = git(["pull", "--rebase", "--autostash", "-q", self.remote, branch], self.root, check=False)
            if r.returncode != 0:
                self._recover_rebase()
                # Append-only files cannot conflict; only single-writer files
                # (checkpoint/heartbeat) can, and for those the remote wins —
                # a stale writer discovers it lost on its next resolve().
                git(["pull", "--rebase", "--autostash", "-q", "-X", "ours", self.remote, branch], self.root)

    def _recover_rebase(self) -> None:
        if (self.root / ".git" / "rebase-merge").exists() or (self.root / ".git" / "rebase-apply").exists():
            git(["rebase", "--abort"], self.root, check=False)

    def commit(self, paths: Iterable, message: str) -> bool:
        paths = [str(Path(p).resolve().relative_to(self.root.resolve())) for p in paths]
        if not paths:
            return False
        with self.lock:
            git(["add", "--", *paths], self.root)
            if git(["diff", "--cached", "--quiet"], self.root, check=False).returncode == 0:
                return False
            git(["commit", "-q", "--no-verify", "-m", message], self.root)
            return True

    def push(self, retries: int = 6) -> None:
        if not self.has_remote():
            return
        with self.lock:
            branch = self.branch()
            delay = 0.5
            for attempt in range(retries):
                r = git(["push", "-q", "-u", self.remote, f"HEAD:{branch}"], self.root, check=False)
                if r.returncode == 0:
                    return
                if attempt == retries - 1:
                    raise GitError(["push"], r)
                # non-fast-forward: somebody else pushed first; rebase and retry
                self.pull()
                time.sleep(delay + random.random() * delay)
                delay = min(delay * 2, 8)

    def transaction(self, build: Callable[[], Iterable], message: str, push: bool = True) -> list[Path]:
        """pull -> build() writes files and returns their paths -> commit -> push.
        ``build`` runs after the pull, under the lock, so any logical clock it
        computes sees everything synced so far."""
        with self.lock:
            self.pull()
            paths = [Path(p) for p in (build() or [])]
            if self.commit(paths, message) and push:
                self.push()
            return paths

    def unpushed(self) -> int:
        if not self.has_remote():
            return 0
        r = git(["rev-list", "--count", "@{u}..HEAD"], self.root, check=False)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    def recent_files(self, since_rev: str | None) -> list[str]:
        if not since_rev:
            return []
        r = git(["diff", "--name-only", "--diff-filter=A", f"{since_rev}..HEAD"], self.root, check=False)
        return [line for line in r.stdout.splitlines() if line]

    def head(self) -> str | None:
        r = git(["rev-parse", "HEAD"], self.root, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def author_email(self, path) -> str | None:
        rel = str(Path(path).resolve().relative_to(self.root.resolve()))
        r = git(["log", "--diff-filter=A", "--format=%ae", "-1", "--", rel], self.root, check=False)
        return r.stdout.strip() or None


@contextlib.contextmanager
def nullcontext():
    yield
