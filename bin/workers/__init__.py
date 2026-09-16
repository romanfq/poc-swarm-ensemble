"""Worker registry. The order is the order of the Board prompt (Ch.10.7)."""
from __future__ import annotations

from workers.base import ClaimedTask, Worker, WorkerError  # noqa: F401
from workers.claude import ClaudeWorker
from workers.intellij import IntelliJWorker
from workers.vscode import VSCodeWorker

WORKERS = {w.name: w for w in (ClaudeWorker, IntelliJWorker, VSCodeWorker)}
LETTERS = dict(zip("abc", WORKERS))


def resolve_name(choice: str) -> str:
    c = str(choice).strip().lower()
    if c in LETTERS:
        return LETTERS[c]
    for name, cls in WORKERS.items():
        if c in (name, cls.label.lower()):
            return name
    raise WorkerError(f"unknown worker {choice!r} (choose one of: {', '.join(WORKERS)})")


def get(name: str, local: dict | None = None, launch=None, platform: str | None = None) -> Worker:
    return WORKERS[resolve_name(name)](local=local, launch=launch, platform=platform)


def allowed_for(autonomy: str) -> list[str]:
    """human-must-scope tasks never go to an AI worker (plan §2.8)."""
    return [n for n, cls in WORKERS.items() if cls.human or autonomy != "human-must-scope"]


def prompt_text(short: str) -> str:
    lines = [f"[swarm-board] I have claimed {short} for completion, who is my worker?"]
    for letter, name in LETTERS.items():
        lines.append(f"  {letter}) {WORKERS[name].label}")
    return "\n".join(lines)
