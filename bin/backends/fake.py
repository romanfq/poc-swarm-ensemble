"""A file-backed issue backend for tests and offline demos.

backend.yaml:
    backend: fake
    fake:
      path: fake-backend.yaml     # relative to the coordination repo root

fake-backend.yaml:
    issues:
      E1: {title: Ingestion, epic: true}
      T1: {title: Poll feed, epic_of: E1, repo: OWNER/app, labels: [swarm:autonomy:self-approve]}
      T2: {title: Parse feed, epic_of: E1, blocked_by: [T1], labels: [type:task]}

Only issues with a ``swarm:`` or ``type:`` label (or a ``status``), and their
epics, are in the plan unless backend.yaml says ``plan_scope: all``.
"""
from __future__ import annotations

import threading
from pathlib import Path

import autonomy
from backends.base import (AUTONOMY_PREFIX, DEFAULT_AUTONOMY, DEFAULT_PLAN_SCOPE, STATUS_PREFIX,
                           Comment, ExportItem, PlanScoped, Task, TaskRef, parse_labels, plan_scope_of)
from dags import records as R

_lock = threading.Lock()


def _as_record(c) -> dict:
    """Comments written before the fake kept authors are bare strings."""
    return c if isinstance(c, dict) else {"author": "dags-bot", "body": str(c), "created_at": "", "url": None}


