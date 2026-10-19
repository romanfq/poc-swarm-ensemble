"""Is any machine running? The check behind moving issues between backends (GH-124).

Export, import and rekey assume nothing else changes the tracker or the ledger. "Running" is read from the
control records through ``ledger.machine_control``: a machine with a ``start`` record whose ``stopped`` is
false. ``pause``, ``resume`` and ``throttle`` never change that (a paused machine still runs ``plan sync``).
Heartbeats exist only for live claims, so an idle machine writes none and they cannot be the test; they only
tell "stopped, but its claims have not lapsed yet" from "stopped".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import resolve
from dags import ledger as L
from dags import records as R
from dags import timeutil


@dataclass(frozen=True)
class Blocker:
    machine: str
    kind: str                       # "running" or "stopping"
    paused: bool = False
    until: datetime | None = None   # stopping: when its last claim lapses

    def advice(self) -> str:
        if self.kind == "stopping":
            when = f" at {timeutil.iso(self.until)}" if self.until else ""
            return (f"{self.machine}: stopped, but a claim it holds still has a fresh heartbeat. "
                    f"Wait for the lease to lapse{when}, then try again")
        paused = " (paused, which still runs plan sync)" if self.paused else ""
        return (f"{self.machine}: running{paused}. Run `swarm.py stop` on that machine; if it crashed without "
                f"stopping, run `swarm.py stop --machine {self.machine}` to record the stop")


def machines(root: Path) -> list[str]:
    """Every machine with a ``start`` record in control/."""
    return sorted({str(d["machine"]) for _, d in R.read_dir(Path(root) / "control")
                   if d.get("action") == "start" and d.get("machine")})


def blockers(root: Path, now: datetime, lease_s: float) -> list[Blocker]:
    root = Path(root)
    out = []
    live: dict[str, datetime] = {}                  # machine -> latest moment one of its claims is still live
    stopped = [m for m in machines(root) if L.machine_control(root, m)["stopped"]]
    if stopped:
        for d in resolve.task_dirs(root):
            res = resolve.resolve(d, now, lease_s)
            heartbeats = resolve.read_heartbeats(d)
            for claim in res.valid:
                if claim.machine in stopped and not resolve.is_expired(claim, heartbeats, now, lease_s):
                    seen = resolve.last_seen(claim, heartbeats)
                    until = seen + timedelta(seconds=lease_s)
                    live[claim.machine] = max(live.get(claim.machine, until), until)
    for m in machines(root):
        state = L.machine_control(root, m)
        if not state["stopped"]:
            out.append(Blocker(m, "running", paused=state["paused"]))
        elif m in live:
            out.append(Blocker(m, "stopping", until=live[m]))
    return out


def refusal(found: list[Blocker], what: str) -> str:
    lines = [f"cannot {what} while the swarm is running:", *[f"  - {b.advice()}" for b in found]]
    return "\n".join(lines)
