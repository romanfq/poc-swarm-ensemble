"""GitHub Issues adapter (whitepaper Ch.3.3).

Reads use one paginated GraphQL query through ``gh api graphql`` so that
sub-issues (parent / subIssues), dependencies (blockedBy) and issue types all
arrive in a single call, independent of which ``--json`` fields a given gh
release exposes on ``gh issue list``. Writes use plain ``gh issue`` commands.

Task references are ``OWNER/REPO#NUMBER``; ``coordination_ref`` is a pass-through.

backend.yaml:
    backend: github
    github:
      repo: OWNER/matchwire-swarm
      use_issue_types: false      # true: Epic/Task come from GitHub issue types (org repos)
      cache_seconds: 20
    plan_scope: labelled          # top level; see backends.base.split_plan
"""
from __future__ import annotations

import re
import threading
import time

from backends.base import (AUTONOMY_PREFIX, AUTONOMY_TIERS, DEFAULT_AUTONOMY, DEFAULT_PLAN_SCOPE,
                           REPO_PREFIX, STATUS_PREFIX, SWARM_STATUSES, PlanScoped, Task, TaskRef,
                           parse_labels, plan_scope_of)
from dags import gh

_ISSUE_FIELDS = """
  number title body url state
  labels(first: 50) { nodes { name } }
  issueType { name }
  parent { number repository { nameWithOwner } }
  subIssues(first: 100) { nodes { number repository { nameWithOwner } } }
  blockedBy(first: 50) { nodes { number state repository { nameWithOwner } } }
"""

LIST_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED],
           orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}
""" % _ISSUE_FIELDS

ONE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { %s }
  }
}
""" % _ISSUE_FIELDS

REF_RE = re.compile(r"^(?P<repo>[^/#\s]+/[^/#\s]+)#(?P<num>\d+)$")

LABEL_COLORS = {
    "swarm:status:": "0e8a16",
    "swarm:autonomy:": "5319e7",
    "type:": "c5def5",
    "repo:": "fbca04",
}


