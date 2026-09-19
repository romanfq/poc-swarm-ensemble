"""Ledger records on disk (whitepaper Ch.4).

Rule: append-only. Claims, withdrawals, arbitration, completions, plan
reviews, events, control, priority and quota records are always new, uniquely named files
(``write_new``). The only files rewritten in place are the single-writer ones
(a machine's own heartbeat for a task, and the checkpoint owned by the current
claim winner), written atomically with ``write_replace``.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Iterator

import yaml

# Top-level directories whose records carry a logical clock.
RECORD_DIRS = ("tasks", "control", "priority", "quota")

TASK_SUBDIRS = ("claims", "withdrawals", "arbitration", "heartbeats", "completions", "meta", "plan-reviews",
                "events")


def slug(text: str) -> str:
    """Filesystem-safe token: 'org/repo#7' -> 'org-repo-7'."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-.")
    return s or "x"


def load_yaml(path) -> dict:
    """Read a YAML mapping. Missing, empty or corrupt files read as {} so a
    half-synced file can never crash resolution on any machine."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return {}
    except (yaml.YAMLError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def write_new(path, data: dict) -> Path:
    """Create a record. Raises FileExistsError rather than overwrite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(dump_yaml(data))
    return path


def write_replace(path, data: dict) -> Path:
    """Atomically replace a single-writer file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_yaml(data))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def iter_yaml(directory) -> Iterator[Path]:
    d = Path(directory)
    if not d.is_dir():
        return iter(())
    return iter(sorted(p for p in d.iterdir() if p.suffix == ".yaml" and not p.name.startswith(".")))


def read_dir(directory) -> list[tuple[Path, dict]]:
    return [(p, load_yaml(p)) for p in iter_yaml(directory)]


def clock_of(data: dict) -> int:
    try:
        return int(data.get("logical_clock", 0) or 0)
    except (TypeError, ValueError):
        return 0


# --- names -----------------------------------------------------------------

def claim_id(machine: str, clock: int) -> str:
    return f"{machine}-{clock}"


def claim_name(machine: str, clock: int) -> str:
    return f"{claim_id(machine, clock)}.yaml"


def withdrawal_name(machine: str, clock: int) -> str:
    return f"{machine}-{clock}.yaml"


def arbitration_name(human: str, clock: int) -> str:
    return f"human-{slug(human)}-{clock}.yaml"


def completion_name(machine: str, kind: str, clock: int) -> str:
    return f"{machine}-{kind}-{clock}.yaml"


def control_name(machine: str, action: str, clock: int) -> str:
    return f"{machine}-{action}-{clock}.yaml"


def priority_name(machine: str, epic: str, action: str, clock: int) -> str:
    return f"{machine}-{slug(epic)}-{action}-{clock}.yaml"


def quota_name(human: str, clock: int) -> str:
    return f"{slug(human)}-{clock}.yaml"


def event_name(machine: str, kind: str, clock: int) -> str:
    return f"{machine}-{kind}-{clock}.yaml"


def heartbeat_name(machine: str) -> str:
    return f"{machine}.yaml"


def meta_revision_name(machine: str, clock: int) -> str:
    return f"{machine}-{clock}.yaml"
