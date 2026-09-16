"""Seed a plan file into the issue backend (Phase 9 POC, spec Appendix B).

A plan file lists epics and tasks, with their parents and dependencies (see
poc/matchwire/plan.yaml). Seeding is idempotent:
- each created issue carries a hidden marker ``<!-- dags-seed: ID -->``;
- a re-run creates only what is missing and adds only missing links;
- existing issues are never edited or deleted beyond those links. A title
  that differs, or a link the plan doesn't have, is reported instead.

``diff()`` works out what would change without writing anything, and
``apply()`` performs the changes. The backend must provide the seeding
helpers (only the GitHub adapter does today).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from backends.base import AUTONOMY_TIERS, DEFAULT_AUTONOMY, SWARM_STATUSES, Task, TaskRef

MARKER = "<!-- dags-seed: {id} -->"
MARKER_RE = re.compile(r"<!--\s*dags-seed:\s*([A-Za-z0-9_.-]+)\s*-->")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SEED_METHODS = ("existing_labels", "required_labels", "create_label", "seed_labels",
                "create_issue", "set_parent", "add_dependency")


class SeedError(Exception):
    pass


@dataclass
class Item:
    id: str
    title: str
    body: str = ""
    epic: str | None = None           # parent epic id (tasks only)
    repo: str | None = None
    autonomy: str | None = None
    depends_on: list[str] = field(default_factory=list)
    is_epic: bool = False

    def issue_body(self) -> str:
        return f"{self.body.rstrip()}\n\n{MARKER.format(id=self.id)}\n".lstrip("\n")


@dataclass
class Plan:
    repo: str | None
    status: str | None
    items: list[Item]

    def by_id(self) -> dict[str, Item]:
        return {i.id: i for i in self.items}


def load(path: Path) -> Plan:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SeedError(f"can't read {path}: {e}") from e
    if not isinstance(data, dict):
        raise SeedError(f"{path}: expected a mapping at the top level")
    items: list[Item] = []
    for raw in data.get("epics") or []:
        items.append(Item(id=str(raw.get("id", "")), title=str(raw.get("title", "")),
                          body=str(raw.get("body") or ""), is_epic=True))
    for raw in data.get("tasks") or []:
        items.append(Item(id=str(raw.get("id", "")), title=str(raw.get("title", "")),
                          body=str(raw.get("body") or ""),
                          epic=str(raw["epic"]) if raw.get("epic") else None,
                          repo=raw.get("repo"), autonomy=raw.get("autonomy"),
                          depends_on=[str(d) for d in raw.get("depends_on") or []]))
    return Plan(repo=data.get("repo"), status=data.get("status", "ready"), items=items)


def topo_order(plan: Plan) -> list[Item]:
    """Epics first (file order), then tasks with every dependency before its dependants."""
    ids = plan.by_id()
    out = [i for i in plan.items if i.is_epic]
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(item: Item, trail: list[str]):
        if item.id in done:
            return
        if item.id in visiting:
            raise SeedError("dependency cycle: " + " -> ".join(trail + [item.id]))
        visiting.add(item.id)
        for d in item.depends_on:
            if d in ids and not ids[d].is_epic:
                visit(ids[d], trail + [item.id])
        visiting.discard(item.id)
        done.add(item.id)
        out.append(item)

    for item in plan.items:
        if not item.is_epic:
            visit(item, [])
    return out


def validate(plan: Plan, backend_repo: str | None, code_repos: list[str]) -> list[str]:
    errors = []
    if plan.repo and backend_repo and plan.repo != backend_repo:
        errors.append(f"plan repo {plan.repo} differs from backend.yaml github.repo {backend_repo}")
    if plan.status is not None and plan.status not in SWARM_STATUSES:
        errors.append(f"status {plan.status!r} is not one of {', '.join(SWARM_STATUSES)}")
    seen: set[str] = set()
    ids = plan.by_id()
    for i in plan.items:
        where = f"{'epic' if i.is_epic else 'task'} {i.id or '?'}"
        if not i.id or not ID_RE.match(i.id):
            errors.append(f"{where}: id must be letters, digits, '.', '_' or '-'")
        if i.id in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(i.id)
        if not i.title.strip():
            errors.append(f"{where}: missing title")
        if i.is_epic:
            continue
        if i.epic and (i.epic not in ids or not ids[i.epic].is_epic):
            errors.append(f"{where}: epic {i.epic} is not an epic in this plan")
        if not i.repo:
            errors.append(f"{where}: missing repo")
        elif code_repos and i.repo not in code_repos:
            errors.append(f"{where}: repo {i.repo} is not listed under repos: in backend.yaml")
        if i.autonomy and i.autonomy not in AUTONOMY_TIERS:
            errors.append(f"{where}: autonomy {i.autonomy!r} is not one of {', '.join(AUTONOMY_TIERS)}")
        for d in i.depends_on:
            if d not in ids:
                errors.append(f"{where}: depends on unknown id {d}")
            elif ids[d].is_epic:
                errors.append(f"{where}: depends on epic {d}; depend on tasks instead")
            elif d == i.id:
                errors.append(f"{where}: depends on itself")
    if not errors:
        try:
            topo_order(plan)
        except SeedError as e:
            errors.append(str(e))
    return errors


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

@dataclass
class Step:
    kind: str                 # label | create | parent | depends
    item: str | None = None   # plan id
    target: str | None = None # plan id of the parent / blocker
    label: tuple[str, str] | None = None
    labels: list[str] = field(default_factory=list)

    def describe(self, plan: Plan, refs: dict[str, TaskRef], short) -> str:
        def name(pid):
            return f"{pid} ({short(refs[pid])})" if pid in refs else f"{pid} (new)"
        if self.kind == "label":
            return f"label   {self.label[0]}"
        if self.kind == "create":
            item = plan.by_id()[self.item]
            kind = "epic" if item.is_epic else "task"
            return f"create  {kind} {self.item}: {item.title}  [{', '.join(self.labels)}]"
        if self.kind == "parent":
            return f"parent  {name(self.item)} -> epic {name(self.target)}"
        return f"depends {name(self.item)} blocked by {name(self.target)}"


@dataclass
class Diff:
    plan: Plan
    existing: dict[str, Task]
    steps: list[Step]
    notes: list[str]

    @property
    def refs(self) -> dict[str, TaskRef]:
        return {pid: t.ref for pid, t in self.existing.items()}

    def count(self, kind: str) -> int:
        return sum(1 for s in self.steps if s.kind == kind)

    @property
    def empty(self) -> bool:
        return not self.steps

    def summary(self) -> str:
        return (f"{self.count('label')} labels, {self.count('create')} issues to create, "
                f"{self.count('parent')} parents and {self.count('depends')} dependencies to link "
                f"({len(self.existing)} issues already seeded)")

    def lines(self, short) -> list[str]:
        refs = self.refs
        return [s.describe(self.plan, refs, short) for s in self.steps]


def check_backend(backend) -> None:
    missing = [m for m in SEED_METHODS if not hasattr(backend, m)]
    if missing:
        raise SeedError(f"the {backend.name} backend can't seed plans (missing {', '.join(missing)})")


def seeded(backend) -> dict[str, Task]:
    if hasattr(backend, "invalidate"):
        backend.invalidate()                   # always decide from a fresh read
    out: dict[str, Task] = {}
    for t in backend.all_tasks():
        m = MARKER_RE.search(t.body or "")
        if not m:
            continue
        pid = m.group(1)
        if pid in out:
            raise SeedError(f"two issues carry seed id {pid}: {out[pid].ref} and {t.ref}; "
                            f"remove the marker from one of them")
        out[pid] = t
    return out


def diff(plan: Plan, backend, code_repos: list[str]) -> Diff:
    check_backend(backend)
    errors = validate(plan, getattr(backend, "repo", None), code_repos)
    if errors:
        raise SeedError("plan file problems:\n  " + "\n  ".join(errors))
    existing = seeded(backend)
    order = topo_order(plan)
    steps: list[Step] = []
    notes: list[str] = []

    have = backend.existing_labels()
    for name, color in backend.required_labels(code_repos):
        if name not in have:
            steps.append(Step("label", label=(name, color)))

    for item in order:
        if item.id in existing:
            t = existing[item.id]
            if t.title != item.title:
                notes.append(f"{item.id} ({t.ref}): title on the tracker differs from the plan; left alone")
            if t.is_epic != item.is_epic:
                notes.append(f"{item.id} ({t.ref}): the tracker has it as "
                             f"{'an epic' if t.is_epic else 'a task'}; left alone")
            continue
        labels = backend.seed_labels(epic=item.is_epic,
                                     status=None if item.is_epic else plan.status,
                                     autonomy=None if item.is_epic else (item.autonomy or DEFAULT_AUTONOMY),
                                     repo=item.repo)
        steps.append(Step("create", item=item.id, labels=labels))

    for item in order:
        t = existing.get(item.id)
        if item.epic:
            want = existing.get(item.epic)
            have_parent = t.epic if t else None
            if t is None or want is None or have_parent != want.ref:
                if t is not None and have_parent is not None and (want is None or have_parent != want.ref):
                    notes.append(f"{item.id} ({t.ref}): has parent {have_parent}, plan says {item.epic}; "
                                 f"re-parenting it")
                steps.append(Step("parent", item=item.id, target=item.epic))
        have_deps = set(t.dependencies) if t else set()
        for d in item.depends_on:
            dt = existing.get(d)
            if t is None or dt is None or dt.ref not in have_deps:
                steps.append(Step("depends", item=item.id, target=d))
        if t is not None:
            wanted = {existing[d].ref for d in item.depends_on if d in existing}
            for extra in sorted(have_deps - wanted, key=lambda r: r.key):
                notes.append(f"{item.id} ({t.ref}): also blocked by {extra}, which the plan doesn't list; "
                             f"left alone")
    return Diff(plan, existing, steps, notes)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply(d: Diff, backend, echo=print) -> dict[str, TaskRef]:
    """Carry out the diff's steps in order. Returns plan id -> issue ref.

    Stops at the first failure; everything done so far is marked on the
    tracker, so re-running `seed` picks up where this left off."""
    check_backend(backend)
    refs = d.refs
    items = d.plan.by_id()
    for s in d.steps:
        line = s.describe(d.plan, refs, backend.short_key)
        if s.kind == "label":
            backend.create_label(*s.label)
        elif s.kind == "create":
            item = items[s.item]
            refs[s.item] = backend.create_issue(item.title, item.issue_body(), s.labels, epic=item.is_epic)
            line = f"created {backend.short_key(refs[s.item])} {s.item}: {item.title}"
        elif s.kind == "parent":
            backend.set_parent(refs[s.item], refs[s.target])
        elif s.kind == "depends":
            backend.add_dependency(refs[s.item], refs[s.target])
        echo(line)
    return refs


def verify(plan: Plan, backend, code_repos: list[str]) -> list[str]:
    """What still differs after seeding (empty when the tracker matches the plan)."""
    after = diff(plan, backend, code_repos)
    return after.lines(backend.short_key)
