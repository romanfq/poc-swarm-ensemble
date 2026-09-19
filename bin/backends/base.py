"""The issue-backend port (whitepaper Ch.3.1).

Nothing outside bin/backends/ may talk to a tracker directly; everything else
depends only on this interface (Ports and Adapters).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

SWARM_STATUSES = ("ready", "claimed", "in-progress", "awaiting-review", "blocked", "done")
AUTONOMY_TIERS = ("auto-pr", "human-must-review", "human-must-scope")
DEFAULT_AUTONOMY = "human-must-review"

STATUS_PREFIX = "swarm:status:"
AUTONOMY_PREFIX = "swarm:autonomy:"
REPO_PREFIX = "repo:"
SWARM_PREFIX = "swarm:"
TYPE_LABELS = ("type:epic", "type:task")

# Which tracker issues belong to the plan (backend.yaml `plan_scope`).
PLAN_SCOPES = ("labelled", "all")
DEFAULT_PLAN_SCOPE = "labelled"


@dataclass(frozen=True)
class TaskRef:
    """Backend-native identifier, e.g. 'MW-14' or 'org/matchwire-swarm#7'."""
    key: str

    def __str__(self) -> str:
        return self.key


@dataclass
class Task:
    ref: TaskRef
    title: str
    body: str = ""
    status: str | None = None               # swarm status label (without prefix)
    autonomy: str = DEFAULT_AUTONOMY
    epic: TaskRef | None = None
    dependencies: list[TaskRef] = field(default_factory=list)
    repo: str | None = None                 # target code repo 'OWNER/NAME'
    url: str | None = None
    is_epic: bool = False
    closed: bool = False
    labels: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status == "done" or (self.closed and self.status != "blocked")

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def spec_markdown(self) -> str:
        deps = ", ".join(d.key for d in self.dependencies) or "none"
        lines = [
            f"# {self.ref.key}: {self.title}",
            "",
            f"- Issue: {self.url or self.ref.key}",
            f"- Epic: {self.epic.key if self.epic else 'none'}",
            f"- Target repo: {self.repo or 'unspecified'}",
            f"- Autonomy: {self.autonomy}",
            f"- Depends on: {deps}",
            "",
            "## Description",
            "",
            self.body.strip() or "_No description provided._",
            "",
        ]
        return "\n".join(lines)


@runtime_checkable
class IssueBackend(Protocol):
    name: str

    def ready_tasks(self) -> list[TaskRef]:
        """Tasks the backend does not hold back: open, not an epic, not
        blocked or done, and not under a blocked epic. The ledger decides the
        rest (dependencies, claims, arbitration)."""
        ...

    def get_task(self, ref: TaskRef) -> Task: ...
    def set_status(self, ref: TaskRef, status: str) -> None: ...
    def dependencies(self, ref: TaskRef) -> list[TaskRef]: ...
    def epic_children(self, ref: TaskRef) -> list[TaskRef]: ...
    def post_comment(self, ref: TaskRef, text: str) -> None: ...
    def coordination_ref(self, ref: TaskRef) -> str: ...

    # Helpers every shipped adapter provides (used by plan sync and the Board).
    def all_tasks(self) -> list[Task]:
        """Every task and epic in the plan (see ``plan_scope``): open ones,
        plus closed ones the adapter can list cheaply."""
        ...

    def all_issues(self) -> list[Task]:
        """Every issue the adapter can list, in the plan or not."""
        ...

    def set_autonomy(self, ref: TaskRef, tier: str) -> None: ...
    def web_url(self, ref: TaskRef) -> str | None: ...
    def short_key(self, ref: TaskRef) -> str: ...


def ready_from(tasks: list[Task]) -> list[TaskRef]:
    """Shared ready_tasks() rule over a snapshot: open, not an epic, not
    blocked or done, and not under a blocked epic (Ch.9.2 'halt everything')."""
    by_key = {t.ref.key: t for t in tasks}
    out = []
    for t in tasks:
        if t.is_epic or t.closed or t.status in ("blocked", "done"):
            continue
        epic = by_key.get(t.epic.key) if t.epic else None
        if epic is not None and epic.blocked:
            continue
        out.append(t.ref)
    return out


def plan_scope_of(cfg: dict) -> str:
    """The ``plan_scope`` setting from backend.yaml (top level)."""
    scope = str(cfg.get("plan_scope") or DEFAULT_PLAN_SCOPE).strip()
    if scope not in PLAN_SCOPES:
        raise ValueError(f"backend.yaml: plan_scope must be one of {', '.join(PLAN_SCOPES)} (got {scope!r})")
    return scope


def is_marked(t: Task) -> bool:
    """Carries a label that puts it in the plan: any ``swarm:`` label, or a type label."""
    return any(lb.startswith(SWARM_PREFIX) or lb in TYPE_LABELS for lb in t.labels)


def split_plan(tasks: list[Task], scope: str = DEFAULT_PLAN_SCOPE) -> tuple[list[Task], list[Task]]:
    """(members, skipped). Under ``labelled`` an issue is in the plan when it
    is marked, or is an ancestor (via ``epic``) of an issue that is, so an
    epic seeded without a status still counts. Under ``all`` everything is."""
    if scope == "all":
        return list(tasks), []
    by_key = {t.ref.key: t for t in tasks}
    keep: set[str] = set()
    for t in tasks:
        if not is_marked(t):
            continue
        cur: Task | None = t
        while cur is not None and cur.ref.key not in keep:
            keep.add(cur.ref.key)
            cur = by_key.get(cur.epic.key) if cur.epic else None
    return [t for t in tasks if t.ref.key in keep], [t for t in tasks if t.ref.key not in keep]


class PlanScoped:
    """The plan-membership rule, shared by every adapter. An adapter provides
    ``all_issues()`` and sets ``plan_scope``; this supplies the scoped views."""
    plan_scope: str = DEFAULT_PLAN_SCOPE

    def all_issues(self) -> list[Task]:
        raise NotImplementedError

    def plan_split(self) -> tuple[list[Task], list[Task]]:
        return split_plan(self.all_issues(), self.plan_scope)

    def all_tasks(self) -> list[Task]:
        return self.plan_split()[0]

    def ready_tasks(self) -> list[TaskRef]:
        return ready_from(self.all_tasks())

    def in_plan(self, ref: TaskRef) -> bool:
        return any(t.ref.key == ref.key for t in self.all_tasks())


def parse_labels(labels: list[str]) -> dict:
    """Pull swarm fields out of plain labels (Ch.3.2/3.3)."""
    status = None
    autonomy = None
    repo = None
    kind = None
    for name in labels:
        if name.startswith(STATUS_PREFIX):
            status = name[len(STATUS_PREFIX):]
        elif name.startswith(AUTONOMY_PREFIX):
            autonomy = name[len(AUTONOMY_PREFIX):]
        elif name.startswith(REPO_PREFIX):
            repo = name[len(REPO_PREFIX):]
        elif name in ("type:epic", "type:task"):
            kind = name.split(":", 1)[1]
    return {"status": status, "autonomy": autonomy if autonomy in AUTONOMY_TIERS else None,
            "repo": repo, "kind": kind}


def downgrade(tier: str) -> str:
    order = list(AUTONOMY_TIERS)
    i = order.index(tier) if tier in order else 1
    return order[min(i + 1, len(order) - 1)]
