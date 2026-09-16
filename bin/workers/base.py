"""The Worker port (whitepaper Ch.7.3).

A worker is whatever implements one claimed task: an AI CLI in a terminal, or
a human in their IDE. The scheduler only ever calls ``dispatch``; completion
is detected from the output contract, never from the worker process.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedTask:
    key: str
    short: str
    title: str
    claim_id: str
    autonomy: str
    repo: str | None
    branch: str


Launch = Callable[[list[str]], None]


def default_launch(cmd: list[str]) -> None:
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


@runtime_checkable
class Worker(Protocol):
    name: str
    label: str
    human: bool

    def dispatch(self, task: ClaimedTask, worktree: Path) -> None: ...


class MacWorker:
    """Shared plumbing for the macOS launchers (plan §2.14)."""
    name = "base"
    label = "base"
    human = True

    def __init__(self, local: dict | None = None, launch: Launch | None = None, platform: str | None = None):
        self.local = local or {}
        self.launch = launch or default_launch
        self.platform = platform or sys.platform

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise WorkerError(f"the {self.label} launcher is macOS-only (plan §2.14); "
                              f"open {self.label} on the worktree yourself")

    def readme(self, worktree: Path) -> Path:
        return worktree / ".swarm-task" / "README.md"

    def dispatch(self, task: ClaimedTask, worktree: Path) -> None:
        self._require_macos()
        for cmd in self.commands(task, Path(worktree)):
            self.launch(cmd)

    def commands(self, task: ClaimedTask, worktree: Path) -> list[list[str]]:  # pragma: no cover
        raise NotImplementedError


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
