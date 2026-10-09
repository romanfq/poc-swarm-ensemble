"""Jira Cloud issue adapter (GH-123).

The adapter owns what the swarm means by a Jira issue; ``dags.jira_client`` owns the wire. It imports only
the ``JiraClient`` interface and builds one through ``build_client``.

Where each swarm concept lives:
    status / autonomy / repo   labels (``swarm:status:ready``, ...), so ``parse_labels`` is reused; the Jira
                               workflow is untouched except that ``done`` closes the issue
    epic or task               the issue type: ``hierarchyLevel >= 1`` is an epic. A Task is also a container
                               when a subtask of it carries a swarm label (a container is never claimed)
    parent                     ``fields.parent``
    dependencies               issue links of type ``blocks_link_type``
    closed                     status category ``done``
    key / short key / url      ``KAN-12`` / ``KAN-12`` / ``{base_url}/browse/KAN-12``

Search is eventually consistent, so writes are applied to the cached snapshot in place (write-through
overlay) and the written issue ids are passed as ``reconcileIssues`` on later searches until they age out
(persisted in ``.swarm/jira-recent.json`` so a re-run in a new process is covered too). Because a human's
write in the Jira UI can still lag, ``lagging_search`` tells the scheduler to re-read a claim candidate and
its epic chain directly before it claims.

backend.yaml:
    backend: jira
    jira:
      base_url: https://yourteam.atlassian.net
      project_key: KAN
      blocks_link_type: Blocks
      issue_types: {epic: Epic, task: Task}
      cache_seconds: 20
      transitions: {}              # optional, e.g. {done: Done, in-progress: In Progress}
    plan_scope: labelled          # top level; see backends.base.split_plan
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from backends.base import (AUTONOMY_PREFIX, AUTONOMY_TIERS, DEFAULT_AUTONOMY, DEFAULT_PLAN_SCOPE, REPO_PREFIX,
                           STATUS_PREFIX, SWARM_PREFIX, SWARM_STATUSES, Comment, PlanScoped, Task, TaskRef,
                           parse_labels, plan_scope_of)
from dags import adf
from dags.jira_client import JiraClient, JiraError, build_client

log = logging.getLogger("dags.jira")

SEARCH_FIELDS = ["summary", "description", "status", "labels", "issuetype", "parent", "issuelinks"]
KEY_RE = re.compile(r"^(?P<project>[A-Za-z][A-Za-z0-9_]*)-(?P<num>\d+)$")
COMMENT_PROPERTY = "dags.markdown"      # the markdown we posted, kept beside the ADF for an exact round trip
COMMENT_LIMIT = 30000                   # bytes of markdown per comment (Jira's own limit is recalled as ~32k)
RECENT_TTL = 600.0                      # seconds a written issue id is passed as reconcileIssues
RECONCILE_BATCH = 50


class LabelCodec:
    """The only place a swarm label name becomes a Jira label and back. Jira labels cannot contain spaces;
    whether ``:`` and ``/`` are accepted is verified against the mirror site. If they are not, fill
    ``SUBSTITUTIONS`` (for example ``(("/", "__"),)``) and nothing else changes."""
    SUBSTITUTIONS: tuple[tuple[str, str], ...] = ()

    def encode(self, name: str) -> str:
        for plain, stored in self.SUBSTITUTIONS:
            name = name.replace(plain, stored)
        return name

    def decode(self, stored: str) -> str:
        for plain, stored_form in reversed(self.SUBSTITUTIONS):
            stored = stored.replace(stored_form, plain)
        return stored


def utc_iso(stamp: str | None) -> str:
    """Jira's ``2026-10-09T10:00:00.000+0100`` as UTC ``2026-10-09T09:00:00Z`` (empty stays empty)."""
    if not stamp:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(stamp, fmt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return stamp


def chunk_markdown(text: str, limit: int = COMMENT_LIMIT) -> list[str]:
    """Split ``text`` on line boundaries into pieces of at most ``limit`` bytes (one longer line is cut)."""
    if len(text.encode()) <= limit:
        return [text]
    lines: list[str] = []
    for line in text.split("\n"):
        step = max(1, limit // 4)                       # worst case 4 bytes per character
        lines += [line[i:i + step] for i in range(0, len(line), step)] if len(line.encode()) > limit else [line]
    out, cur = [], ""
    for line in lines:
        joined = f"{cur}\n{line}" if cur else line
        if cur and len(joined.encode()) > limit:
            out.append(cur)
            cur = line
        else:
            cur = joined
    if cur:
        out.append(cur)
    return out


class JiraBackend(PlanScoped):
    name = "jira"
    uses_type_labels = False
    epics_carry_status = False          # containers get no swarm status on adoption
    lagging_search = True               # the scheduler re-reads a candidate before it claims

    def __init__(self, client: JiraClient, project_key: str, base_url: str, *, blocks_link_type: str = "Blocks",
                 issue_types: dict | None = None, cache_seconds: float = 20, transitions: dict | None = None,
                 plan_scope: str = DEFAULT_PLAN_SCOPE, recent_path: Path | None = None,
                 code_repos: list[str] | None = None, clock=time.time):
        if not project_key or project_key.upper() == "KEY":
            raise ValueError("backend.yaml jira.project_key must be your project's key (e.g. KAN)")
        self.client = client
        self.project_key = project_key
        self.base_url = base_url.rstrip("/")
        self.blocks_link_type = blocks_link_type
        types = {"epic": "Epic", "task": "Task", **(issue_types or {})}
        self.epic_type, self.task_type = types["epic"], types["task"]
        self.cache_seconds = cache_seconds
        self.transitions = {str(k): str(v) for k, v in (transitions or {}).items()}
        self.plan_scope = plan_scope
        self.recent_path = Path(recent_path) if recent_path else None
        self.code_repos = list(code_repos or [])
        self.codec = LabelCodec()
        self._clock = clock
        self._lock = threading.RLock()
        self._raw: dict[str, dict] | None = None
        self._tasks_cache: dict[str, Task] | None = None
        self._snap_at = 0.0
        self._recent: dict[str, float] = self._load_recent()
        self._props_ok = True

    @classmethod
    def from_config(cls, cfg: dict, ctx) -> "JiraBackend":
        local = ctx.local
        return cls(build_client(cfg, local), str(cfg.get("project_key", "")), str(cfg.get("base_url", "")),
                   blocks_link_type=str(cfg.get("blocks_link_type") or "Blocks"),
                   issue_types=cfg.get("issue_types") or None, cache_seconds=float(cfg.get("cache_seconds", 20)),
                   transitions=cfg.get("transitions") or None, plan_scope=plan_scope_of(ctx.backend_cfg),
                   recent_path=ctx.swarm_dir / "jira-recent.json",
                   code_repos=sorted((ctx.backend_cfg.get("repos") or {}).keys()))

    def whoami(self) -> dict:
        """The connected account (``accountId`` is what ``humans.yaml`` ``jira:`` takes)."""
        return self.client.myself()

    # -- refs ------------------------------------------------------------------------------------------
    def parse_ref(self, text: str) -> TaskRef:
        """Accepts 'KAN-7', 'kan-7', '7', '#7' and a '.../browse/KAN-7' URL."""
        text = str(text).strip()
        m = re.search(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)", text)
        if m:
            text = m.group(1)
        num = re.match(r"^#?(\d+)$", text)
        if num:
            return TaskRef(f"{self.project_key}-{int(num.group(1))}")
        k = KEY_RE.match(text)
        if k and k.group("project").upper() == self.project_key.upper():
            return TaskRef(f"{self.project_key}-{int(k.group('num'))}")
        raise ValueError(f"not a {self.project_key} issue reference: {text!r}")

    def short_key(self, ref: TaskRef) -> str:
        return ref.key

    def web_url(self, ref: TaskRef) -> str | None:
        return f"{self.base_url}/browse/{ref.key}"

    def coordination_ref(self, ref: TaskRef) -> str:
        return ref.key

    # -- recent writes (reconcileIssues) -----------------------------------------------------------------
    def _load_recent(self) -> dict[str, float]:
        if not self.recent_path:
            return {}
        try:
            data = json.loads(self.recent_path.read_text())
        except (OSError, ValueError):
            return {}
        now = self._clock()
        return {str(k): float(v) for k, v in data.items() if now - float(v) < RECENT_TTL} \
            if isinstance(data, dict) else {}

    def _note_written(self, issue_id: str | int | None) -> None:
        if issue_id is None:
            return
        with self._lock:
            now = self._clock()
            self._recent[str(issue_id)] = now
            self._recent = {k: v for k, v in self._recent.items() if now - v < RECENT_TTL}
            if self.recent_path:
                try:
                    self.recent_path.parent.mkdir(parents=True, exist_ok=True)
                    self.recent_path.write_text(json.dumps(self._recent))
                except OSError as e:
                    log.warning("could not persist %s: %s", self.recent_path, e)

    def _recent_ids(self) -> list[str]:
        now = self._clock()
        return [k for k, v in self._recent.items() if now - v < RECENT_TTL]

    def _id_of(self, key: str) -> str | None:
        raw = (self._raw or {}).get(key)
        if raw and raw.get("id"):
            return str(raw["id"])
        try:
            return str(self.client.get_issue(key, ["labels"]).get("id") or "") or None
        except JiraError:
            return None

    # -- reading -------------------------------------------------------------------------------------------
    def _search_all(self) -> dict[str, dict]:
        jql = f'project = "{self.project_key}" ORDER BY created ASC'
        recent = self._recent_ids()
        batches = [recent[i:i + RECONCILE_BATCH] for i in range(0, len(recent), RECONCILE_BATCH)] or [None]
        out: dict[str, dict] = {}
        for batch in batches:
            token = None
            while True:
                page = self.client.search(jql, SEARCH_FIELDS, next_page_token=token, max_results=100,
                                          reconcile_issues=[int(i) for i in batch] if batch else None)
                for issue in page.get("issues") or []:
                    out[issue["key"]] = issue
                token = page.get("nextPageToken")
                if page.get("isLast", True) or not token:
                    break
        return out

    def _snapshot(self, fresh: bool = False) -> dict[str, dict]:
        with self._lock:
            if fresh or self._raw is None or self._clock() - self._snap_at > self.cache_seconds:
                found = self._search_all()
                # an overlay not yet visible to search must not be lost: keep recently written issues we hold
                for key, issue in (self._raw or {}).items():
                    if key not in found and str(issue.get("id")) in self._recent_ids():
                        found[key] = issue
                self._raw, self._tasks_cache, self._snap_at = found, None, self._clock()
            return self._raw

    def invalidate(self) -> None:
        with self._lock:
            self._snap_at = float("-inf")

    def _labels(self, raw: dict) -> list[str]:
        return [self.codec.decode(x) for x in (raw.get("fields") or {}).get("labels") or []]

    def _is_epic_type(self, fields: dict) -> bool:
        itype = fields.get("issuetype") or {}
        level = itype.get("hierarchyLevel")
        if level is not None:
            return int(level) >= 1
        return str(itype.get("name") or "") == self.epic_type

    def _to_task(self, raw: dict, containers: set[str] | None = None) -> Task:
        f = raw.get("fields") or {}
        key = raw["key"]
        labels = self._labels(raw)
        parsed = parse_labels(labels)
        parent = (f.get("parent") or {}).get("key")
        deps = []
        for link in f.get("issuelinks") or []:
            if (link.get("type") or {}).get("name") == self.blocks_link_type and link.get("inwardIssue"):
                deps.append(TaskRef(link["inwardIssue"]["key"]))     # on a read, inwardIssue is the BLOCKER
        category = ((f.get("status") or {}).get("statusCategory") or {}).get("key")
        return Task(
            ref=TaskRef(key), title=f.get("summary") or "",
            body=adf.to_markdown(f.get("description")),
            status=parsed["status"] if parsed["status"] in SWARM_STATUSES else None,
            autonomy=parsed["autonomy"] or DEFAULT_AUTONOMY,
            epic=TaskRef(parent) if parent else None, dependencies=deps, repo=parsed["repo"],
            url=f"{self.base_url}/browse/{key}",
            is_epic=self._is_epic_type(f) or key in (containers or ()),
            closed=category == "done", labels=labels)

    def _tasks(self) -> dict[str, Task]:
        with self._lock:
            raw = self._snapshot()
            if self._tasks_cache is None:
                containers = set()
                for r in raw.values():                      # a Task with a swarm-labelled subtask is a container
                    parent = ((r.get("fields") or {}).get("parent") or {}).get("key")
                    if parent and any(lb.startswith(SWARM_PREFIX) for lb in self._labels(r)):
                        containers.add(parent)
                self._tasks_cache = {k: self._to_task(r, containers) for k, r in raw.items()}
            return self._tasks_cache

    def all_issues(self) -> list[Task]:
        return list(self._tasks().values())

    def get_task(self, ref: TaskRef) -> Task:
        hit = self._tasks().get(ref.key)
        if hit is not None:
            return hit
        return self.get_task_fresh(ref)

    def get_task_fresh(self, ref: TaskRef) -> Task:
        """One issue read straight from Jira (``GET /issue``, which is consistent), bypassing search."""
        try:
            raw = self.client.get_issue(ref.key, SEARCH_FIELDS)
        except JiraError as e:
            if e.status == 404:
                raise KeyError(f"no such issue {ref.key}") from e
            raise
        with self._lock:
            if self._raw is not None and ref.key in self._raw:
                self._raw[ref.key] = raw
                self._tasks_cache = None
        return self._to_task(raw, {k for k, t in (self._tasks_cache or {}).items() if t.is_epic})

    def dependencies(self, ref: TaskRef) -> list[TaskRef]:
        return self.get_task(ref).dependencies

    def epic_children(self, ref: TaskRef) -> list[TaskRef]:
        return [t.ref for t in self.all_tasks() if t.epic and t.epic.key == ref.key]

    # -- overlay ---------------------------------------------------------------------------------------------
    def _overlay(self, key: str, fn) -> None:
        """Apply a write to the cached raw issue in place and drop the derived tasks (not the search)."""
        with self._lock:
            if self._raw is not None and key in self._raw:
                fn(self._raw[key].setdefault("fields", {}))
                self._tasks_cache = None

    def _swap_family(self, ref: TaskRef, prefix: str, values: tuple[str, ...], value: str) -> None:
        """One request: add the new label and remove every other value of the family, whatever the snapshot
        believes, so a label a human just set cannot survive beside the new one."""
        new = self.codec.encode(prefix + value)
        ops = [{"add": new}] + [{"remove": self.codec.encode(prefix + v)} for v in values if v != value]
        self.client.update_issue(ref.key, {"update": {"labels": ops}})
        gone = {self.codec.encode(prefix + v) for v in values if v != value}

        def apply(fields):
            fields["labels"] = [x for x in fields.get("labels") or [] if x not in gone and x != new] + [new]
        self._overlay(ref.key, apply)
        self._note_written(self._id_of(ref.key))

    def set_status(self, ref: TaskRef, status: str) -> None:
        if status not in SWARM_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self._swap_family(ref, STATUS_PREFIX, SWARM_STATUSES, status)
        if status == "done":
            self._close(ref)
        elif status in self.transitions:
            self._move_to(ref, self.transitions[status])

    def set_autonomy(self, ref: TaskRef, tier: str) -> None:
        if tier not in AUTONOMY_TIERS:
            raise ValueError(f"unknown autonomy tier {tier!r}")
        self._swap_family(ref, AUTONOMY_PREFIX, AUTONOMY_TIERS, tier)

    # -- workflow ----------------------------------------------------------------------------------------------
    def _transition(self, key: str, chosen: dict) -> None:
        try:
            self.client.transition(key, chosen["id"])
        except JiraError as e:
            if e.status == 400:
                raise JiraError(e.status, [f"transition '{chosen.get('name')}' on {key} needs more than the swarm "
                                           f"can fill in (a transition screen with required fields?)", *e.messages],
                                path=e.path) from e
            raise

    def _close(self, ref: TaskRef) -> None:
        """Idempotent: already in a done-category status (often a human dragged the card) is success."""
        issue = self.client.get_issue(ref.key, ["status"])
        if self._category(issue) == "done":
            self._overlay(ref.key, lambda f: f.update(status={"statusCategory": {"key": "done"}}))
            return
        options = self.client.get_transitions(ref.key)
        wanted = self.transitions.get("done")
        if wanted:
            cands = [t for t in options if wanted.lower() in (str(t.get("name", "")).lower(),
                                                              str((t.get("to") or {}).get("name", "")).lower())]
        else:
            cands = [t for t in options if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done"]
        if len(cands) != 1:
            names = ", ".join(f"'{t.get('name')}'" for t in (cands or options)) or "none"
            what = "more than one transition reaches Done" if len(cands) > 1 else "no transition reaches Done"
            raise JiraError(0, f"cannot close {ref.key}: {what} (available: {names}); "
                               f"set jira.transitions.done in backend.yaml to the one to use", path=ref.key)
        self._transition(ref.key, cands[0])
        self._overlay(ref.key, lambda f: f.update(status={"statusCategory": {"key": "done"}}))
        self._note_written(issue.get("id") or self._id_of(ref.key))

    def _move_to(self, ref: TaskRef, name: str) -> None:
        issue = self.client.get_issue(ref.key, ["status"])
        if str((issue.get("fields", {}).get("status") or {}).get("name", "")).lower() == name.lower():
            return
        options = self.client.get_transitions(ref.key)
        cands = [t for t in options if name.lower() in (str(t.get("name", "")).lower(),
                                                        str((t.get("to") or {}).get("name", "")).lower())]
        if not cands:
            names = ", ".join(f"'{t.get('name')}'" for t in options) or "none"
            raise JiraError(0, f"cannot move {ref.key} to '{name}': no such transition is available "
                               f"(available: {names})", path=ref.key)
        self._transition(ref.key, cands[0])
        self._note_written(issue.get("id") or self._id_of(ref.key))

    @staticmethod
    def _category(issue: dict) -> str | None:
        return (((issue.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key")

    # -- comments ------------------------------------------------------------------------------------------------
    def post_comment(self, ref: TaskRef, text: str) -> str | None:
        """Posts ``text``; the original markdown rides along as a comment property so it reads back byte for
        byte. Text over the size limit is split into numbered continuation comments (marker on the first)."""
        pieces = chunk_markdown(text)
        first_url = None
        for n, piece in enumerate(pieces, 1):
            body = piece if n == 1 else f"(continued {n}/{len(pieces)})\n\n{piece}"
            if n == 1 and len(pieces) > 1:
                body = f"{piece}\n\n(continues 1/{len(pieces)})"
            url = self._add_comment(ref, body)
            first_url = first_url or url
        return first_url

    def _add_comment(self, ref: TaskRef, markdown: str) -> str | None:
        adf_body = adf.to_adf(markdown)
        props = [{"key": COMMENT_PROPERTY, "value": {"markdown": markdown}}]
        try:
            resp = self.client.add_comment(ref.key, adf_body, props if self._props_ok else None)
        except JiraError as e:
            if self._props_ok and e.status == 400 and "propert" in " ".join(e.messages).lower():
                log.warning("Jira refused comment properties (%s); comments will be read back from ADF", e)
                self._props_ok = False
                resp = self.client.add_comment(ref.key, adf_body, None)
            else:
                raise
        self._note_written(self._id_of(ref.key))
        cid = (resp or {}).get("id")
        return f"{self.base_url}/browse/{ref.key}?focusedCommentId={cid}" if cid else f"{self.base_url}/browse/{ref.key}"

    def list_comments(self, ref: TaskRef) -> list[Comment]:
        out = []
        for c in self.client.list_comments(ref.key):
            stored = next((p.get("value") for p in c.get("properties") or [] if p.get("key") == COMMENT_PROPERTY),
                          None)
            body = stored.get("markdown") if isinstance(stored, dict) and "markdown" in stored \
                else adf.to_markdown(c.get("body"))
            cid = c.get("id")
            out.append(Comment((c.get("author") or {}).get("accountId"), body, utc_iso(c.get("created")),
                               f"{self.base_url}/browse/{ref.key}?focusedCommentId={cid}" if cid else None))
        return out

    # -- one-time setup: Jira has no label registry, so there is nothing to create -----------------------------
    def required_labels(self, code_repos: list[str]) -> list[tuple[str, str]]:
        return []

    def init_commands(self, code_repos: list[str]) -> list[list[str]]:
        return []

    def _swarm_label_names(self, code_repos: list[str]) -> set[str]:
        names = {STATUS_PREFIX + s for s in SWARM_STATUSES} | {AUTONOMY_PREFIX + t for t in AUTONOMY_TIERS}
        return names | {REPO_PREFIX + r for r in [*self.code_repos, *code_repos]}

    def existing_labels(self) -> set[str]:
        """Labels Jira lists plus every one the swarm itself would create, so no caller tries to 'create' one."""
        listed = {self.codec.decode(x) for x in self.client.list_labels()}
        return listed | self._swarm_label_names([])

    def create_label(self, name: str, color: str) -> None:
        """A Jira label exists once an issue carries it; nothing to do."""

    # -- plan seeding (``backend seed`` / ``backend file``) ----------------------------------------------------------
    def seed_labels(self, *, epic: bool, status: str | None, autonomy: str | None,
                    repo: str | None) -> list[str]:
        labels = []
        if status:
            labels.append(STATUS_PREFIX + status)
        if autonomy:
            labels.append(AUTONOMY_PREFIX + autonomy)
        if repo:
            labels.append(REPO_PREFIX + repo)
        return labels

    def create_issue(self, title: str, body: str, labels: list[str], *, epic: bool = False) -> TaskRef:
        fields = {"project": {"key": self.project_key}, "summary": title, "description": adf.to_adf(body),
                  "issuetype": {"name": self.epic_type if epic else self.task_type},
                  "labels": [self.codec.encode(x) for x in labels]}
        made = self.client.create_issue(fields)
        key = made["key"]
        self._note_written(made.get("id"))
        with self._lock:
            if self._raw is not None:                         # visible to the next read before search catches up
                try:
                    self._raw[key] = self.client.get_issue(key, SEARCH_FIELDS)
                    self._tasks_cache = None
                except JiraError as e:
                    log.warning("could not read back %s: %s", key, e)
        return TaskRef(key)

    def set_parent(self, ref: TaskRef, parent: TaskRef) -> None:
        self.client.update_issue(ref.key, {"fields": {"parent": {"key": parent.key}}})
        self._overlay(ref.key, lambda f: f.update(parent={"key": parent.key}))
        self._note_written(self._id_of(ref.key))

    def add_dependency(self, ref: TaskRef, blocker: TaskRef) -> None:
        """``ref`` is blocked by ``blocker``. In the create payload ``inwardIssue`` is the issue that IS
        blocked ('is blocked by') and ``outwardIssue`` the blocker ('blocks'); on a READ the entry on the
        blocked issue carries ``inwardIssue`` = the blocker. Easy to get backwards: both ends are tested."""
        have = self.client.get_issue(ref.key, ["issuelinks"])
        for link in (have.get("fields") or {}).get("issuelinks") or []:
            if ((link.get("type") or {}).get("name") == self.blocks_link_type
                    and (link.get("inwardIssue") or {}).get("key") == blocker.key):
                return                                         # already linked: re-posting could duplicate it
        self.client.create_issue_link(self.blocks_link_type, ref.key, blocker.key)
        entry = {"type": {"name": self.blocks_link_type}, "inwardIssue": {"key": blocker.key}}
        self._overlay(ref.key, lambda f: f.setdefault("issuelinks", []).append(entry))
        self._note_written(self._id_of(ref.key))
        self._note_written(self._id_of(blocker.key))
