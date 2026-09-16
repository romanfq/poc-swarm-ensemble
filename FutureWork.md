# Future work

## Jira adapter (plan Phase 8), deferred

**Status:** not built. `backend: jira` in `backend.yaml` fails at startup with a
message pointing here (`bin/backends/__init__.py`, `NOT_YET`).

### Why it's deferred

- **There's no Jira instance to build against.** The POC (Phase 9) runs on GitHub
  Issues only. An adapter written with no real tracker behind it could only be
  tested against a fake that I wrote myself. That fake would just repeat my
  assumptions about Jira, so the contract suite would pass without proving
  anything.
- **Jira Cloud's API keeps shifting in the areas this adapter needs:**
  - issue search moved from `/rest/api/3/search` to `/rest/api/3/search/jql`, which pages with `nextPageToken`;
  - the Epic Link field is giving way to `parent`;
  - comments must be in Atlassian Document Format.
  Each of these has to be checked against a live site, not guessed.
- **The whitepaper doesn't need it for the POC.** The port (Ch.3.1) is what keeps
  the rest of DAGS tracker-agnostic, and the GitHub adapter already exercises
  every method of that port.
- **Nothing else is blocked by it.** The scheduler, poller, Board and skill only
  depend on `IssueBackend`, so the adapter can be added later without touching
  them.

### What to build when a Jira site is available

Follow the whitepaper, Ch.3.2, and plan §2.6 and §2.11:

| Port method | Jira mechanism |
|---|---|
| `all_tasks` / `ready_tasks` | `POST /rest/api/3/search/jql` with `project = KEY`, paged with `nextPageToken`. Fields: `summary, description, labels, status, issuetype, parent, issuelinks, attachment` plus the Epic Link custom field, if configured. The ready rule is the shared `backends.base.ready_from`. |
| `get_task` | `GET /rest/api/3/issue/{key}`. Convert the ADF description to plain text. |
| `set_status` | Swap the `swarm:status:*` label (`PUT /issue/{key}` with `update.labels` add/remove). On `done`, also run the configured transition (`GET`/`POST /issue/{key}/transitions`). |
| `set_autonomy` | Swap the `swarm:autonomy:*` label. |
| `dependencies` | `issuelinks` of the configured type (default `Blocks`). A link carrying `inwardIssue` means *this issue is blocked by* that one. |
| `epic_children` | JQL `parent = KEY`, falling back to `"Epic Link" = KEY`. |
| `post_comment` | `POST /issue/{key}/comment` with an ADF body. |
| `coordination_ref` | Latest `swarm.yaml` attachment (`coordination_ref:`, `repo:`). Re-attach a new version on change (`POST /issue/{key}/attachments`, header `X-Atlassian-Token: no-check`). Jira's attachment history is the audit trail. |
| `short_key` | The issue key itself (`MW-14`). |

Other requirements:

- **Credentials:** `JIRA_EMAIL` + `JIRA_API_TOKEN` from the environment, or the
  macOS Keychain (service `dags-jira-token`). Never from committed files.
- **HTTP:** stdlib `urllib` only, so `bin/requirements.txt` stays at four packages.
- **Tests:**
  - a recorded-response fake Jira, added to `tests/test_backend_contract.py`
    (the `adapter` fixture) so it runs the same contract suite as `fake` and `github`;
  - one manual smoke run against a real site before calling it done.
- **Labels:** `swarm.py backend init` should print the labels to create. Jira
  creates labels on first use, so this is only a checklist.
