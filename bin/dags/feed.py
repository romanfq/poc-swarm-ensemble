"""The activity feed (whitepaper Ch.10.3): ledger records -> plain English,
always attributed to a person from humans.yaml (Ch.10.6)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import resolve
from dags import records as R

FEED_DIRS = ("claims", "withdrawals", "arbitration", "completions")


@dataclass(frozen=True)
class Event:
    clock: int
    path: str
    text: str
    kind: str


def _name(human) -> str:
    return str(human or "someone").capitalize()


def _task_label(path: Path) -> str:
    task_dir = path.parent.parent
    if (task_dir / "meta.yaml").exists():
        return resolve.label(task_dir)
    return task_dir.name


def describe(root: Path, path: Path, data: dict) -> Event | None:
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    clock = R.clock_of(data)
    who = _name(data.get("human"))
    machine = data.get("machine", "?")
    top = parts[0]
    if top == "control":
        action = data.get("action")
        text = {
            "pause": f"{who} paused their swarm ({machine})",
            "resume": f"{who} resumed their swarm ({machine})",
            "stop": f"{who} stopped their swarm ({machine})",
            "start": f"{who} started their swarm ({machine}, quota share {data.get('quota_share', '?')})",
            "throttle": f"{who} throttled {machine} to quota share {data.get('quota_share')}",
        }.get(action, f"{who}: {action} on {machine}")
        return Event(clock, rel, text, f"control:{action}")
    if top == "priority":
        verb = "took over" if data.get("action") == "takeover" else "released"
        return Event(clock, rel, f"{who}'s swarm {verb} {data.get('epic')}", f"priority:{data.get('action')}")
    if top == "quota":
        reason = f" — '{data['reason']}'" if data.get("reason") else ""
        return Event(clock, rel, f"{who} set the global quota to {data.get('n')}{reason}", "quota")
    if top != "tasks" or len(parts) < 5 or parts[-2] not in FEED_DIRS:
        return None
    task = _task_label(path)
    kind = parts[-2]
    if kind == "claims":
        return Event(clock, rel, f"{who}'s swarm claimed {task} ({machine})", "claim")
    if kind == "withdrawals":
        reason = data.get("reason")
        if reason == "lost-race":
            text = f"{machine} lost the race for {task} to {data.get('winner')} and withdrew"
        elif reason == "released":
            text = f"{machine} released {task}"
        else:
            text = f"{machine} gave up {task} ({reason})"
        return Event(clock, rel, text, "withdrawal")
    if kind == "arbitration":
        why = f" — '{data.get('reason')}'" if data.get("reason") else ""
        if data.get("action") == "withdraw":
            text = f"{who} lifted the arbitration on {task}{why}"
        elif str(data.get("winner")) in ("none", "None", ""):
            text = f"{who} froze {task}{why}"
        else:
            winner = str(data.get("winner"))
            text = f"{who} arbitrated {task}: {winner.rsplit('-', 1)[0]} wins{why}"
        return Event(clock, rel, text, "arbitration")
    if kind == "completions":
        k = data.get("kind")
        url = data.get("pr_url") or ""
        text = {
            "pr-opened": f"{data.get('worker') or machine} finished {task}. The PR can be found at {url}",
            "done": f"{task} is done" + (" (merged)" if not data.get("imported") else " (closed in the tracker)"),
            "reopened": f"Changes requested on {task} — back in the queue",
            "rejected": f"The approach for {task} was rejected (PR closed) — needs re-planning",
            "replanned": f"{task} was re-planned and is ready again",
        }.get(k)
        return Event(clock, rel, text, f"completion:{k}") if text else None
    return None


def all_events(root: Path) -> list[Event]:
    root = Path(root)
    out = []
    for top in ("control", "priority", "quota"):
        for path, data in R.read_dir(root / top):
            e = describe(root, path, data)
            if e:
                out.append(e)
    for d in resolve.task_dirs(root):
        for sub in FEED_DIRS:
            for path, data in R.read_dir(d / sub):
                e = describe(root, path, data)
                if e:
                    out.append(e)
    return sorted(out, key=lambda e: (e.clock, e.path))


def new_events(root: Path, seen: set[str]) -> list[Event]:
    """Events whose record wasn't seen before; updates ``seen`` in place."""
    fresh = [e for e in all_events(root) if e.path not in seen]
    seen.update(e.path for e in fresh)
    return fresh
