"""Opt-in live smoke test for the Jira adapter (GH-123). Run by a human, never in CI:

    DAGS_JIRA_LIVE=1 DAGS_JIRA_ALLOWED_BASE_URLS=https://dags-swarm.atlassian.net \\
    JIRA_BASE_URL=https://dags-swarm.atlassian.net JIRA_PROJECT_KEY=KAN \\
    JIRA_EMAIL=... JIRA_API_TOKEN=... python3 tests/live_jira_smoke.py

It refuses any base URL not in the allow-list (an environment variable or ``jira.live_allowed_base_urls``
in .swarm/local.yaml, never a committed file). It creates its own issues, labelled ``dags-smoke`` and with
no ``swarm:`` labels or seed markers (so they never join a plan), exercises each port method, and cleans up
by moving them to Done. It never deletes.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from backends.base import TaskRef  # noqa: E402
from backends.jira import JiraBackend  # noqa: E402
from dags import records as R  # noqa: E402
from dags.jira_client import build_client  # noqa: E402

SMOKE = "dags-smoke"


def allowed() -> set[str]:
    env = {u.strip().rstrip("/") for u in os.environ.get("DAGS_JIRA_ALLOWED_BASE_URLS", "").split(",") if u.strip()}
    local = R.load_yaml(Path(".swarm") / "local.yaml").get("jira") or {}
    return env | {str(u).rstrip("/") for u in local.get("live_allowed_base_urls") or []}


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        raise SystemExit(1)


def main() -> int:
    if os.environ.get("DAGS_JIRA_LIVE") != "1":
        print("set DAGS_JIRA_LIVE=1 to run against a real Jira site")
        return 2
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    key = os.environ.get("JIRA_PROJECT_KEY", "")
    if not base or not key:
        print("set JIRA_BASE_URL and JIRA_PROJECT_KEY")
        return 2
    if base not in allowed():
        print(f"refusing to run against {base}: not in DAGS_JIRA_ALLOWED_BASE_URLS / .swarm/local.yaml")
        return 2
    cfg = {"base_url": base, "project_key": key}
    client = build_client(cfg, {"jira": R.load_yaml(Path(".swarm") / "local.yaml").get("jira") or {}})
    b = JiraBackend(client, key, base, cache_seconds=0, recent_path=Path(".swarm") / "jira-recent-smoke.json")
    made: list[TaskRef] = []
    tag = uuid.uuid4().hex[:6]
    try:
        me = b.whoami()
        check(bool(me.get("accountId")), f"credentials work (accountId {me.get('accountId')})")
        types = client.list_issue_types(key)
        check(any(t.get("hierarchyLevel", 0) >= 1 for t in types), "project has an issue type with hierarchyLevel >= 1")
        epic = b.create_issue(f"smoke epic {tag}", "smoke", [SMOKE], epic=True)
        t1 = b.create_issue(f"smoke task A {tag}", "smoke", [SMOKE, "repo:org/app"])
        t2 = b.create_issue(f"smoke task B {tag}", "smoke", [SMOKE])
        made += [epic, t1, t2]
        check(b.get_task_fresh(epic).is_epic, "epic is detected from its issue type")
        b.set_parent(t1, epic)
        b.add_dependency(t2, t1)
        b.add_dependency(t2, t1)
        check(b.get_task_fresh(t1).epic == epic, "parent set")
        check(b.get_task_fresh(t2).dependencies == [t1], "Blocks link reads back as t2 blocked by t1")
        b.set_status(t1, "ready")
        b.set_status(t1, "claimed")
        got = b.get_task_fresh(t1)
        check([x for x in got.labels if x.startswith("swarm:status:")] == ["swarm:status:claimed"],
              "':' and '/' labels accepted; one status label after a swap")
        check(got.repo == "org/app", "repo label round trip")
        text = "<!-- dags-plan: smoke -->\n## Plan\nline one\nline two"
        url = b.post_comment(t1, text)
        c = b.list_comments(t1)[-1]
        check(c.body == text and bool(url), "comment properties accepted; markdown round trips exactly")
        check(c.created_at.endswith("Z"), "comment timestamps are UTC")
        check("repo:org/app" in b.existing_labels(), "existing_labels covers swarm labels")
        b.set_status(t1, "done")
        b.set_status(t1, "done")
        check(b.get_task_fresh(t1).closed, "done closes the issue, twice without error")
    finally:
        for ref in made:                                   # clean up by closing, never by deleting
            try:
                if not b.get_task_fresh(ref).closed:
                    b._close(ref)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup of {ref.key} failed: {e}")
    print("smoke passed; issues closed:", ", ".join(r.key for r in made))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
