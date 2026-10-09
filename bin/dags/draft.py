"""File an issue from a draft (GH-83, spec Ch.10.3).

A draft is a markdown file: YAML frontmatter, an H1 title, then the body.

    ---
    labels: [next-version]
    autonomy: human-must-review
    repo: OWNER/app
    epic: 7
    depends_on: [78]
    status: blocked          # optional
    ---
    # The title

    The body, verbatim.

``parse`` / ``render`` are the two directions of that format; ``validate`` lists
every problem against its field; ``plan_filing`` works out what would be written;
``apply`` writes it. The CLI (``backend file``) and the Board form are two front
ends onto these same functions, so neither can do what the other can't.

Issues are created through ``dags.gh`` without a token, i.e. with the operator's
own ``gh`` login and not the worker bot's. Nothing here ever sets an issue ready.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

import autonomy
from backends.base import (AUTONOMY_PREFIX, AUTONOMY_TIERS, DEFAULT_AUTONOMY, REPO_PREFIX, STATUS_PREFIX,
                           SWARM_STATUSES, TaskRef)
from dags import gh, seed

DRAFTS_DIR = "drafts"
FILED_DIR = "filed"
KEYS = ("labels", "autonomy", "repo", "epic", "depends_on", "status", "url")
OWNED_PREFIXES = (STATUS_PREFIX, AUTONOMY_PREFIX, REPO_PREFIX, "type:epic", "type:task")
H1_RE = re.compile(r"^#[ \t]+(\S.*?)[ \t]*#*[ \t]*$")


class DraftError(Exception):
    pass


@dataclass
class Problem:
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass
class Draft:
    title: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)
    autonomy: str | None = None
    repo: str | None = None
    epic: str | None = None
    depends_on: list[str] = field(default_factory=list)
    status: str | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# the format
# ---------------------------------------------------------------------------

def _refs(value, name: str) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    for i in items:
        if isinstance(i, (dict, list)) or isinstance(i, bool):
            raise DraftError(f"frontmatter {name}: expected issue numbers, got {i!r}")
    return [str(i).strip() for i in items if str(i).strip()]


def split(text: str) -> tuple[dict, str]:
    """Frontmatter mapping and the text after it."""
    text = text.lstrip("﻿")
    m = re.match(r"---[ \t]*\n(.*?)(?:\n|^)---[ \t]*(?:\n|\Z)", text, re.S)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise DraftError(f"frontmatter is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise DraftError("frontmatter must be a mapping")
    return data, text[m.end():]


def parse(text: str) -> Draft:
    data, rest = split(text)
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise DraftError(f"unknown frontmatter key(s): {', '.join(map(str, unknown))} "
                         f"(known: {', '.join(KEYS)})")
    lines = rest.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    m = H1_RE.match(lines[i]) if i < len(lines) else None
    if not m:
        raise DraftError("the draft must start (after any frontmatter) with an H1 title line: '# Title'")
    body = "\n".join(lines[i + 1:]).lstrip("\n")

    def text_of(k):
        v = data.get(k)
        return None if v is None else str(v).strip() or None

    labels = data.get("labels")
    if labels is not None and not isinstance(labels, list):
        labels = [labels]
    return Draft(title=m.group(1), body=body,
                 labels=[str(x).strip() for x in labels or [] if str(x).strip()],
                 autonomy=autonomy.LEGACY.get(text_of("autonomy"), text_of("autonomy")), repo=text_of("repo"),
                 epic=(_refs(data.get("epic"), "epic") or [None])[0],
                 depends_on=_refs(data.get("depends_on"), "depends_on"),
                 status=text_of("status"), url=text_of("url"))


def looks_like_draft(text: str) -> bool:
    """True when pasted text parses as a draft (the Board fills its form from it)."""
    t = text.lstrip("﻿\n \t")
    if not (t.startswith("---") or t.startswith("# ")):
        return False
    try:
        parse(text)
    except DraftError:
        return False
    return True


def render(d: Draft) -> str:
    front = {}
    if d.labels:
        front["labels"] = list(d.labels)
    for k in ("autonomy", "repo"):
        if getattr(d, k):
            front[k] = getattr(d, k)
    if d.epic:
        front["epic"] = int(d.epic) if d.epic.isdigit() else d.epic
    if d.depends_on:
        front["depends_on"] = [int(x) if x.isdigit() else x for x in d.depends_on]
    if d.status:
        front["status"] = d.status
    if d.url:
        front["url"] = d.url
    head = ""
    if front:
        head = "---\n" + yaml.safe_dump(front, sort_keys=False, default_flow_style=None,
                                        allow_unicode=True, width=1000) + "---\n"
    body = d.body if not d.body or d.body.endswith("\n") else d.body + "\n"
    return f"{head}# {d.title}\n\n{body}" if body else f"{head}# {d.title}\n"


def load(path: Path) -> Draft:
    try:
        return parse(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise DraftError(f"can't read {path}: {e}") from e
    except DraftError as e:
        raise DraftError(f"{path}: {e}") from e


# ---------------------------------------------------------------------------
# labels and validation
# ---------------------------------------------------------------------------

def resolved_labels(backend, d: Draft) -> list[str]:
    """The exact label set the issue gets: the plain labels, then what autonomy,
    repo and status map onto. The Board's live preview shows this same list."""
    labels = list(dict.fromkeys(d.labels))
    owned = backend.seed_labels(epic=False, status=d.status or None,
                                autonomy=d.autonomy or DEFAULT_AUTONOMY, repo=d.repo or None)
    return labels + [lb for lb in owned if lb not in labels]


