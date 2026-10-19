"""The issue bundle: a backend-neutral zip of a tracker's issues (GH-124).

Pure: no prompts, no network, no clock. An adapter yields ``ExportItem``s; this module decides what goes in
the bundle, orders it, writes it byte for byte the same every time, and reads one back safely. Import (a
later change) builds on ``read_bundle`` and the record shape defined here.

    bundle.zip
      manifest.json         format, source, plan_scope, scope, tool commit, counts, sha256 of each issue file
      issues/0001.json ...  one issue each, parents before children, blockers before dependants
      README.txt
"""
from __future__ import annotations

import hashlib
import heapq
import json
import re
import zipfile
from zlib import error as zlib_error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from backends.base import (AUTONOMY_PREFIX, REPO_PREFIX, STATUS_PREFIX, SWARM_STATUSES, TYPE_LABELS, ExportItem,
                           parse_labels, split_plan)

FORMAT = "dags-issue-bundle/1"
MANIFEST = "manifest.json"
README = "README.txt"
ISSUE_RE = re.compile(r"^issues/(\d{4,})\.json$")
ZIP_DATE = (2020, 1, 1, 0, 0, 0)
MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 50_000
TRANSIENT = ("claimed", "in-progress", "awaiting-review")
SCOPES = ("all", "plan")

README_TEXT = """\
This is a DAGS issue bundle (format {format}).

It holds the issues of one tracker, in dependency order, with their comments and labels. It is the input of
`swarm.py backend import`, which can write it into whichever backend backend.yaml names.

WARNING: it contains private issue text and comments. Do not commit it to a repository, and share it only
with people who may read the tracker.

To import it:
  1. stop every machine (`swarm.py stop`; a crashed one needs `swarm.py stop --machine NAME`)
  2. point backend.yaml at the new tracker, then `swarm.py backend init --apply`
  3. `swarm.py backend import this-file.zip` (a dry run), then again with --apply
  4. `swarm.py ledger rekey import-map.json`, then `swarm.py plan sync`
"""


class BundleError(ValueError):
    """A bundle that cannot be written, or fails a check on reading. It is refused whole."""


@dataclass
class Bundle:
    manifest: dict
    records: list[dict]
    report: dict = field(default_factory=dict)

    @property
    def bundle_id(self) -> str:
        return self.manifest["bundle_id"]


# -- records ------------------------------------------------------------------------------------------------

def _is_swarm_label(name: str) -> bool:
    return (name.startswith(STATUS_PREFIX) or name.startswith(AUTONOMY_PREFIX) or name.startswith(REPO_PREFIX)
            or name in TYPE_LABELS)


def _comment(c) -> dict:
    return {"author": c.author, "created_utc": c.created_at, "body": c.body, "url": c.url}


def _ordered(records: dict[str, dict]) -> list[dict]:
    """Parents before children, blockers before dependants; ties by (created_utc, source id)."""
    deps: dict[str, set[str]] = {}
    for key, r in records.items():
        need = {d for d in r["depends_on"] if isinstance(d, str)}
        if r["parent"]:
            need.add(r["parent"])
        deps[key] = need
    users: dict[str, list[str]] = {k: [] for k in records}
    for key, need in deps.items():
        for n in need:
            users[n].append(key)
    sort = lambda k: (records[k]["created_utc"] or "", k)                       # noqa: E731
    heap = [(sort(k), k) for k, need in deps.items() if not need]
    heapq.heapify(heap)
    out, left = [], {k: len(need) for k, need in deps.items()}
    while heap:
        _, key = heapq.heappop(heap)
        out.append(records[key])
        for u in users[key]:
            left[u] -= 1
            if left[u] == 0:
                heapq.heappush(heap, (sort(u), u))
    if len(out) != len(records):
        stuck = sorted(k for k, n in left.items() if n)
        raise BundleError(f"the parent/dependency links form a cycle through {', '.join(stuck[:5])}"
                          f"{' ...' if len(stuck) > 5 else ''}")
    return out


def _descendants(items: dict[str, ExportItem], roots: set[str]) -> set[str]:
    kids: dict[str, list[str]] = {}
    for key, it in items.items():
        if it.task.epic:
            kids.setdefault(it.task.epic.key, []).append(key)
    out, todo = set(), list(roots)
    while todo:
        key = todo.pop()
        if key not in out:
            out.add(key)
            todo += kids.get(key, [])
    return out


