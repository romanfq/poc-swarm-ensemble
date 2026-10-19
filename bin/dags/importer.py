"""``backend import``: write a bundle into the tracker backend.yaml names now (GH-124).

Two halves. ``plan_import`` reads and judges (no writes; it is the dry run). ``run`` writes, in three global
passes so a half-built hierarchy is never visible to the swarm:

    1  create every issue (title, body, footer, anchor and foreign labels, no swarm labels)
    2  parents, then dependency links
    3  comments, then reference rewriting, then swarm labels, then close

Idempotent: the target is the truth and the map file is a cache. Each issue has a step (``STEPS``); a resume
continues from the recorded step and every step reads the current state by key and acts only on the
difference, so a second run issues no writes and a killed run finishes to the same result.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from backends.base import AUTONOMY_PREFIX, REPO_PREFIX, STATUS_PREFIX, BackendError, TaskRef
from dags import bundle as B
from dags import seed

MAP_FORMAT = "dags-import-map/1"
STEPS = ("creating", "created", "parented", "linked", "commented", "bodied", "labelled", "closed")
EXIT_RATE_LIMITED = 75
DEFAULT_COLOR = "ededed"
CALLS_PER_ISSUE = (10, 20)               # Jira: creates, labels, transitions, links, comments and reads


class ImportRefused(RuntimeError):
    """The import cannot start (or go on) and nothing more was written; the message says what to do."""


class ImportStop(RuntimeError):
    """The run stopped part-way with the map flushed. ``code`` is the exit status (75: rate limited)."""

    def __init__(self, message: str, code: int = 1, resume_at: str | None = None):
        super().__init__(message)
        self.code, self.resume_at = code, resume_at


@dataclass
class Options:
    hold: bool = False
    flatten: bool = False
    force_new: bool = False
    rebuild_map: bool = False
    map_path: Path | None = None


@dataclass
class Plan:
    bundle: B.Bundle
    run_id: str
    records: dict[str, dict]                                   # by source id, in bundle order
    dropped: dict[str, str | None] = field(default_factory=dict)   # flattened container -> its own parent
    parent: dict[str, str | None] = field(default_factory=dict)    # effective parent of each created issue
    depends: dict[str, list[str]] = field(default_factory=dict)
    part_of: dict[str, list[str]] = field(default_factory=dict)    # titles of dropped ancestors, outermost first
    labels: dict[str, list[str]] = field(default_factory=dict)     # foreign labels as the target will hold them
    renamed_labels: list[tuple[str, str, str]] = field(default_factory=list)    # (issue, from, to)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_replies: list[str] = field(default_factory=list)
    ready_after: int = 0
    calls: int = 0
    kept_open: list[str] = field(default_factory=list)         # closed and blocked: imported open and blocked

    @property
    def creates(self) -> list[dict]:
        return [r for i, r in self.records.items() if i not in self.dropped]

    @property
    def ok(self) -> bool:
        return not self.problems


# -- the map ---------------------------------------------------------------------------------------------------

class MapFile:
    """``{source id: {target, short, target_short, hash, step, anchor}}``, rewritten whole after every change."""

    def __init__(self, path: Path, target: dict):
        self.path = Path(path)
        self.data = {"format": MAP_FORMAT, "target": target, "bundles": [], "issues": {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except ValueError as e:
                raise ImportRefused(f"{self.path} is not valid JSON ({e}); fix or remove it, then "
                                    f"`--rebuild-map` can rebuild it from the tracker") from e
            if data.get("format") != MAP_FORMAT:
                raise ImportRefused(f"{self.path} is not an import map")
            if data.get("target") != target:
                raise ImportRefused(f"{self.path} belongs to {data.get('target')}, not {target}; "
                                    f"give --map another file")
            self.data = data

    @property
    def issues(self) -> dict[str, dict]:
        return self.data["issues"]

    def set(self, source_id: str, **fields) -> dict:
        entry = self.issues.setdefault(source_id, {})
        entry.update(fields)
        self.save()
        return entry

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


def step_at_least(entry: dict | None, step: str) -> bool:
    return bool(entry) and entry.get("step") in STEPS and STEPS.index(entry["step"]) >= STEPS.index(step)


class Lock:
    """Two importers must not run at once on one map. The anchor check is the second line of defence."""

    def __init__(self, path: Path):
        self.path = Path(str(path) + ".lock")

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ImportRefused(f"{self.path} exists: another import is running on this map. If none is, "
                                f"delete that file") from None
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# -- reading and judging (the dry run) ---------------------------------------------------------------------------

def target_identity(backend) -> dict:
    return {"backend": backend.name, "project": getattr(backend, "repo", None) or getattr(backend, "project_key", None)}


def run_id_for(bundle_id: str, anchored: int, force_new: bool) -> str:
    if not force_new or not anchored:
        return bundle_id
    return hashlib.sha256(f"{bundle_id}:again:{anchored}".encode()).hexdigest()[:8]


def anchored_issues(backend) -> dict[str, "object"]:
    """Issues on the target that carry any ``dags-import-*`` label (a fresh read), by key."""
    if hasattr(backend, "invalidate"):
        backend.invalidate()
    return {t.ref.key: t for t in backend.all_issues() if any(lb.startswith(B.ANCHOR_PREFIX) for lb in t.labels)}


def _depth(parent: dict[str, str | None], key: str) -> int:
    n = 1
    while parent.get(key):
        key = parent[key]
        n += 1
    return n


def plan_import(bundle: B.Bundle, backend, opts: Options, *, is_human: Callable[[str | None], bool] = lambda a: False,
                anchored: int = 0) -> Plan:
    records = {r["id"]: r for r in bundle.records}
    plan = Plan(bundle, run_id_for(bundle.bundle_id, anchored, opts.force_new), records)
    problems, notes = plan.problems, plan.notes

    # the bundle against the target's plan
    want = bundle.manifest.get("plan_scope")
    if want != getattr(backend, "plan_scope", want):
        problems.append(f"the bundle was exported with plan_scope '{want}' but backend.yaml says "
                        f"'{backend.plan_scope}': a different scope changes which issues the swarm sees")
    seen: dict[str, str] = {}
    for r in bundle.records:
        m = seed.MARKER_RE.search(r["body"] or "")
        if m:
            if m.group(1) in seen:
                problems.append(f"{r['short']} and {seen[m.group(1)]} carry the same dags-seed id {m.group(1)}")
            seen[m.group(1)] = r["short"]
    if hasattr(backend, "whoami"):
        try:
            backend.whoami()
        except BackendError as e:
            problems.append(f"cannot reach the target: {e}")

    # hierarchy: does it fit?
    parent = {i: r["parent"] for i, r in records.items()}
    levels = backend.hierarchy_levels() if hasattr(backend, "hierarchy_levels") else 99
    children: dict[str, list[str]] = {}
    for i, p in parent.items():
        if p:
            children.setdefault(p, []).append(i)
    deepest = max((_depth(parent, i) for i in records), default=0)
    too_deep = sorted(i for i in records if _depth(parent, i) > levels)
    if deepest > levels:
        if not opts.flatten:
            problems.append(f"the bundle is {deepest} levels deep and {backend.name} offers {levels}. These do not "
                            f"fit: {', '.join(records[i]['short'] for i in too_deep)}. "
                            f"--flatten drops the containers in between (each descendant then says `Part of:`)")
        else:
            while max((_depth(parent, i) for i in records if i not in plan.dropped), default=0) > levels:
                level = 2 if levels >= 2 else 1
                drop = [i for i in records if i not in plan.dropped and children.get(i)
                        and _depth(parent, i) == level]
                if not drop:
                    problems.append("cannot flatten the hierarchy any further")
                    break
                for i in drop:
                    plan.dropped[i] = parent[i]
                    notes.append(f"flatten: {records[i]['short']} '{records[i]['title']}' is not created; its "
                                 f"descendants say `Part of:` it" + (" (it was blocked: its descendants are no "
                                 "longer halted by it)" if records[i]["status"] == "blocked" else ""))
                for i in list(records):
                    p = parent[i]
                    while p in plan.dropped:
                        p = plan.dropped[p]
                    parent[i] = p
                children = {}
                for i, p in parent.items():
                    if p and i not in plan.dropped:
                        children.setdefault(p, []).append(i)
    for i in records:
        if i in plan.dropped:
            continue
        titles, p = [], records[i]["parent"]
        while p in plan.dropped:
            titles.insert(0, records[p]["title"])
            p = records[p]["parent"]
        if titles:
            plan.part_of[i] = titles
        plan.parent[i] = parent[i]
        plan.depends[i] = [d for d in records[i]["depends_on"] if isinstance(d, str) and d not in plan.dropped]
        lost = [d for d in records[i]["depends_on"] if isinstance(d, str) and d in plan.dropped]
        if lost:
            notes.append(f"{records[i]['short']}: dependency on the dropped container(s) "
                         f"{', '.join(records[d]['short'] for d in lost)} is not kept")
        for d in records[i]["depends_on"]:
            if isinstance(d, dict):
                notes.append(f"{records[i]['short']}: depends on {d['id']}, which is in another repository and "
                             f"not in the bundle; the link is dropped (lossy)")
    limit = (getattr(backend, "import_limits", None) or {})
    if limit.get("children"):
        for p, kids in children.items():
            if len(kids) > limit["children"]:
                problems.append(f"{records[p]['short']} has {len(kids)} sub-issues; {backend.name} allows "
                                f"{limit['children']} per parent")
    if limit.get("body"):
        for r in plan.creates:
            if len(r["body"] or "") > limit["body"]:
                problems.append(f"the body of {r['short']} is {len(r['body'])} characters; {backend.name} accepts "
                                f"about {limit['body']}")
    if limit.get("comment"):
        for r in plan.creates:
            for n, c in enumerate(r["comments"], 1):
                if len(c["body"] or "") > limit["comment"]:
                    problems.append(f"comment {n} on {r['short']} is {len(c['body'])} characters; "
                                    f"{backend.name} accepts about {limit['comment']}")
    adapt = getattr(backend, "adapt_label", lambda x: x)
    for r in plan.creates:
        out = []
        for lb in r["labels"]:
            new = adapt(lb)
            if new != lb:
                plan.renamed_labels.append((r["short"], lb, new))
            out.append(new)
        plan.labels[r["id"]] = out

    if hasattr(backend, "seed_labels") and hasattr(backend, "existing_labels") and backend.required_labels([]):
        have = backend.existing_labels()
        lacking = sorted({lb for r in plan.creates if r["in_plan"]
                          for lb in backend.seed_labels(epic=r["kind"] == "epic", status=effective_status(r, opts),
                                                        autonomy=r["autonomy"], repo=r["repo"])} - have)
        if lacking:
            problems.append(f"{backend.name} lacks the swarm label(s) {', '.join(lacking)}: run "
                            f"`swarm.py backend init --apply` first")

    # what the swarm will see afterwards
    for r in plan.creates:
        if r["state"] == "closed" and r["status"] == "blocked":
            plan.kept_open.append(r["short"])
        if r["kind"] == "task" and r["in_plan"] and not _imported_closed(r) and effective_status(r, opts) == "ready":
            plan.ready_after += 1
        marks = [i for i, c in enumerate(r["comments"]) if _is_marker(c["body"])]
        last = marks[-1] if marks else -1
        late = [c for c in r["comments"][last + 1:] if is_human(c["author"])]
        if marks and late:
            plan.skipped_replies.append(f"{r['short']}: {len(late)} human repl{'y' if len(late) == 1 else 'ies'} "
                                        f"since its latest plan/question comment will be skipped by replies(); "
                                        f"repost them after the move")
        plan.calls += CALLS_PER_ISSUE[0] + len(r["comments"]) + len(plan.depends.get(r["id"], []))
    return plan


def _is_marker(body: str) -> bool:
    from dags import work                        # imported late: work pulls in the worker side
    return bool(work._MARKER_RE.search(body or ""))


def _imported_closed(r: dict) -> bool:
    return r["state"] == "closed" and r["status"] != "blocked"


def effective_status(r: dict, opts: Options) -> str | None:
    if opts.hold and r["in_plan"] and r["kind"] == "task" and r["state"] == "open":
        return "blocked"
    return r["status"]


def render(plan: Plan, opts: Options) -> list[str]:
    """The tree of what will be created, with the labels each gets, then the totals."""
    lines = []
    kids: dict[str | None, list[str]] = {}
    for r in plan.creates:
        kids.setdefault(plan.parent.get(r["id"]), []).append(r["id"])

    def walk(parent, depth):
        for i in kids.get(parent, []):
            r = plan.records[i]
            tag = [r["kind"] or "outside the plan", effective_status(r, opts) or "no status", r["state"]]
            lines.append(f"{'  ' * depth}{r['short']}  {r['title']}  [{', '.join(tag)}]"
                         + (f"  labels: {', '.join(plan.labels[i])}" if plan.labels.get(i) else ""))
            walk(i, depth + 1)
    walk(None, 0)
    lines.append("")
    lines.append(f"{len(plan.creates)} issues, anchor label {B.ANCHOR_PREFIX}{plan.run_id}, about {plan.calls} calls "
                 f"(roughly {CALLS_PER_ISSUE[0]} to {CALLS_PER_ISSUE[1]} per issue)")
    lines.append(f"{plan.ready_after} task(s) will be ready when the import finishes"
                 + (" (--hold: none until a human releases them)" if opts.hold else ""))
    for s in plan.kept_open:
        lines.append(f"closed and blocked, imported OPEN and blocked: {s}")
    for sh, a, b in plan.renamed_labels:
        lines.append(f"label renamed on {sh}: {a!r} -> {b!r}")
    lines += [f"note: {n}" for n in plan.notes]
    lines += [f"replies: {s}" for s in plan.skipped_replies]
    lines += [f"problem: {p}" for p in plan.problems]
    return lines


# -- writing ------------------------------------------------------------------------------------------------------

def compose_body(plan: Plan, r: dict) -> str:
    parts = [r["body"].rstrip()] if (r["body"] or "").strip() else []
    parts += [f"Part of: {t}" for t in plan.part_of.get(r["id"], [])]
    parts.append(B.footer(r["id"], r["url"]))
    return "\n\n".join(parts) + "\n"


def comment_key(r: dict, n: int, c: dict) -> str:
    return c["url"] or f"{r['id']}#comment-{n}"


def comment_text(r: dict, n: int, c: dict, forms: dict[str, str]) -> str:
    who = f"@{c['author']}" if c["author"] else "an unknown author"
    head = f"{B.IMPORTED_TAG}\nOriginally by {who} on {c['created_utc']} ({comment_key(r, n, c)})\n\n"
    return head + B.rewrite_refs(c["body"] or "", forms)


class Importer:
    def __init__(self, plan: Plan, backend, mapfile: MapFile, opts: Options, *, echo: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, guard: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.time, guard_every: int = 50):
        self.plan, self.b, self.map, self.opts = plan, backend, mapfile, opts
        self.echo, self.sleep, self.guard, self.clock = echo, sleep, guard, clock
        self.guard_every = guard_every
        self.writes = 0
        self.stats = {"created": 0, "skipped": 0, "writes": 0, "edited": 0, "changed": 0}
        self.lag = float(getattr(backend, "lag_window_s", 0) or 0)

    # every write goes through here: counted, guarded, and mapped to a clean stop
    def write(self, fn, *args, **kw):
        if self.writes and self.writes % self.guard_every == 0:
            self.guard()
        self.writes += 1
        self.stats["writes"] += 1
        try:
            return fn(*args, **kw)
        except BackendError as e:
            wait = getattr(e, "retry_after", None)
            if wait:
                at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() + float(wait)))
                raise ImportStop(f"rate limited: {e}. Resume at {at} by running the same command again",
                                 EXIT_RATE_LIMITED, at) from e
            raise ImportStop(f"{e}\nstopped at the first failure; the map is saved, run the same command again "
                             f"to continue") from e

    def ref(self, source_id: str) -> TaskRef:
        return TaskRef(self.map.issues[source_id]["target"])

    def fresh(self, ref: TaskRef):
        getter = getattr(self.b, "get_task_fresh", self.b.get_task)
        return getter(ref)

    def forms(self) -> dict[str, str]:
        targets = {}
        for i, e in self.map.issues.items():
            if e.get("target"):
                targets[i] = (e["target"], self.b.web_url(TaskRef(e["target"])))
        return B.reference_forms(list(self.plan.records.values()), targets,
                                 self.plan.bundle.manifest.get("source", {}).get("project"))

    # -- the three passes ---------------------------------------------------------------------------------------
    def run(self) -> dict:
        self.guard()
        self.ensure_labels()
        todo = self.plan.creates
        self.echo(f"pass 1/3: create {len(todo)} issues")
        for r in todo:
            self.create(r)
        self.guard()
        self.echo("pass 2/3: parents, then dependency links")
        for r in todo:
            self.parent(r)
        for r in todo:
            self.link(r)
        self.guard()
        self.echo("pass 3/3: comments, references, swarm labels, close")
        for name in ("comments", "body", "labels", "close"):
            for r in todo:
                getattr(self, name)(r)
            self.guard()
        self.stats["changed"] = sum(1 for r in todo if self.map.issues[r["id"]].get("hash") != B.content_hash(r))
        return self.stats

    def ensure_labels(self) -> None:
        if not hasattr(self.b, "existing_labels"):
            return
        have = self.b.existing_labels()
        need = {f"{B.ANCHOR_PREFIX}{self.plan.run_id}", *[lb for ls in self.plan.labels.values() for lb in ls]}
        for name in sorted(need - have):
            self.write(self.b.create_label, name, DEFAULT_COLOR)

    # -- pass 1 -------------------------------------------------------------------------------------------------
    def anchor(self) -> str:
        return f"{B.ANCHOR_PREFIX}{self.plan.run_id}"

    def create(self, r: dict) -> None:
        e = self.map.issues.get(r["id"])
        if e and e.get("target") and step_at_least(e, "created"):
            self.stats["skipped"] += 1
            return
        if e and e.get("step") == "creating":
            found = self.settle(r, e.get("anchor") or self.anchor())
            if found:
                self.map.set(r["id"], target=found.key, step="created", short=r["short"],
                             target_short=self.b.short_key(found))
                self.echo(f"adopted {found.key} for {r['short']} (created by an earlier run)")
                return
        anchor = self.anchor()
        self.map.set(r["id"], step="creating", anchor=anchor, short=r["short"], hash=B.content_hash(r),
                     bundle=self.plan.bundle.bundle_id, target=None)
        labels = [*self.plan.labels[r["id"]], anchor]
        epic = r["kind"] == "epic" or any(self.plan.parent.get(o) == r["id"] for o in self.plan.parent)
        ref = self.write(self.b.create_issue, r["title"], compose_body(self.plan, r), labels, epic=epic)
        self.map.set(r["id"], target=ref.key, step="created", target_short=self.b.short_key(ref))
        self.stats["created"] += 1
        self.echo(f"created {ref.key} for {r['short']}")

    def settle(self, r: dict, anchor: str):
        """An issue stuck in ``creating``: was the create call made? Ask by anchor label, a lag window apart,
        until two reads agree; if they never do, stop rather than risk a duplicate."""
        def listing():
            if hasattr(self.b, "invalidate"):
                self.b.invalidate()
            return sorted(t.ref.key for t in self.b.all_issues()
                          if anchor in t.labels and B.source_of(t.body) == r["id"])
        self.sleep(self.lag)
        first = listing()
        self.sleep(self.lag)
        second = listing()
        if first != second or len(second) > 1:
            raise ImportStop(f"cannot tell whether {r['short']} ({r['id']}) was created: reads disagree "
                             f"({first} then {second}). Look for it in {self.b.name} by the label {anchor}, then "
                             f"fix import-map.json by hand or delete the duplicate and run again")
        return TaskRef(second[0]) if second else None

    # -- pass 2 -------------------------------------------------------------------------------------------------
    def parent(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "parented"):
            return
        want = self.plan.parent.get(r["id"])
        if want:
            have = self.fresh(self.ref(r["id"])).epic
            if not have or have.key != self.ref(want).key:
                self.write(self.b.set_parent, self.ref(r["id"]), self.ref(want))
        self.map.set(r["id"], step="parented")

    def link(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "linked"):
            return
        wanted = [self.ref(d).key for d in self.plan.depends.get(r["id"], []) if d in self.map.issues]
        if wanted:
            have = {d.key for d in self.fresh(self.ref(r["id"])).dependencies}
            for key in wanted:
                if key not in have:
                    self.write(self.b.add_dependency, self.ref(r["id"]), TaskRef(key))
        self.map.set(r["id"], step="linked")

    # -- pass 3 -------------------------------------------------------------------------------------------------
    def comments(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "commented"):
            return
        if r["comments"]:
            have = {c.body.split("\n")[1] for c in self.b.list_comments(self.ref(r["id"]))
                    if c.body.startswith(B.IMPORTED_TAG) and len(c.body.split("\n")) > 1}
            forms = self.forms()
            for n, c in enumerate(r["comments"], 1):
                key = comment_key(r, n, c)
                if any(line.endswith(f"({key})") for line in have):
                    continue
                self.write(self.b.post_comment, self.ref(r["id"]), comment_text(r, n, c, forms))
        self.map.set(r["id"], step="commented")

    def body(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "bodied"):
            return
        current = self.fresh(self.ref(r["id"])).body
        new = B.rewrite_refs(current, self.forms())
        if new != current:
            self.write(self.b.update_body, self.ref(r["id"]), new)
            self.stats["edited"] += 1
        self.map.set(r["id"], step="bodied")

    def wanted_labels(self, r: dict) -> list[str]:
        if not r["in_plan"]:
            return []
        return self.b.seed_labels(epic=r["kind"] == "epic", status=effective_status(r, self.opts),
                                  autonomy=r["autonomy"], repo=r["repo"])

    def labels(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "labelled"):
            return
        want = self.wanted_labels(r)
        if want:
            have = self.fresh(self.ref(r["id"])).labels
            families = (STATUS_PREFIX, AUTONOMY_PREFIX, REPO_PREFIX)

            def taken(lb):                       # a label of that family is already there: leave it alone
                fam = next((p for p in families if lb.startswith(p)), None)
                return fam is not None and any(h.startswith(fam) for h in have)
            missing = [lb for lb in want if lb not in have and not taken(lb)]
            if missing:
                self.write(self.b.add_labels, self.ref(r["id"]), missing)
        self.map.set(r["id"], step="labelled")

    def close(self, r: dict) -> None:
        e = self.map.issues[r["id"]]
        if step_at_least(e, "closed"):
            return
        if r["state"] == "closed" and r["status"] != "blocked":
            if not self.fresh(self.ref(r["id"])).closed:
                self.write(self.b.close_issue, self.ref(r["id"]))
        self.map.set(r["id"], step="closed")


# -- a lost map ------------------------------------------------------------------------------------------------------

def rebuild_map(plan: Plan, backend, mapfile: MapFile, *, sleep: Callable[[float], None] = time.sleep,
                echo: Callable[[str], None] = print) -> int:
    """Reconstruct the map from the target: list by the ``dags-import-*`` label (any bundle id), read each issue
    by key and match the source id in its footer. Refuses to go on until two listings a lag window apart agree."""
    lag = float(getattr(backend, "lag_window_s", 0) or 0)
    first = anchored_issues(backend)
    for _ in range(3):
        sleep(lag)
        second = anchored_issues(backend)
        if sorted(first) == sorted(second):
            break
        first = second
    else:
        raise ImportStop("the listing of imported issues keeps changing (search is still catching up); "
                         "wait a few minutes and run --rebuild-map again")
    seen: dict[str, str] = {}
    for key in sorted(second):
        t = getattr(backend, "get_task_fresh", backend.get_task)(TaskRef(key))
        src = B.source_of(t.body)
        if not src:
            echo(f"skipping {key}: it carries an import label but no `Imported from` footer")
            continue
        if src in seen:
            raise ImportStop(f"{seen[src]} and {key} were both imported from {src}; delete the duplicate first")
        seen[src] = key
        anchor = next(lb for lb in t.labels if lb.startswith(B.ANCHOR_PREFIX))
        rec = plan.records.get(src)
        mapfile.set(src, target=key, step="created", anchor=anchor, target_short=backend.short_key(TaskRef(key)),
                    short=(rec or {}).get("short"), hash=B.content_hash(rec) if rec else None)
    return len(seen)