def _lookup(backend, text: str):
    try:
        ref = backend.parse_ref(text)
    except ValueError:
        return None, None
    by_key = {t.ref.key: t for t in backend.all_issues()}
    return ref, by_key.get(ref.key)


def validate(d: Draft, backend, code_repos: list[str]) -> list[Problem]:
    """Every problem with the draft, each against the field it belongs to.
    Reads the tracker (labels, issues); writes nothing."""
    seed.check_backend(backend)
    out: list[Problem] = []
    if not d.title.strip():
        out.append(Problem("title", "missing"))
    if d.autonomy and d.autonomy not in AUTONOMY_TIERS:
        out.append(Problem("autonomy", f"{d.autonomy!r} is not one of {', '.join(AUTONOMY_TIERS)}"))
    if not d.repo:
        out.append(Problem("repo", "missing"))
    elif code_repos and d.repo not in code_repos:
        out.append(Problem("repo", f"{d.repo} is not listed under repos: in backend.yaml"))
    if d.status == "ready":
        out.append(Problem("status", "filing never sets an issue ready; do that as a separate decision"))
    elif d.status and d.status not in SWARM_STATUSES:
        out.append(Problem("status", f"{d.status!r} is not one of {', '.join(SWARM_STATUSES)}"))

    for lb in d.labels:
        if lb.startswith(OWNED_PREFIXES):
            field_ = ("status" if lb.startswith(STATUS_PREFIX) else "autonomy" if lb.startswith(AUTONOMY_PREFIX)
                      else "repo" if lb.startswith(REPO_PREFIX) else "the filing itself")
            out.append(Problem("labels", f"{lb} is set from {field_}; remove it from labels"))
    have = backend.existing_labels()
    for lb in d.labels:
        if not lb.startswith(OWNED_PREFIXES) and lb not in have:
            out.append(Problem("labels", f"{lb} does not exist on the tracker"))
    if not any(p.field in ("autonomy", "repo", "status") for p in out):
        for lb in resolved_labels(backend, d):
            if lb.startswith(OWNED_PREFIXES) and lb not in have:
                out.append(Problem("labels", f"{lb} does not exist; run `swarm.py backend init --apply`"))

    epic = None
    if not d.epic:
        out.append(Problem("epic", "missing; every issue is filed under an epic"))
    else:
        ref, epic = _lookup(backend, d.epic)
        if ref is None:
            out.append(Problem("epic", f"{d.epic!r} is not an issue reference"))
        elif epic is None:
            out.append(Problem("epic", f"{d.epic} does not exist"))
        elif not epic.is_epic:
            out.append(Problem("epic", f"{backend.short_key(ref)} is not an epic"))
    seen = set()
    for dep in d.depends_on:
        ref, t = _lookup(backend, dep)
        if ref is None:
            out.append(Problem("depends_on", f"{dep!r} is not an issue reference"))
        elif t is None:
            out.append(Problem("depends_on", f"{dep} does not exist"))
        elif t.is_epic:
            out.append(Problem("depends_on", f"{backend.short_key(ref)} is an epic; depend on tasks instead"))
        elif ref.key in seen:
            out.append(Problem("depends_on", f"{backend.short_key(ref)} is listed twice"))
        seen.add(ref.key)
    return out


# ---------------------------------------------------------------------------
# the diff and the write
# ---------------------------------------------------------------------------

