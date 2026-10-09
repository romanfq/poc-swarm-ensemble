"""In-memory stand-in for GitHub (via the gh CLI).

It is seeded from one neutral plan description so the backend contract
suite can run unchanged against every adapter (the Jira adapter runs against FakeJira, below).
"""
from __future__ import annotations

import json

# name -> fields. epic: bool, parent: name, deps: [names], status, autonomy, repo, closed
# Tasks carry a status label, as `backend seed` gives them (epics get none).
PLAN = {
    "E1": {"title": "Ingestion", "epic": True},
    "E2": {"title": "Frontend", "epic": True, "status": "blocked"},
    "T0": {"title": "Schema", "parent": "E1", "closed": True, "status": "done"},
    "T1": {"title": "Poll feed", "parent": "E1", "autonomy": "self-approve", "repo": "OWNER/app",
           "body": "Poll the feed every minute.", "status": "ready"},
    "T2": {"title": "Parse feed", "parent": "E1", "deps": ["T1"], "status": "ready"},
    "T3": {"title": "Blocked one", "parent": "E1", "status": "blocked"},
    "T4": {"title": "Page", "parent": "E2", "status": "ready"},
    "T5": {"title": "Loose task", "deps": ["T0"], "status": "ready"},
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
        for i, name in enumerate(plan, start=1):
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
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": x} for x in sorted(self.labels)])
        if args[:2] == ["issue", "create"]:
            for lb in opts["--label"]:
                if lb not in self.labels:
                    return ("", 1, f"could not add label: '{lb}' not found")
            n = max(self.issues, default=0) + 1
            self.issues[n] = {
                "number": n, "title": opts["--title"][0], "body": input or "",
                "url": f"https://github.com/{self.repo}/issues/{n}", "state": "OPEN",
                "labels": list(opts["--label"]),
                "issueType": {"name": opts["--type"][0]} if opts["--type"] else None,
                "parent": None, "blockedBy": [], "comments": [],
            }
            return self.issues[n]["url"] + "\n"
        if args[:2] == ["issue", "edit"]:
            i = self.issues[int(args[2])]
            for p in opts["--parent"]:
                if int(p) not in self.issues or int(p) == int(args[2]):
                    return ("", 1, f"invalid parent {p}")
                i["parent"] = int(p)
            for b in opts["--add-blocked-by"]:
                if int(b) not in self.issues:
                    return ("", 1, f"no issue {b}")
                if int(b) not in i["blockedBy"]:
                    i["blockedBy"].append(int(b))
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
            comments = self.issues[int(args[2])]["comments"]
            comments.append(input)
            return f"https://github.com/{args[args.index('--repo') + 1]}/issues/{args[2]}#issuecomment-{len(comments)}\n"
        if args[:2] == ["issue", "view"] and "comments" in args:
            n = int(args[2])
            repo = args[args.index("--repo") + 1]
            posted = [{"author": {"login": (c.get("author") if isinstance(c, dict) else "dags-bot")},
                       "body": c.get("body") if isinstance(c, dict) else c,
                       "createdAt": f"t{i:04d}",
                       "url": f"https://github.com/{repo}/issues/{n}#issuecomment-{i + 1}"}
                      for i, c in enumerate(self.issues[n]["comments"])]
            return json.dumps({"comments": posted})
        if args[:2] == ["label", "create"]:
            self.labels.add(args[2])
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    @staticmethod
    def _opts(args):
        multi = ("--add-label", "--remove-label", "--label", "--title", "--type", "--parent",
                 "--add-blocked-by")
        out = {"-f": {}, "-F": {}, **{m: [] for m in multi}}
        it = iter(range(len(args)))
        for i in it:
            a = args[i]
            if a in ("-f", "-F") and i + 1 < len(args):
                k, _, v = args[i + 1].partition("=")
                out[a][k] = v
                next(it, None)
            elif a in multi and i + 1 < len(args):
                out[a].append(args[i + 1])
                next(it, None)
        return out

    def comments(self, key):
        return [c["body"] if isinstance(c, dict) else c for c in self.issues[int(key.split("#")[1])]["comments"]]

    def reply(self, key, author, text):
        """A person's comment on the issue."""
        self.issues[int(key.split("#")[1])]["comments"].append({"author": author, "body": text})


# ---------------------------------------------------------------------------
# GitHub pull requests (code repos)
# ---------------------------------------------------------------------------

class FakePRs:
    """Answers the gh pr commands used by `swarm-task done`, the poller and the Board."""

    def __init__(self):
        self.prs: dict[tuple[str, int], dict] = {}
        self.calls: list[dict] = []
        self.next = 1

    def add(self, repo, branch, state="OPEN", files=(), reviews=(), checks=()):
        n = self.next
        self.next += 1
        self.prs[(repo, n)] = {"number": n, "url": f"https://github.com/{repo}/pull/{n}", "state": state,
                               "headRefName": branch, "files": list(files), "reviews": list(reviews),
                               "comments": [], "statusCheckRollup": list(checks), "reviewDecision": None,
                               "title": branch, "body": ""}
        return self.prs[(repo, n)]

    def get(self, url):
        repo, n = url.split("github.com/")[1].split("/pull/")
        return self.prs[(repo, int(n))]

    @staticmethod
    def _opt(args, name, default=None):
        return args[args.index(name) + 1] if name in args else default

    def __call__(self, args, env, input):
        self.calls.append({"args": args, "token": env.get("GH_TOKEN"), "input": input})
        if args[0] != "pr":
            raise AssertionError(f"unexpected gh call {args}")
        repo = self._opt(args, "--repo")
        verb = args[1]
        if verb == "list":
            head = self._opt(args, "--head")
            rows = [{"number": p["number"], "url": p["url"], "state": p["state"]}
                    for (r, _), p in self.prs.items() if r == repo and p["headRefName"] == head]
            return json.dumps(rows)
        if verb == "create":
            branch = self._opt(args, "--head")
            body = open(self._opt(args, "--body-file")).read()
            pr = self.add(repo, branch)
            pr["title"] = self._opt(args, "--title")
            pr["body"] = body
            pr["base"] = self._opt(args, "--base")
            return pr["url"] + "\n"
        pr = self.prs[(repo, int(args[2]))]
        if verb == "view":
            fields = self._opt(args, "--json", "").split(",")
            return json.dumps({k: v for k, v in pr.items() if k in fields})
        if verb == "comment":
            pr["comments"].append({"author": {"login": "bot"}, "body": input, "createdAt": "t"})
            return ""
        if verb == "diff":
            return "\n".join(pr["files"]) + "\n"
        if verb == "review":
            pr["reviewDecision"] = "APPROVED"
            return ""
        if verb == "merge":
            pr["state"] = "MERGED"
            return ""
        raise AssertionError(f"unexpected gh call {args}")


# ---------------------------------------------------------------------------
# Jira (implements the JiraClient interface directly: no HTTP)
# ---------------------------------------------------------------------------

class FakeJira:
    """In-memory Jira project in Jira's documented JSON shapes. ``lag=True`` models eventual consistency:
    search serves an index that only catches up when ``settle()`` is called, except for issue ids passed
    as ``reconcile_issues``. Direct reads (``get_issue``) always see the latest write."""

    STATUSES = {"To Do": "new", "In Progress": "indeterminate", "Done": "done"}

    def __init__(self, project="KAN", page_size=3, lag=False):
        import copy
        self._copy = copy
        self.project, self.page_size, self.lag = project, page_size, lag
        self.issues: dict[str, dict] = {}
        self.index: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.names: dict[str, str] = {}
        self.seq = 0
        self.tick = 0
        self.properties_ok = True
        self.extra_transitions: list[dict] = []
        self.closed_by_human: set[str] = set()

    # -- seeding
    def issue_type(self, epic: bool, subtask=False):
        if subtask:
            return {"name": "Subtask", "hierarchyLevel": -1, "subtask": True}
        return {"name": "Epic", "hierarchyLevel": 1} if epic else {"name": "Task", "hierarchyLevel": 0}

    def add(self, name, title, *, epic=False, labels=(), parent=None, closed=False, body="", deps=(),
            subtask=False, index=True):
        self.seq += 1
        key = f"{self.project}-{self.seq}"
        self.names[name] = key
        self.issues[key] = {"id": str(10000 + self.seq), "key": key, "fields": {
            "summary": title, "description": None if not body else
            {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]},
            "status": {"name": "Done" if closed else "To Do",
                       "statusCategory": {"key": "done" if closed else "new"}},
            "labels": list(labels), "issuetype": self.issue_type(epic, subtask),
            "parent": {"key": self.names[parent]} if parent else None,
            "issuelinks": []}}
        for d in deps:
            self._link("Blocks", key, self.names[d])
        self.comments[key] = []
        if index:
            self.settle()
        return key

    def settle(self):
        self.index = self._copy.deepcopy(self.issues)

    def _link(self, type_name, blocked, blocker):
        self.issues[blocked]["fields"]["issuelinks"].append(
            {"type": {"name": type_name}, "inwardIssue": {"key": blocker}})
        self.issues[blocker]["fields"]["issuelinks"].append(
            {"type": {"name": type_name}, "outwardIssue": {"key": blocked}})

    def human_comment(self, key, author, adf_body):
        self.tick += 1
        self.comments[key].append({"id": str(900 + self.tick), "author": {"accountId": author}, "body": adf_body,
                                   "created": f"2026-10-09T10:{self.tick:02d}:00.000+0100", "properties": []})

    # -- the JiraClient interface
    def myself(self):
        self.calls.append(("myself",))
        return {"accountId": "bot-account", "displayName": "dags-bot"}

    def _view(self, issue, fields):
        out = self._copy.deepcopy(issue)
        if fields:
            out["fields"] = {k: v for k, v in out["fields"].items() if k in fields}
        return out

    def search(self, jql, fields, *, next_page_token=None, max_results=100, reconcile_issues=None):
        self.calls.append(("search", next_page_token, tuple(reconcile_issues or ())))
        src = self.index if self.lag else self.issues
        pool = dict(src)
        for i in reconcile_issues or ():
            for k, v in self.issues.items():
                if v["id"] == str(i):
                    pool[k] = v
        rows = sorted(pool.values(), key=lambda i: int(i["id"]))
        start = int(next_page_token or 0)
        page = rows[start:start + self.page_size]
        last = start + self.page_size >= len(rows)
        return {"issues": [self._view(i, fields) for i in page], "isLast": last,
                **({} if last else {"nextPageToken": str(start + self.page_size)})}

    def _get(self, key):
        from dags.jira_client import JiraError
        if key not in self.issues:
            raise JiraError(404, "Issue does not exist or you do not have permission to see it.", path=key)
        return self.issues[key]

    def get_issue(self, key, fields=None):
        self.calls.append(("get_issue", key))
        return self._view(self._get(key), fields)

    def create_issue(self, fields):
        self.calls.append(("create_issue", fields["summary"]))
        epic = fields["issuetype"]["name"] == "Epic"
        key = self.add(f"_new{self.seq + 1}", fields["summary"], epic=epic, labels=fields.get("labels") or (),
                       body="", index=False)
        self.issues[key]["fields"]["description"] = fields.get("description")
        return {"id": self.issues[key]["id"], "key": key}

    def _write(self, key):
        # the index is deliberately NOT updated: under lag it catches up only on settle()
        return self._get(key)

    def update_issue(self, key, body):
        self.calls.append(("update_issue", key, body))
        issue = self._write(key)
        f = issue["fields"]
        for op in (body.get("update") or {}).get("labels") or []:
            if "add" in op and op["add"] not in f["labels"]:
                f["labels"].append(op["add"])
            if "remove" in op and op["remove"] in f["labels"]:
                f["labels"].remove(op["remove"])          # removing a label that is not there is a no-op
        if "parent" in (body.get("fields") or {}):
            f["parent"] = {"key": body["fields"]["parent"]["key"]}

    def get_transitions(self, key):
        self._get(key)
        return [{"id": "11", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                {"id": "31", "name": "Finish", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
                *self.extra_transitions]

    def transition(self, key, transition_id):
        self.calls.append(("transition", key, transition_id))
        names = {"11": "In Progress", "31": "Done", **{t["id"]: t["to"]["name"] for t in self.extra_transitions}}
        name = names[str(transition_id)]
        cat = self.STATUSES.get(name, "done" if name == "Done" else "indeterminate")
        self._write(key)["fields"]["status"] = {"name": name, "statusCategory": {"key": cat}}

    def create_issue_link(self, type_name, inward_key, outward_key):
        self.calls.append(("link", type_name, inward_key, outward_key))
        self._get(inward_key), self._get(outward_key)
        self._link(type_name, inward_key, outward_key)       # inward is the blocked issue

    def add_comment(self, key, adf_body, properties=None):
        self.calls.append(("add_comment", key))
        from dags.jira_client import JiraError
        self._get(key)
        if properties and not self.properties_ok:
            raise JiraError(400, "comment properties are not supported")
        self.tick += 1
        c = {"id": str(900 + self.tick), "author": {"accountId": "bot-account"}, "body": adf_body,
             "created": f"2026-10-09T10:{self.tick:02d}:00.000+0100", "properties": properties or []}
        self.comments[key].append(c)
        return {"id": c["id"]}

    def list_comments(self, key):
        self._get(key)
        return self._copy.deepcopy(self.comments[key])

    def list_labels(self):
        return sorted({lb for i in self.issues.values() for lb in i["fields"]["labels"]})

    def list_issue_types(self, project_key):
        return [self.issue_type(True), self.issue_type(False), self.issue_type(False, subtask=True)]