def build(items: Iterable[ExportItem], *, plan_scope: str, scope: str = "all", roots: Iterable[str] = (),
          open_only: bool = False, keep: Iterable[str] = (), source: dict, tool_commit: str | None,
          short_of: Callable[[str], str], url_of: Callable[[str], str | None],
          extra: Iterable[ExportItem] = (), include_external: bool = False) -> Bundle:
    """Choose, order and describe the issues. ``roots`` are source keys (callers resolve short names);
    ``keep`` are keys the ledger refers to (they stay under ``scope=plan`` even when closed); ``extra`` are
    issues of other repositories that were fetched because ``include_external`` was asked for."""
    if scope not in SCOPES:
        raise BundleError(f"scope must be one of {', '.join(SCOPES)} (got {scope!r})")
    by_key = {it.task.ref.key: it for it in items}
    members, _ = split_plan([it.task for it in by_key.values()], plan_scope)
    in_plan = {t.ref.key for t in members}

    chosen = set(by_key)
    if scope == "plan":
        chosen = set(in_plan) | {k for k in keep if k in by_key and by_key[k].task.closed}
        grew = True
        while grew:                                   # closed issues a kept issue depends on, or sits under
            grew = False
            for k in list(chosen):
                t = by_key[k].task
                for ref in [*t.dependencies, *([t.epic] if t.epic else [])]:
                    if ref.key in by_key and ref.key not in chosen and by_key[ref.key].task.closed:
                        chosen.add(ref.key)
                        grew = True
    roots = set(roots)
    if roots:
        missing = sorted(roots - set(by_key))
        if missing:
            raise BundleError(f"--root {', '.join(missing)}: no such issue in the tracker")
        chosen &= _descendants(by_key, roots)
    dropped_closed = set()
    if open_only:
        dropped_closed = {k for k in chosen if by_key[k].task.closed}
        chosen -= dropped_closed
    for it in extra:
        by_key.setdefault(it.task.ref.key, it)
        chosen.add(it.task.ref.key)

    report: dict = {"dropped_dependencies": [], "dropped_parents": [], "external": [], "warnings": []}
    records: dict[str, dict] = {}
    for key in sorted(chosen):
        it = by_key[key]
        t = it.task
        planned = key in in_plan
        parsed = parse_labels(t.labels)
        parent = t.epic.key if t.epic else None
        if parent and parent not in chosen:
            report["dropped_parents"].append({"id": key, "parent": parent})
            parent = None
        depends: list = []
        for ref in sorted(t.dependencies, key=lambda r: r.key):
            if ref.key in chosen:
                depends.append(ref.key)
            elif ref.key in by_key:
                report["dropped_dependencies"].append({"id": key, "depends_on": ref.key})
            else:
                depends.append({"id": ref.key, "url": url_of(ref.key), "external": True})
                report["external"].append({"id": key, "depends_on": ref.key})
        status = parsed["status"] if parsed["status"] in SWARM_STATUSES else None
        records[key] = {
            "id": key, "short": short_of(key), "url": t.url or url_of(key),
            "title": t.title, "body": t.body,
            "in_plan": planned, "kind": ("epic" if t.is_epic else "task") if planned else None,
            "parent": parent, "depends_on": depends,
            "status": status if planned else None,
            "autonomy": parsed["autonomy"] if planned else None,
            "repo": parsed["repo"] if planned else None,
            "labels": sorted(lb for lb in t.labels if not _is_swarm_label(lb)),
            "state": "closed" if t.closed else "open", "closed_utc": it.closed_utc if t.closed else None,
            "created_utc": it.created_utc, "author": it.author,
            "comments": [_comment(c) for c in it.comments],
            "lossy": sorted(it.lossy),
        }
        if planned and status in TRANSIENT:
            report["warnings"].append(f"{short_of(key)} is {status}: that only means something while a swarm "
                                      f"runs, and it will be imported as {status}")
    ordered = _ordered(records)
    return seal(ordered, plan_scope=plan_scope, scope=scope, source=source, tool_commit=tool_commit,
                options={"roots": sorted(roots), "open_only": open_only, "include_external": include_external},
                report=report)


# -- writing -------------------------------------------------------------------------------------------------

def _dump(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def issue_name(n: int) -> str:
    return f"issues/{n:04d}.json"


def summarize(records: list[dict]) -> dict:
    by_id = {r["id"]: r for r in records}
    depth: dict[str, int] = {}

    def level(r) -> int:
        if r["id"] not in depth:
            depth[r["id"]] = 1 + (level(by_id[r["parent"]]) if r["parent"] in by_id else 0)
        return depth[r["id"]]

    kinds: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for r in records:
        kinds[r["kind"] or "not in plan"] = kinds.get(r["kind"] or "not in plan", 0) + 1
        if r["status"]:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {"issues": len(records), "by_kind": dict(sorted(kinds.items())),
            "by_status": dict(sorted(statuses.items())),
            "open": sum(r["state"] == "open" for r in records),
            "closed": sum(r["state"] == "closed" for r in records),
            "comments": sum(len(r["comments"]) for r in records),
            "deepest_level": max((level(r) for r in records), default=0)}


def seal(records: list[dict], *, plan_scope: str, scope: str, source: dict, tool_commit: str | None,
         options: dict, report: dict | None = None) -> Bundle:
    files = {issue_name(i): _dump(r) for i, r in enumerate(records, 1)}
    sums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    bundle_id = hashlib.sha256("\n".join(f"{n} {h}" for n, h in sorted(sums.items())).encode()).hexdigest()[:8]
    manifest = {"format": FORMAT, "bundle_id": bundle_id, "source": source, "plan_scope": plan_scope,
                "scope": scope, "options": options, "tool_commit": tool_commit,
                "counts": summarize(records), "sha256": dict(sorted(sums.items()))}
    return Bundle(manifest, records, {**(report or {}), **summarize(records)})


def write_bundle(bundle: Bundle, path: str | Path) -> None:
    """Same bytes for the same bundle: sorted entries, fixed timestamps, stable key order, no export time."""
    entries = [(MANIFEST, _dump(bundle.manifest)), (README, README_TEXT.format(format=FORMAT).encode()),
               *[(issue_name(i), _dump(r)) for i, r in enumerate(bundle.records, 1)]]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, data)


