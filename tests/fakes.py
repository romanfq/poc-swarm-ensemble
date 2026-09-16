"""In-memory stand-in for GitHub (via the gh CLI).

It is seeded from one neutral plan description so the backend contract
suite can run unchanged against every adapter (the Jira adapter is future work,
see FutureWork.md).
"""
from __future__ import annotations

import json

# name -> fields. epic: bool, parent: name, deps: [names], status, autonomy, repo, closed
PLAN = {
    "E1": {"title": "Ingestion", "epic": True},
    "E2": {"title": "Frontend", "epic": True, "status": "blocked"},
    "T0": {"title": "Schema", "parent": "E1", "closed": True, "status": "done"},
    "T1": {"title": "Poll feed", "parent": "E1", "autonomy": "auto-pr", "repo": "OWNER/app",
           "body": "Poll the feed every minute."},
    "T2": {"title": "Parse feed", "parent": "E1", "deps": ["T1"]},
    "T3": {"title": "Blocked one", "parent": "E1", "status": "blocked"},
    "T4": {"title": "Page", "parent": "E2"},
    "T5": {"title": "Loose task", "deps": ["T0"]},
}


def plan_labels(spec: dict, issue_types: bool = False) -> list[str]:
    labels = []
    if spec.get("status"):
        labels.append(f"swarm:status:{spec['status']}")
    if spec.get("autonomy"):
        labels.append(f"swarm:autonomy:{spec['autonomy']}")
    if spec.get("repo"):
        labels.append(f"repo:{spec['repo']}")
    if not issue_types:
        labels.append("type:epic" if spec.get("epic") else "type:task")
    return labels


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class FakeGitHub:
    """Answers the subset of gh commands the GitHub adapter uses."""

    def __init__(self, repo="acme/plan", page_size=3):
        self.repo = repo
        self.page_size = page_size
        self.issues: dict[int, dict] = {}
        self.labels: set[str] = set()
        self.calls: list[list[str]] = []
        self.names: dict[str, int] = {}

    def seed(self, plan=PLAN, issue_types=False):
        for i, (name, spec) in enumerate(plan.items(), start=1):
            self.names[name] = i
        for name, spec in plan.items():
            n = self.names[name]
            self.issues[n] = {
                "number": n, "title": spec["title"], "body": spec.get("body", ""),
                "url": f"https://github.com/{self.repo}/issues/{n}",
                "state": "CLOSED" if spec.get("closed") else "OPEN",
                "labels": plan_labels(spec, issue_types),
                "issueType": ({"name": "Epic" if spec.get("epic") else "Task"} if issue_types else None),
                "parent": self.names.get(spec.get("parent")),
                "blockedBy": [self.names[d] for d in spec.get("deps", [])],
                "comments": [],
            }
            self.labels.update(self.issues[n]["labels"])
        return {name: f"{self.repo}#{n}" for name, n in self.names.items()}

    # -- node rendering ---------------------------------------------------------
    def _mini(self, n):
        return {"number": n, "state": self.issues[n]["state"], "repository": {"nameWithOwner": self.repo}}

    def node(self, n):
        i = self.issues[n]
        return {
            "number": n, "title": i["title"], "body": i["body"], "url": i["url"], "state": i["state"],
            "labels": {"nodes": [{"name": x} for x in i["labels"]]},
            "issueType": i["issueType"],
            "parent": self._mini(i["parent"]) if i["parent"] else None,
            "subIssues": {"nodes": [self._mini(k) for k, v in self.issues.items() if v["parent"] == n]},
            "blockedBy": {"nodes": [self._mini(k) for k in i["blockedBy"]]},
        }

    # -- runner --------------------------------------------------------------------
    def __call__(self, args, env, input):
        self.calls.append(args)
        opts = self._opts(args)
        if args[:2] == ["api", "graphql"]:
            q = opts["-f"].get("query", "")
            if "issue(number" in q:
                n = int(opts["-F"]["number"])
                return json.dumps({"data": {"repository": {"issue": self.node(n) if n in self.issues else None}}})
            nums = sorted(self.issues)
            start = int(opts["-f"].get("cursor", "0") or 0)
            page = nums[start:start + self.page_size]
            more = start + self.page_size < len(nums)
            return json.dumps({"data": {"repository": {"issues": {
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page_size)},
                "nodes": [self.node(n) for n in page]}}}})
        if args[:2] == ["issue", "edit"]:
            i = self.issues[int(args[2])]
            for lb in opts["--add-label"]:
                if lb not in self.labels:
                    return ("", 1, f"failed to update: '{lb}' not found")
                if lb not in i["labels"]:
                    i["labels"].append(lb)
            for lb in opts["--remove-label"]:
                if lb in i["labels"]:
                    i["labels"].remove(lb)
            return ""
        if args[:2] == ["issue", "close"]:
            self.issues[int(args[2])]["state"] = "CLOSED"
            return ""
        if args[:2] == ["issue", "comment"]:
            self.issues[int(args[2])]["comments"].append(input)
            return ""
        if args[:2] == ["label", "create"]:
            self.labels.add(args[2])
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    @staticmethod
    def _opts(args):
        out = {"-f": {}, "-F": {}, "--add-label": [], "--remove-label": []}
        it = iter(range(len(args)))
        for i in it:
            a = args[i]
            if a in ("-f", "-F") and i + 1 < len(args):
                k, _, v = args[i + 1].partition("=")
                out[a][k] = v
                next(it, None)
            elif a in ("--add-label", "--remove-label") and i + 1 < len(args):
                out[a].append(args[i + 1])
                next(it, None)
        return out

    def comments(self, key):
        return self.issues[int(key.split("#")[1])]["comments"]