class FakeBackend(PlanScoped):
    name = "fake"

    def __init__(self, path: Path, plan_scope: str = DEFAULT_PLAN_SCOPE):
        self.path = Path(path)
        self.plan_scope = plan_scope

    @classmethod
    def from_config(cls, cfg: dict, ctx) -> "FakeBackend":
        return cls(ctx.root / cfg.get("path", "fake-backend.yaml"), plan_scope_of(ctx.backend_cfg))

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

    def all_issues(self) -> list[Task]:
        return [self.get_task(TaskRef(k)) for k in self._load()]

    def export_records(self, refs: list[TaskRef] | None = None) -> list[ExportItem]:
        issues = self._load()
        keys = [r.key for r in refs] if refs is not None else list(issues)
        out = []
        for key in keys:
            i = issues[key]
            task = self.get_task(TaskRef(key))
            out.append(ExportItem(task, i.get("created_utc") or "", i.get("closed_utc") if task.closed else None,
                                  i.get("author"),
                                  tuple(Comment(c.get("author"), c.get("body", ""), c.get("created_at", ""),
                                                c.get("url")) for c in map(_as_record, i.get("comments") or [])),
                                  tuple(i.get("lossy") or ())))
        return out

    def set_status(self, ref: TaskRef, status: str) -> None:
        with _lock:
            issues = self._load()
            issues[ref.key]["status"] = status
            if status == "done":
                issues[ref.key]["closed"] = True
            self._save(issues)

    def set_autonomy(self, ref: TaskRef, tier: str) -> None:
        tier = autonomy.canonical(tier)
        with _lock:
            issues = self._load()
            labels = [x for x in issues[ref.key].get("labels") or [] if not x.startswith(AUTONOMY_PREFIX)]
            issues[ref.key]["labels"] = labels + [AUTONOMY_PREFIX + tier]
            self._save(issues)

    uses_type_labels = True
    max_levels = 8                      # parent/child levels offered; tests lower it to model a shallow tracker
    import_limits: dict = {}
    lag_window_s = 0.0

    def set_kind(self, ref: TaskRef, epic: bool) -> None:
        label = "type:epic" if epic else "type:task"
        with _lock:
            issues = self._load()
            labels = list(issues[ref.key].get("labels") or [])
            if label not in labels:
                issues[ref.key]["labels"] = labels + [label]
                self._save(issues)

    def dependencies(self, ref: TaskRef) -> list[TaskRef]:
        return self.get_task(ref).dependencies

    # -- what plan seeding and `backend import` need -----------------------------------------------------------
    def get_task_fresh(self, ref: TaskRef) -> Task:
        return self.get_task(ref)

    def parse_ref(self, text: str) -> TaskRef:
        return TaskRef(str(text).strip())

    def invalidate(self) -> None:
        pass

    def hierarchy_levels(self) -> int:
        return self.max_levels

    def existing_labels(self) -> set[str]:
        from backends.base import AUTONOMY_TIERS, SWARM_STATUSES
        known = {STATUS_PREFIX + s for s in SWARM_STATUSES} | {AUTONOMY_PREFIX + t for t in AUTONOMY_TIERS}
        return known | {"type:epic", "type:task"} | {lb for i in self._load().values() for lb in i.get("labels") or []}

    def required_labels(self, code_repos: list[str]) -> list[tuple[str, str]]:
        return []

    def init_commands(self, code_repos: list[str]) -> list[list[str]]:
        return []

    def create_label(self, name: str, color: str) -> None:
        pass

    def seed_labels(self, *, epic: bool, status: str | None, autonomy: str | None,
                    repo: str | None) -> list[str]:
        labels = []
        if status:
            labels.append(STATUS_PREFIX + status)
        if autonomy:
            labels.append(AUTONOMY_PREFIX + autonomy)
        if repo:
            labels.append("repo:" + repo)
        labels.append("type:epic" if epic else "type:task")
        return labels

    @staticmethod
    def _put_labels(issue: dict, labels: list[str]) -> None:
        for lb in labels:
            if lb.startswith(STATUS_PREFIX):
                issue["status"] = lb[len(STATUS_PREFIX):]
            elif lb not in (issue.get("labels") or []):
                issue.setdefault("labels", []).append(lb)

    def create_issue(self, title: str, body: str, labels: list[str], *, epic: bool = False) -> TaskRef:
        with _lock:
            issues = self._load()
            self._seq = max(getattr(self, "_seq", 0), len(issues))
            while f"FK-{self._seq + 1}" in issues:
                self._seq += 1
            self._seq += 1
            key = f"FK-{self._seq}"
            issue = {"title": title, "body": body, "epic": epic, "labels": [], "comments": [],
                     "created_utc": f"2000-01-01T00:{self._seq // 60:02d}:{self._seq % 60:02d}Z",
                     "author": "dags-bot"}
            self._put_labels(issue, labels)
            issues[key] = issue
            self._save(issues)
            return TaskRef(key)

    def set_parent(self, ref: TaskRef, parent: TaskRef) -> None:
        with _lock:
            issues = self._load()
            issues[ref.key]["epic_of"] = parent.key
            self._save(issues)

    def add_dependency(self, ref: TaskRef, blocker: TaskRef) -> None:
        with _lock:
            issues = self._load()
            blocked = issues[ref.key].setdefault("blocked_by", [])
            if blocker.key not in blocked:
                blocked.append(blocker.key)
                self._save(issues)

    def update_body(self, ref: TaskRef, text: str) -> None:
        with _lock:
            issues = self._load()
            issues[ref.key]["body"] = text
            self._save(issues)

    def add_labels(self, ref: TaskRef, labels: list[str]) -> None:
        with _lock:
            issues = self._load()
            self._put_labels(issues[ref.key], labels)
            self._save(issues)

    def close_issue(self, ref: TaskRef) -> None:
        with _lock:
            issues = self._load()
            if not issues[ref.key].get("closed"):
                issues[ref.key]["closed"] = True
                issues[ref.key]["closed_utc"] = "2000-01-02T00:00:00Z"
                self._save(issues)

    def epic_children(self, ref: TaskRef) -> list[TaskRef]:
        return [TaskRef(k) for k, v in self._load().items() if v.get("epic_of") == ref.key]

    def post_comment(self, ref: TaskRef, text: str, author: str = "dags-bot") -> str | None:
        with _lock:
            issues = self._load()
            comments = issues[ref.key].setdefault("comments", [])
            url = f"fake://{ref.key}/comments/{len(comments) + 1}"
            comments.append({"author": author, "body": text, "created_at": f"c{len(comments) + 1:06d}",
                             "url": url})
            self._save(issues)
            return url

    def list_comments(self, ref: TaskRef) -> list[Comment]:
        return [Comment(c.get("author"), c.get("body", ""), c.get("created_at", ""), c.get("url"))
                for c in map(_as_record, self._issue(ref).get("comments") or [])]

    def coordination_ref(self, ref: TaskRef) -> str:
        return ref.key

    def web_url(self, ref: TaskRef) -> str | None:
        return None

    def short_key(self, ref: TaskRef) -> str:
        return ref.key

    # -- test/demo helpers (not part of the port) ------------------------------------
    def comments(self, ref: TaskRef) -> list[str]:
        return [c["body"] for c in map(_as_record, self._issue(ref).get("comments") or [])]

    def reply(self, ref: TaskRef, author: str, text: str) -> str | None:
        """A person's comment on the issue (the bot's are posted through the port)."""
        return self.post_comment(ref, text, author=author)

    def add(self, key: str, **fields) -> None:
        with _lock:
            issues = self._load()
            issues[key] = fields
            self._save(issues)