@dataclass
class Filing:
    draft: Draft
    labels: list[str]
    epic: TaskRef
    depends_on: list[TaskRef]
    existing: TaskRef | None = None       # an earlier, part-way filing of this draft
    missing_epic: bool = True
    missing_deps: list[TaskRef] = field(default_factory=list)

    def lines(self, short) -> list[str]:
        out = []
        if self.existing is None:
            out.append(f"create  task: {self.draft.title}  [{', '.join(self.labels)}]")
        else:
            out.append(f"resume  {short(self.existing)}: {self.draft.title} (already created)")
        who = short(self.existing) if self.existing else "(new)"
        if self.missing_epic:
            out.append(f"parent  {who} -> epic {short(self.epic)}")
        for dep in self.missing_deps:
            out.append(f"depends {who} blocked by {short(dep)}")
        return out


def plan_filing(d: Draft, backend) -> Filing:
    """What filing would write. The draft must have passed ``validate``."""
    epic = backend.parse_ref(d.epic)
    deps = [backend.parse_ref(x) for x in d.depends_on]
    f = Filing(d, resolved_labels(backend, d), epic, deps, missing_deps=list(deps))
    if d.url:                                    # stamped by a filing that stopped part-way
        try:
            f.existing = backend.parse_ref(_number(d.url))
            t = {x.ref.key: x for x in backend.all_issues()}.get(f.existing.key)
        except ValueError:
            raise DraftError(f"url {d.url!r} is not an issue of this tracker") from None
        if t is None:
            raise DraftError(f"url {d.url} does not match an issue on the tracker")
        f.missing_epic = t.epic is None or t.epic.key != epic.key
        f.missing_deps = [r for r in deps if r.key not in {x.key for x in t.dependencies}]
    return f


def _number(url: str) -> str:
    m = re.search(r"/issues/(\d+)", url)
    return m.group(1) if m else url


class FilingError(Exception):
    """The issue exists but a later step failed; ``url`` says which."""

    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(message)


def apply(f: Filing, backend, echo=print) -> TaskRef:
    """Create the issue and link it. Never touches status beyond the labels it was given."""
    seed.check_backend(backend)
    ref = f.existing
    if ref is None:
        ref = backend.create_issue(f.draft.title, f.draft.body, f.labels, epic=False)
        echo(f"created {backend.short_key(ref)}: {f.draft.title}")
    try:
        if f.missing_epic:
            backend.set_parent(ref, f.epic)
            echo(f"parent  {backend.short_key(ref)} -> epic {backend.short_key(f.epic)}")
        for dep in f.missing_deps:
            backend.add_dependency(ref, dep)
            echo(f"depends {backend.short_key(ref)} blocked by {backend.short_key(dep)}")
    except gh.GhError as e:
        raise FilingError(backend.web_url(ref) or ref.key, str(e)) from e
    return ref


def is_filed(path: Path) -> bool:
    return Path(path).parent.name == FILED_DIR


def stamp(path: Path, d: Draft, url: str) -> None:
    Path(path).write_text(render(replace(d, url=url)), encoding="utf-8")


def move_to_filed(path: Path, d: Draft, url: str, root: Path) -> Path:
    """Stamp the URL and move the draft to drafts/filed/, so it can't be filed twice."""
    dest = Path(root) / DRAFTS_DIR / FILED_DIR / Path(path).name
    if dest.exists():
        raise DraftError(f"{dest} already exists; rename the draft and move it by hand")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(replace(d, url=url)), encoding="utf-8")
    Path(path).unlink()
    return dest


@dataclass
class Result:
    url: str
    ref: TaskRef
    moved_to: Path | None
    synced: bool
    sync_error: str = ""


def file_draft(ctx, path: Path, d: Draft, f: Filing, echo=print) -> Result:
    """The one write path: file, plan sync, stamp and move. The caller has validated and
    shown the diff. On a failure after the issue exists, the draft is stamped with its URL
    and stays where it is, so running the same command again finishes the links."""
    from dags import plan
    backend = ctx.backend
    if is_filed(path):
        raise DraftError(f"{path} is already filed ({d.url or 'see drafts/filed'})")
    try:
        ref = apply(f, backend, echo)
    except FilingError as e:
        stamp(path, d, e.url)
        raise DraftError(f"{e}\nthe issue exists: {e.url}\nthe draft was stamped with it and left in "
                         f"drafts/; run the same command again to finish the links") from e
    url = backend.web_url(ref) or ref.key
    synced, sync_error = True, ""
    try:
        plan.sync(ctx)
    except Exception as e:                       # the issue is filed either way
        synced, sync_error = False, str(e)
    moved = move_to_filed(path, d, url, ctx.root)
    return Result(url, ref, moved, synced, sync_error)