# -- reading ---------------------------------------------------------------------------------------------------

REQUIRED = ("id", "short", "url", "title", "body", "in_plan", "kind", "parent", "depends_on", "status",
            "autonomy", "repo", "labels", "state", "closed_utc", "created_utc", "author", "comments", "lossy")


def _safe_name(name: str) -> None:
    if (name.startswith(("/", "\\")) or "\\" in name or re.match(r"^[A-Za-z]:", name)
            or ".." in name.split("/") or name.endswith("/")):
        raise BundleError(f"unsafe entry name {name!r}")
    if name not in (MANIFEST, README) and not ISSUE_RE.match(name):
        raise BundleError(f"unexpected entry {name!r}")


def read_bundle(path: str | Path, *, max_entry: int = MAX_ENTRY_BYTES, max_total: int = MAX_TOTAL_BYTES) -> Bundle:
    """Read and verify a bundle. Every check runs before anything is used; one failure refuses the whole file."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BundleError(f"{path} is not a readable zip file: {e}") from e
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BundleError(f"too many entries ({len(infos)})")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise BundleError("duplicate entry names")
        total = 0
        for i in infos:
            _safe_name(i.filename)
            if i.file_size > max_entry:
                raise BundleError(f"{i.filename} is {i.file_size} bytes, over the {max_entry} byte limit")
            total += i.file_size
        if total > max_total:
            raise BundleError(f"the bundle expands to {total} bytes, over the {max_total} byte limit")

        def read(name: str) -> bytes:
            try:
                with z.open(name) as f:
                    data = f.read(max_entry + 1)                    # the header's size may lie
                if len(data) > max_entry:
                    raise BundleError(f"{name} is over the {max_entry} byte limit")
                return data
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, zlib_error) as e:
                raise BundleError(f"{name} cannot be read: {e}") from e

        if MANIFEST not in names:
            raise BundleError("no manifest.json")
        try:
            manifest = json.loads(read(MANIFEST))
        except ValueError as e:
            raise BundleError(f"manifest.json is not valid JSON: {e}") from e
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BundleError(f"unknown bundle format {manifest.get('format') if isinstance(manifest, dict) else manifest!r}"
                              f" (this tool reads {FORMAT})")
        sums = manifest.get("sha256")
        issue_names = sorted(n for n in names if ISSUE_RE.match(n))
        if not isinstance(sums, dict) or sorted(sums) != issue_names:
            raise BundleError("the manifest and the issue files do not list the same entries")
        records = []
        for name in issue_names:
            data = read(name)
            if hashlib.sha256(data).hexdigest() != sums[name]:
                raise BundleError(f"checksum mismatch for {name}")
            try:
                rec = json.loads(data)
            except ValueError as e:
                raise BundleError(f"{name} is not valid JSON: {e}") from e
            missing = [k for k in REQUIRED if not isinstance(rec, dict) or k not in rec]
            if missing:
                raise BundleError(f"{name} lacks {', '.join(missing)}")
            records.append(rec)
    bundle = Bundle(manifest, records, summarize(records))
    check_graph(bundle)
    return bundle


def check_graph(bundle: Bundle) -> None:
    ids = [r["id"] for r in bundle.records]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise BundleError(f"duplicate issue ids: {', '.join(dup)}")
    known = set(ids)
    for r in bundle.records:
        if r["parent"] is not None and r["parent"] not in known:
            raise BundleError(f"{r['id']} has parent {r['parent']}, which is not in the bundle")
        for d in r["depends_on"]:
            if isinstance(d, str) and d not in known:
                raise BundleError(f"{r['id']} depends on {d}, which is not in the bundle")
    if bundle.manifest.get("counts", {}).get("issues") != len(bundle.records):
        raise BundleError("the manifest's issue count does not match the issue files")
    _ordered({r["id"]: r for r in bundle.records})                      # raises on a cycle

