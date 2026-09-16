"""A file-backed issue backend for tests and offline demos.

backend.yaml:
    backend: fake
    fake:
      path: fake-backend.yaml     # relative to the coordination repo root

fake-backend.yaml:
    issues:
      E1: {title: Ingestion, epic: true}
      T1: {title: Poll feed, epic_of: E1, repo: OWNER/app, labels: [swarm:autonomy:auto-pr]}
      T2: {title: Parse feed, epic_of: E1, blocked_by: [T1]}
"""
from __future__ import annotations

import threading
from pathlib import Path

from backends.base import (AUTONOMY_PREFIX, DEFAULT_AUTONOMY, STATUS_PREFIX, Task, TaskRef,
                           parse_labels, ready_from)
from dags import records as R

_lock = threading.Lock()


class FakeBackend:
    name = "fake"

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def from_config(cls, cfg: dict, ctx) -> "FakeBackend":
        return cls(ctx.root / cfg.get("path", "fake-backend.yaml"))

    # -- storage --------------------------------------------------------------
    def _load(self) -> dict:
        return R.load_yaml(self.path).get("issues") or {}

    def _save(self, issues: dict) -> None:
        R.write_replace(self.path, {"issues": issues})

    def _issue(self, ref: TaskRef) -> dict:
        issues = self._load()
        if ref.key not in issues:
            raise KeyError(f"no such issue {ref.key}")
        return issues[ref.key]

    # -- port -------------------------------------------------------------------
    def get_task(self, ref: TaskRef) -> Task:
        i = self._issue(ref)
        labels = list(i.get("labels") or [])
        if i.get("status"):
            labels.append(STATUS_PREFIX + i["status"])
        parsed = parse_labels(labels)
        return Task(
            ref=ref,
            title=i.get("title", ref.key),
            body=i.get("body", ""),
            status=parsed["status"],
            autonomy=parsed["autonomy"] or DEFAULT_AUTONOMY,
            epic=TaskRef(i["epic_of"]) if i.get("epic_of") else None,
            dependencies=[TaskRef(k) for k in i.get("blocked_by") or []],
            repo=i.get("repo") or parsed["repo"],
            url=f"fake://{ref.key}",
            is_epic=bool(i.get("epic")),
            closed=bool(i.get("closed")),
            labels=labels,
        )

    def all_tasks(self) -> list[Task]:
        return [self.get_task(TaskRef(k)) for k in self._load()]

    def ready_tasks(self) -> list[TaskRef]:
        return ready_from(self.all_tasks())

    def set_status(self, ref: TaskRef, status: str) -> None:
        with _lock:
            issues = self._load()
            issues[ref.key]["status"] = status
            if status == "done":
                issues[ref.key]["closed"] = True
            self._save(issues)

    def set_autonomy(self, ref: TaskRef, tier: str) -> None:
        with _lock:
            issues = self._load()
            labels = [x for x in issues[ref.key].get("labels") or [] if not x.startswith(AUTONOMY_PREFIX)]
            issues[ref.key]["labels"] = labels + [AUTONOMY_PREFIX + tier]
            self._save(issues)

    def dependencies(self, ref: TaskRef) -> list[TaskRef]:
        return self.get_task(ref).dependencies

    def epic_children(self, ref: TaskRef) -> list[TaskRef]:
        return [TaskRef(k) for k, v in self._load().items() if v.get("epic_of") == ref.key]

    def post_comment(self, ref: TaskRef, text: str) -> None:
        with _lock:
            issues = self._load()
            issues[ref.key].setdefault("comments", []).append(text)
            self._save(issues)

    def coordination_ref(self, ref: TaskRef) -> str:
        return ref.key

    def web_url(self, ref: TaskRef) -> str | None:
        return None

    def short_key(self, ref: TaskRef) -> str:
        return ref.key

    # -- test/demo helpers (not part of the port) ------------------------------------
    def comments(self, ref: TaskRef) -> list[str]:
        return list(self._issue(ref).get("comments") or [])

    def add(self, key: str, **fields) -> None:
        with _lock:
            issues = self._load()
            issues[key] = fields
            self._save(issues)
