"""The activity feed (whitepaper Ch.10.3): ledger records -> plain English,
always attributed to a person from humans.yaml (Ch.10.6)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import resolve
from dags import records as R

FEED_DIRS = ("claims", "withdrawals", "arbitration", "completions", "plan-reviews", "events", "test-scope")


@dataclass(frozen=True)
class Event:
    clock: int
    path: str
    text: str
    kind: str
    wall: str | None = None     # the record's wall_utc, for notification timestamps
    machine: str | None = None
    task: str | None = None     # the task's key, for filtering (None for control/priority/quota)
    task_label: str | None = None


def _name(human) -> str:
    return str(human or "someone").capitalize()


def _task_label(path: Path) -> str:
    task_dir = path.parent.parent
    if (task_dir / "meta.yaml").exists():
        return resolve.label(task_dir)
    return task_dir.name


def _quote(text) -> str:
    return f" — '{text}'" if text else ""


def _describe_event(task: str, who: str, machine: str, data: dict) -> str | None:
    """Records in a task's ``events/``: what a checkpoint change meant."""
    k = data.get("kind")
    worker = data.get("worker") or "the worker"
    if k == "plan-submitted":
        tail = "self-approved (auto-pr)" if data.get("self_approved") else "waiting for review"
        return f"{worker} submitted a plan for {task} ({machine}) — {tail}"
    if k == "needs-human":
        return f"{task} needs a human decision{_quote(data.get('question'))}"
    if k == "worker-dispatched":
        return f"{who}'s swarm handed {task} to {worker} ({machine})"
    if k == "awaiting-worker":
        return f"{task} is waiting for a worker on {machine}"
    if k == "still-working":
        return f"{who} confirmed {task} is still being worked on ({machine})"
    if k == "pause-requested":
        minutes = int(data.get("grace_s") or 0) // 60
        within = f" within {minutes} minutes" if minutes else ""
        return (f"The quota was lowered: {task} was asked to record its progress and stop{within} "
                f"({machine})")
    if k == "human-answered":
        return f"{who} answered {task}'s question{_quote(data.get('answer'))}"
    if k == "autonomy-changed":
        return (f"{who} changed the autonomy of {task} from {data.get('old')} to {data.get('new')}"
                f"{_quote(data.get('reason'))}")
    if k == "unparked":
        return f"{who} unparked {task}{_quote(data.get('reason'))}"
    if k == "pause-lifted":
        return f"The quota was raised again: {task} can carry on ({machine})"
    return None


def describe(root: Path, path: Path, data: dict) -> Event | None:
    ev = _describe(root, path, data)
    if ev is None:
        return None
    wall = data.get("wall_utc")
    task = label = None
    parts = path.relative_to(root).parts
    if parts[0] == "tasks" and len(parts) >= 5:
        task_dir = path.parent.parent
        task = str(R.load_yaml(task_dir / "meta.yaml").get("key") or task_dir.name)
        label = resolve.label(task_dir)
    return replace(ev, wall=str(wall) if wall else None, machine=data.get("machine"), task=task,
                   task_label=label)


def _describe(root: Path, path: Path, data: dict) -> Event | None:
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
        elif reason == "parked-by-human":
            why = f" — '{data['note']}'" if data.get("note") else ""
            text = f"{data.get('human') or machine} parked {task}{why}; it stays out of the queue until unparked"
        elif reason == "quota":
            text = f"{machine} paused {task} because the quota was lowered; it resumes from its checkpoint"
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
    if kind == "plan-reviews":
        if data.get("decision") == "approved":
            text = f"{who} approved the plan for {task}{_quote(data.get('note'))}"
        else:
            text = f"{who} sent the plan for {task} back{_quote(data.get('note'))}"
        return Event(clock, rel, text, f"plan-review:{data.get('decision')}")
    if kind == "test-scope":
        if data.get("kind") == "question":
            text = (f"{machine}'s worker asks which tests to run for {task}: recommends "
                    f"{(data.get('proposal') or {}).get('recommendation')}")
        elif data.get("self_accepted"):
            text = f"{machine}'s worker accepted its own test scope for {task}: {data.get('scope')}"
        else:
            enough = " (enough for done)" if data.get("targeted_enough") else ""
            text = f"{who} answered the test scope for {task}: {data.get('scope')}{enough}{_quote(data.get('note'))}"
        return Event(clock, rel, text, f"test-scope:{data.get('kind')}")
    if kind == "events":
        text = _describe_event(task, who, machine, data)
        return Event(clock, rel, text, f"event:{data.get('kind')}") if text else None
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


def heartbeat_summary(root: Path) -> list[Event]:
    """One line per task and machine: when it was last heard from. Heartbeats are rewritten in
    place, so only the latest survives; this is the collapsed form of them."""
    root = Path(root)
    out = []
    for d in resolve.task_dirs(root):
        for path, data in R.read_dir(d / "heartbeats"):
            machine = str(data.get("machine") or path.stem)
            label = resolve.label(d)
            wall = data.get("wall_utc")
            seen = f" (last at {wall})" if wall else ""
            out.append(Event(R.clock_of(data), path.relative_to(root).as_posix(),
                             f"{machine} is working on {label}{seen}", "heartbeat",
                             wall=str(wall) if wall else None, machine=machine,
                             task=str(R.load_yaml(d / "meta.yaml").get("key") or d.name),
                             task_label=label))
    return sorted(out, key=lambda e: (e.clock, e.path))


def select(events: list[Event], task: str | None = None, machine: str | None = None,
           kind: str | None = None) -> list[Event]:
    """Filter by task (key or short label, case-insensitive), machine and kind prefix."""
    def keep(e: Event) -> bool:
        if task and task.lower() not in ((e.task or "").lower(), (e.task_label or "").lower()):
            return False
        if machine and e.machine != machine:
            return False
        return not kind or e.kind.startswith(kind)
    return [e for e in events if keep(e)]