class GitHubBackend(PlanScoped):
    name = "github"

    def __init__(self, repo: str, use_issue_types: bool = False, cache_seconds: float = 20,
                 extra_repos: list[str] | None = None, plan_scope: str = DEFAULT_PLAN_SCOPE):
        if not repo or "/" not in repo or repo.startswith("OWNER/"):
            raise ValueError(f"backend.yaml github.repo must be OWNER/NAME (got {repo!r})")
        self.repo = repo
        self.use_issue_types = use_issue_types
        self.cache_seconds = cache_seconds
        self.extra_repos = extra_repos or []
        self.plan_scope = plan_scope
        self._snap: dict[str, Task] | None = None
        self._snap_at = 0.0
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: dict, ctx) -> "GitHubBackend":
        return cls(str(cfg.get("repo", "")), bool(cfg.get("use_issue_types", False)),
                   float(cfg.get("cache_seconds", 20)), plan_scope=plan_scope_of(ctx.backend_cfg))

    # -- refs -----------------------------------------------------------------
    def ref(self, number: int | str, repo: str | None = None) -> TaskRef:
        return TaskRef(f"{repo or self.repo}#{int(number)}")

    def parse_ref(self, text: str) -> TaskRef:
        """Accepts 'OWNER/REPO#7', '#7', '7' or 'GH-7'."""
        text = str(text).strip()
        if REF_RE.match(text):
            return TaskRef(text)
        m = re.match(r"^(?:#|GH-)?(\d+)$", text, re.I)
        if m:
            return self.ref(m.group(1))
        raise ValueError(f"not a GitHub issue reference: {text!r}")

    @staticmethod
    def _split(ref: TaskRef) -> tuple[str, int]:
        m = REF_RE.match(ref.key)
        if not m:
            raise ValueError(f"not a GitHub issue reference: {ref.key!r}")
        return m.group("repo"), int(m.group("num"))

    def short_key(self, ref: TaskRef) -> str:
        repo, num = self._split(ref)
        return f"GH-{num}" if repo == self.repo else f"{repo.split('/')[1]}-{num}"

    # -- reading ----------------------------------------------------------------
    def _node_ref(self, node: dict) -> TaskRef:
        repo = (node.get("repository") or {}).get("nameWithOwner") or self.repo
        return self.ref(node["number"], repo)

    def _to_task(self, node: dict, repo: str) -> Task:
        labels = [n["name"] for n in (node.get("labels") or {}).get("nodes") or []]
        parsed = parse_labels(labels)
        itype = ((node.get("issueType") or {}).get("name") or "").lower()
        subs = (node.get("subIssues") or {}).get("nodes") or []
        if self.use_issue_types and itype:
            is_epic = itype == "epic"
        elif parsed["kind"]:
            is_epic = parsed["kind"] == "epic"
        else:
            is_epic = itype == "epic" or bool(subs)
        parent = node.get("parent")
        return Task(
            ref=self.ref(node["number"], repo),
            title=node.get("title") or "",
            body=node.get("body") or "",
            status=parsed["status"] if parsed["status"] in SWARM_STATUSES else None,
            autonomy=parsed["autonomy"] or DEFAULT_AUTONOMY,
            epic=self._node_ref(parent) if parent else None,
            dependencies=[self._node_ref(n) for n in (node.get("blockedBy") or {}).get("nodes") or []],
            repo=parsed["repo"],
            url=node.get("url"),
            is_epic=is_epic,
            closed=str(node.get("state", "")).upper() == "CLOSED",
            labels=labels,
        )

    def _graphql(self, query: str, **variables) -> dict:
        args = ["api", "graphql", "-f", f"query={query}"]
        for k, v in variables.items():
            if v is None:
                continue
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
        data = gh.gh_json(args) or {}
        if data.get("errors"):
            raise gh.GhError(args[:2], 1, "; ".join(e.get("message", "") for e in data["errors"]))
        return data.get("data") or {}

    def _fetch_all(self, repo: str) -> list[Task]:
        owner, name = repo.split("/", 1)
        out, cursor = [], None
        while True:
            data = self._graphql(LIST_QUERY, owner=owner, name=name, cursor=cursor)
            issues = ((data.get("repository") or {}).get("issues")) or {}
            out += [self._to_task(n, repo) for n in issues.get("nodes") or [] if n]
            page = issues.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return out
            cursor = page.get("endCursor")

    def _snapshot(self, fresh: bool = False) -> dict[str, Task]:
        with self._lock:
            if fresh or self._snap is None or time.monotonic() - self._snap_at > self.cache_seconds:
                tasks = []
                for repo in [self.repo, *self.extra_repos]:
                    tasks += self._fetch_all(repo)
                self._snap = {t.ref.key: t for t in tasks}
                self._snap_at = time.monotonic()
            return self._snap

    def invalidate(self) -> None:
        with self._lock:
            self._snap = None

    def all_issues(self) -> list[Task]:
        return list(self._snapshot().values())

    def get_task(self, ref: TaskRef) -> Task:
        hit = self._snapshot().get(ref.key)
        if hit is not None:
            return hit
        repo, num = self._split(ref)            # e.g. a dependency in another repo
        owner, name = repo.split("/", 1)
        node = ((self._graphql(ONE_QUERY, owner=owner, name=name, number=num).get("repository") or {})
                .get("issue"))
        if not node:
            raise KeyError(f"no such issue {ref.key}")
        return self._to_task(node, repo)

    def dependencies(self, ref: TaskRef) -> list[TaskRef]:
        return self.get_task(ref).dependencies

    def epic_children(self, ref: TaskRef) -> list[TaskRef]:
        return [t.ref for t in self.all_tasks() if t.epic and t.epic.key == ref.key]

    def coordination_ref(self, ref: TaskRef) -> str:
        return ref.key

    def web_url(self, ref: TaskRef) -> str | None:
        try:
            return self.get_task(ref).url
        except (KeyError, gh.GhError):
            repo, num = self._split(ref)
            return f"https://github.com/{repo}/issues/{num}"

    # -- writing -------------------------------------------------------------------
    def _swap_label(self, ref: TaskRef, prefix: str, value: str) -> None:
        repo, num = self._split(ref)
        try:
            current = self.get_task(ref).labels
        except KeyError:
            current = []
        new = prefix + value
        args = ["issue", "edit", str(num), "--repo", repo]
        if new not in current:
            args += ["--add-label", new]
        stale = [lb for lb in current if lb.startswith(prefix) and lb != new]
        for lb in stale:
            args += ["--remove-label", lb]
        if len(args) > 5:
            try:
                gh.gh(args)
            except gh.GhError as e:
                if "not found" in str(e).lower():
                    raise gh.GhError(args, e.returncode,
                                     f"{e.stderr.strip()} — create the swarm labels first with "
                                     f"`swarm.py backend init`") from e
                raise
        self.invalidate()

    def set_status(self, ref: TaskRef, status: str) -> None:
        if status not in SWARM_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self._swap_label(ref, STATUS_PREFIX, status)
        if status == "done":
            repo, num = self._split(ref)
            if not self.get_task(ref).closed:
                gh.gh(["issue", "close", str(num), "--repo", repo, "--reason", "completed"])
                self.invalidate()

    @property
    def uses_type_labels(self) -> bool:
        return not self.use_issue_types

    def set_kind(self, ref: TaskRef, epic: bool) -> None:
        """Add type:epic / type:task (plan membership). Only adds: other
        `type:` labels people use (type:bug, ...) are left alone."""
        label = "type:epic" if epic else "type:task"
        try:
            current = self.get_task(ref).labels
        except KeyError:
            current = []
        if label in current:
            return
        repo, num = self._split(ref)
        gh.gh(["issue", "edit", str(num), "--repo", repo, "--add-label", label])
        self.invalidate()

    def set_autonomy(self, ref: TaskRef, tier: str) -> None:
        if tier not in AUTONOMY_TIERS:
            raise ValueError(f"unknown autonomy tier {tier!r}")
        self._swap_label(ref, AUTONOMY_PREFIX, tier)

    def post_comment(self, ref: TaskRef, text: str) -> None:
        repo, num = self._split(ref)
        gh.gh(["issue", "comment", str(num), "--repo", repo, "--body-file", "-"], input=text)

    # -- one-time setup, run by a human (`swarm.py backend init`) -----------------------
    def required_labels(self, code_repos: list[str]) -> list[tuple[str, str]]:
        names = [STATUS_PREFIX + s for s in SWARM_STATUSES]
        names += [AUTONOMY_PREFIX + t for t in AUTONOMY_TIERS]
        if not self.use_issue_types:
            names += ["type:epic", "type:task"]
        names += [REPO_PREFIX + r for r in code_repos]
        out = []
        for n in names:
            color = next(c for p, c in LABEL_COLORS.items() if n.startswith(p))
            out.append((n, color))
        return out

    def init_commands(self, code_repos: list[str]) -> list[list[str]]:
        return [self.label_command(name, color) for name, color in self.required_labels(code_repos)]

    def label_command(self, name: str, color: str) -> list[str]:
        return ["label", "create", name, "--repo", self.repo, "--color", color, "--force",
                "--description", "DAGS swarm label"]

    # -- plan seeding, run by a human (`swarm.py backend seed`) ---------------------
    def existing_labels(self) -> set[str]:
        rows = gh.gh_json(["label", "list", "--repo", self.repo, "--limit", "1000", "--json", "name"]) or []
        return {r["name"] for r in rows}

    def create_label(self, name: str, color: str) -> None:
        gh.gh(self.label_command(name, color))

    def seed_labels(self, *, epic: bool, status: str | None, autonomy: str | None,
                    repo: str | None) -> list[str]:
        labels = []
        if status:
            labels.append(STATUS_PREFIX + status)
        if autonomy:
            labels.append(AUTONOMY_PREFIX + autonomy)
        if repo:
            labels.append(REPO_PREFIX + repo)
        if not self.use_issue_types:
            labels.append("type:epic" if epic else "type:task")
        return labels

    def create_issue(self, title: str, body: str, labels: list[str], *, epic: bool = False) -> TaskRef:
        args = ["issue", "create", "--repo", self.repo, "--title", title, "--body-file", "-"]
        for lb in labels:
            args += ["--label", lb]
        if self.use_issue_types:
            args += ["--type", "Epic" if epic else "Task"]
        out = gh.gh(args, input=body)
        m = re.search(r"/issues/(\d+)", out or "")
        if not m:
            raise gh.GhError(args[:2], 1, f"could not read the new issue number from {out!r}")
        self.invalidate()
        return self.ref(m.group(1))

    def set_parent(self, ref: TaskRef, parent: TaskRef) -> None:
        repo, num = self._split(ref)
        _, pnum = self._split(parent)
        gh.gh(["issue", "edit", str(num), "--repo", repo, "--parent", str(pnum)])
        self.invalidate()

    def add_dependency(self, ref: TaskRef, blocker: TaskRef) -> None:
        repo, num = self._split(ref)
        _, bnum = self._split(blocker)
        gh.gh(["issue", "edit", str(num), "--repo", repo, "--add-blocked-by", str(bnum)])
        self.invalidate()
