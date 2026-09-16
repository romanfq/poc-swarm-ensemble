---
title: "DAGS — Distributed AGent Swarm"
subtitle: "Human-governed execution on an issue backend and a GitHub coordination layer — Specification v1.1"
date: "2026-09-16"
---

# About this version

This is **version 1.1** of the DAGS whitepaper. It keeps the text of v1.0
(the PDF in the DAGS project) and folds in the decisions made while building
the proof of concept.

- **Where v1.1 changes or adds to v1.0**, a marked note follows the paragraph it
  affects:

  > **v1.1 —** like this.

  Each note cites a decision (**D1**–**D25**). They are listed with their
  rationale in **Appendix A**.
- **Where v1.0's text and figures disagree**, the text wins. The figures of
  v1.0 (1–5) are not reproduced here. Wherever a figure differs from this
  text or its v1.1 notes, the text and notes win.
- **New to v1.1:** Appendix B is a primer for someone new to the tool,
  Appendix C holds how-tos for common tasks, and Appendix D is a reference for
  commands, files and settings.

Status of the implementation (September 2026):
- Chapters 3–10 are built in the coordination repo `poc-swarm-ensemble`,
  except the Jira adapter (D4).
- The full test suite passes on macOS, including the CLI and Swarm Board tests.
- The end-to-end trial on MatchWire has not been run yet.


# Contents

1. Problem Statement and Design Constraints
2. Architecture Overview
3. The Task Graph — A Pluggable Issue Backend
4. The Coordination Repository — GitHub as the State Store
5. Bootstrapping — One Script to Join the Swarm
6. The Claim Protocol — Deterministic, Leaderless Coordination
7. Quota, Concurrency, and Multi-Machine Scheduling
8. Pause, Resume, and Failure Recovery
9. Human Control — Review, Merge, and Conflict Arbitration
10. Swarm Board — A Local Command Centre for Humans
11. Gluing It Together — The End-to-End Flow
12. Limitations and Operating Boundaries

- Appendix A. Decisions log (v1.0 → v1.1)
- Appendix B. Primer — DAGS in ten minutes
- Appendix C. How-to guides
- Appendix D. Reference

**Infrastructure assumed:**

- an issue backend behind the port defined in Chapter 3 (Jira and GitHub Issues
  are the designed adapters; others can be added);
- GitHub: repositories, git, and the official `gh` CLI.

There are no GitHub Actions, no Pages, no hosted CI, and no server anyone has
to operate. Each participating machine runs a lightweight local poller instead
of hosted automation.

**Scope:**

- the coordination protocol;
- one-command swarm bootstrap;
- quota-bounded concurrency and multi-machine parallelism;
- pause/resume and dependency management;
- human authority over code merges and over agent-conflict arbitration.


# 1. Problem Statement and Design Constraints

A set of epics, tracked in whatever issue backend a team already uses, must be
decomposed and executed by a swarm of coding agents. At most N agents may run
concurrently, where N is governed by a shared, time-varying quota. Work may be
parallelized across multiple machines, each capable of running its own local
swarm. The system must handle task interdependencies, safe pausing and
resumption, and agent autonomy boundaries — while guaranteeing that humans
retain final authority over what ships and how conflicts between agents are
settled.

**Hard constraints:**

- No process may be installed on, or kept alive by, a central server.
  - Only these are assumed available:
    - an issue backend behind the abstract port in Chapter 3;
    - GitHub: repositories, git, and the official `gh` CLI;
    - Python 3.10+ on every participating machine (Ch.5), the one runtime
      prerequisite nothing here can install on a human's behalf.
- No hosted automation either: no GitHub Actions, no Pages, no
  webhooks-as-a-service. Any "always on" behaviour must run as an ordinary
  local process on a machine someone already controls.
- Machines may be offline or disconnected for arbitrary periods and must
  reconcile safely on reconnect.
- No agent may merge code into a protected branch without human approval, and
  humans must be able to settle any agent-vs-agent conflict directly, not
  merely observe the automated outcome.
- Joining the swarm on a new machine must be a single command, not a manual
  setup procedure.
- The system must not assume the thing implementing a task is an AI agent, or
  that it can be spawned and left unattended. A human driving their own IDE is
  an equally valid way to complete a claimed task (Ch.7.3).

These constraints rule out both a classic client-server scheduler and reliance
on a platform's hosted automation. The architecture below replaces a live
coordinator with a deterministic protocol every node evaluates identically from
shared, synced state, and replaces hosted notifications with a local poller
every human or machine runs themselves.

> **v1.1 — D3:** the first implementation launches workers on **macOS only**.
> Scheduling, the ledger and the poller are portable Python; only the launchers
> behind the Worker port (Ch.7.3) are macOS-specific.

# 2. Architecture Overview

The system has four layers, each addressed by a specific tool and detailed in
its own chapter. Every machine reads all of them before it acts, and every
machine computes the same conclusions from the same data — there is no
privileged node and no hosted service.

| Layer | Responsibility | Tool |
|---|---|---|
| Plan | Epics, tasks, dependencies, human-facing status, review workflow | Pluggable issue backend (Ch.3) |
| State | Claims, heartbeats, checkpoints, completions, arbitration, audit trail | GitHub coordination repo, plain git |
| Execution | Code changes, tests, pull requests, reviews, merges | GitHub code repos, via `gh` |
| Notification & bootstrap | Joining the swarm; surfacing review-ready and stale work | Local poller, start script and Swarm Board on each machine |

The bound issue backend is the plan humans read and steer. The coordination
repository is the ledger machines use to avoid stepping on each other, and the
record humans use to arbitrate them when they do collide. It is a plain data
repo, so plain git is all it needs.

Code repositories are where actual work happens and where human review gates
live. There, the official `gh` CLI does the GitHub-specific work — opening PRs,
checking CI status, reviewing, merging — in place of the GitHub web UI or a
hand-rolled API client.

The poller, start script and Swarm Board are the only "always on" pieces:
ordinary local processes, not hosted infrastructure.

Of everything in this table, the scheduler loop, poller, `resolve()` and Swarm
Board are all ordinary deterministic Python — none of them writes code or makes
a judgment call. The implementation work is done by a *worker*, dispatched once
per claimed task (Ch.7.3), and this document does not assume that is an AI
agent.

> **v1.1 — D1:** all DAGS code lives inside the coordination repo, in `bin/`.
> "Installing the protocol" is cloning that repo.


# 3. The Task Graph — A Pluggable Issue Backend

**Tool:** an abstract issue-backend port, bound to one concrete adapter per
coordination repo.

The swarm never talks to a tracker directly. It calls a small, fixed interface —
list ready tasks, read a task's dependencies and autonomy tier, transition its
status, post a comment — and a concrete adapter, chosen once (3.4), translates
those calls into whatever the bound tracker actually supports. This is a Ports
and Adapters (hexagonal) boundary: the scheduler, the poller and the Board
depend only on the port.

Each epic is decomposed into subtasks sized for a single agent run (hour-scale,
not minute-scale — see Chapter 12), with dependencies between them forming a
DAG.

## 3.1 The port

```python
# bin/backends/base.py
class IssueBackend(Protocol):
    def ready_tasks(self) -> list[TaskRef]: ...
    def get_task(self, ref: TaskRef) -> Task: ...
    def set_status(self, ref: TaskRef, status: str) -> None: ...
    def dependencies(self, ref: TaskRef) -> list[TaskRef]: ...
    def epic_children(self, ref: TaskRef) -> list[TaskRef]: ...
    def post_comment(self, ref: TaskRef, text: str) -> None: ...
    def coordination_ref(self, ref: TaskRef) -> str: ...
```

`Task` carries whatever the port's callers need regardless of backend:
dependencies, `swarm:autonomy` tier, epic membership and status.

`coordination_ref` is the one method worth flagging. Some backends have no
native home for an arbitrary structured pointer and must store it explicitly
(3.2). Others have an ID that already serves that purpose and implement the
method as a pass-through (3.3).

> **v1.1 — D10, D18:**
>
> - Every shipped adapter also provides:
>   - `all_tasks()`, used by plan sync;
>   - `set_autonomy()`, used by the failure downgrade;
>   - `web_url()` and `short_key()`, used by the Board.
> - `Task` also carries the target code repo (`repo`), whether it is an epic,
>   and whether it is closed.
> - `ready_tasks()` means "tasks the backend does not hold back": open, not an
>   epic, not `blocked` or `done`, and not under a blocked epic. The ledger
>   decides the rest (Ch.7.2).

## 3.2 The Jira adapter

No custom fields and no custom workflow states are assumed — only two
mechanisms available on any Jira instance without admin configuration: plain
labels, for short enumerable values, and a plain file attachment, for anything
more structured.

- **Status labels:** `swarm:status:ready`, `claimed`, `in-progress`,
  `awaiting-review`, `blocked` and `done`, layered on top of whatever native
  status (To Do / In Progress / Done) humans already use.
- **Autonomy labels:** `swarm:autonomy:auto-pr`,
  `swarm:autonomy:human-must-review` and `swarm:autonomy:human-must-scope`
  (Ch.9.2).

`coordination_ref` doesn't fit a label, so the adapter reads and writes one
small attachment, `swarm.yaml`:

```yaml
# swarm.yaml, attached to the Jira ticket
coordination_ref: tasks/EPIC-14/TASK-3
```

`dependencies()` and `epic_children()` read Jira's native issue links and epic
relationship. Re-attaching a new version of `swarm.yaml` is the update
mechanism, and Jira's attachment history is the audit trail.

> **v1.1 — D4:** the Jira adapter is **deferred**; there was no Jira site to
> build against. `backend: jira` currently stops with a pointer to
> `FutureWork.md`, which records the design:
>
> - search via `/rest/api/3/search/jql`;
> - epic membership via `parent`, falling back to Epic Link;
> - comments in ADF;
> - `repo:` in `swarm.yaml` (D10);
> - credentials from the environment or Keychain.

## 3.3 The GitHub Issues adapter

Native issue relationships cover almost the whole port. An issue's own
`org/repo#number` is already a permanent, structured identifier, so
`coordination_ref()` is a pass-through.

```bash
gh issue create --repo org/matchwire-swarm --title "E2: sports-data ingestion" \
  --type Task --parent org/matchwire-swarm#1 \
  --label "swarm:status:ready" --label "swarm:autonomy:human-must-review"
gh issue edit 7 --add-label "swarm:status:claimed" --remove-label "swarm:status:ready"
gh issue edit 7 --add-blocked-by org/matchwire-swarm#1
```

`set_status` and the autonomy tier are ordinary labels. `dependencies()` and
`epic_children()` read GitHub's native sub-issue (`--parent`) and dependency
(`blocked by`) relationships directly — this is the DAG, not a mirror of it.

Issue types (Epic / Task) are configured at the organization level, so they are
unavailable on a personal account's repos. There the adapter uses a plain
`type:epic` / `type:task` label instead.

> **v1.1 — D15, D18, D19, D10:**
>
> - **Command fix:** the edit flag is `--add-blocked-by`; `--blocked-by` is
>   only valid on `gh issue create`.
> - **Reading:** the adapter reads everything with one paginated
>   `gh api graphql` query (labels, parent, subIssues, blockedBy, issueType).
>   This avoids depending on which `--json` fields a given `gh` release
>   exposes. Without type labels, an issue that has sub-issues counts as an epic.
> - **Keys:** tasks are keyed `OWNER/REPO#N`; the short name used everywhere
>   in the UI is `GH-N`.
> - **Target repo:** a task names its code repo with a `repo:OWNER/NAME` label.
> - **Labels:** they must exist before use; `swarm.py backend init` prints (or,
>   with `--apply`, creates) the full set.

## 3.4 Selecting a backend

One config file at the coordination repo's root is read once by `swarm.py` at
startup to import the matching module from `bin/backends/`. It is the one place
a human's choice of tracker is recorded.

```yaml
# backend.yaml, coordination repo root
backend: github            # or: jira (deferred)
github:
  repo: org/matchwire-swarm
jira:
  base_url: https://yourorg.atlassian.net
  project_key: MW
```

Humans author and edit the plan directly in whichever backend is bound. The
swarm never invents tasks or dependencies; decomposition is a human act, kept
separate from execution. A human may use an AI assistant to draft epics, but
that assistant is not a swarm participant — it holds no claim and consumes no
quota.

> **v1.1 — D10, D14, D6, D9:** `backend.yaml` also holds the swarm-wide
> settings every machine must share:
>
> ```yaml
> repos:                          # per code repo: base branch and local test command
>   org/matchwire-backend: {base: main, test_command: ./mvnw -q verify}
>   org/matchwire-frontend: {base: main, test_command: npm test --silent}
> default_repo: org/matchwire-backend   # repo for tasks without a repo: label
> epic_repos:                           # optional per-epic default
>   GH-1: org/matchwire-frontend
> swarm:
>   default_quota: 3                    # global N until a quota/ record overrides it
>   lease_minutes: 15
>   heartbeat_minutes: 3
>   max_retries: 3
>   human_idle_hours: 8
>   thrash_threshold: 2
> ```
>
> A task's code repo is, in order: its `repo:` label, its epic's entry in
> `epic_repos`, then `default_repo`. The resolved repo is recorded in the
> task's `meta.yaml`.


# 4. The Coordination Repository — GitHub as the State Store

**Tool:** a dedicated GitHub repository (not a code repo).

It acts as the append-only ledger of machine state: who is doing what, since
when, what has been tried, and where a human has stepped in to settle a
conflict. It mirrors, but does not replace, the issue backend's task status.

Layout (v1.1):

```text
bin/
  swarm.py            # one-command bootstrap and CLI (Ch.5)
  resolve.py          # claim resolution, run locally (Ch.6)
  poll.py             # local poller (Ch.9.4)
  board.py            # Swarm Board, Textual UI (Ch.10)
  dev-setup.sh        # venv + full test suite, for developers
  requirements.txt    # typer, rich, textual, pyyaml (Ch.5.2)
  dags/               # support package: ledger I/O, git sync, gh, scheduler, …
  backends/           # base.py (port), github.py, fake.py; jira.py deferred
  workers/            # base.py (port), claude.py, intellij.py, vscode.py
  skill/              # injected as .swarm-task/ (Ch.7.3): README.md, swarm-task
backend.yaml          # adapter + shared settings (Ch.3.4)
humans.yaml           # recognised humans (Ch.6.6)
CONVENTIONS.md        # required reading for every worker (Ch.9.3)
templates/
  commit-message.txt  # every worker commit (4.1)
  pr-description.md   # every worker PR (4.1)
tasks/EPIC-14/TASK-3/
  meta.yaml           # issue ref, repo, dependencies, autonomy — never rewritten
  meta/<machine>-<clock>.yaml             # later tracker changes (revisions)
  claims/<machine>-<clock>.yaml
  withdrawals/<machine>-<clock>.yaml
  arbitration/human-<name>-<clock>.yaml   # optional (Ch.6.6)
  plan-reviews/human-<name>-<clock>.yaml  # plan approvals (Ch.7.3)
  heartbeats/<machine>.yaml               # single writer
  checkpoint.yaml                         # single writer (Ch.8)
  completions/<machine>-<kind>-<clock>.yaml
tasks/EPIC-14/_epic/meta.yaml             # the epic itself
control/<machine>-<action>-<clock>.yaml   # pause/resume/stop/start/throttle (Ch.10.3)
priority/<machine>-<epic>-<action>-<clock>.yaml   # epic takeover/release (Ch.10.4)
quota/<human>-<clock>.yaml                # global N (Ch.7.1)
.swarm/      (git-ignored) machine-local state: venv, identity, logs, clones
.worktrees/  (git-ignored) one git worktree per claimed task
```

The governing rule is **append-only, never mutate-in-place**. Claims,
completions, arbitration, control, priority and quota records are always new,
uniquely named files. Two writers at once therefore cannot produce a textual
git conflict: their files coexist after a sync, and arbitration happens
afterwards, as pure computation (Chapter 6).

Every read or write against this repository is plain git — pull, commit, push.
There is nothing here for `gh` to do; it earns its place in the code repos.

> **v1.1 — D7:**
>
> - **Mutable files.** Two kinds of file are rewritten in place:
>   - `heartbeats/<machine>.yaml` (YAML rather than `.txt`);
>   - `checkpoint.yaml`.
>
>   Only the current owner of the task writes them, and it re-checks
>   ownership after every pull, so no two machines write them concurrently.
>   If a stale copy ever conflicts, the remote version wins.
> - **Record names.** The claim ID is `<machine>-<clock>`, e.g. `laptop-b-450`.
>   Every record carries `logical_clock`, `machine` and `wall_utc`.
> - **`meta.yaml`.** It is written once at import. Later changes in the tracker
>   become revisions under `meta/`, and readers merge them by clock.

## 4.1 Commit and PR templates

Two committed templates keep every worker's output legible regardless of which
machine, human or IDE produced it.

```text
# templates/commit-message.txt
[{task_ref}] {summary}

Issue: {issue_ref}
Coordination-ref: {coordination_ref}
```

```markdown
# templates/pr-description.md
## Summary
{summary}

## Risks
{risks}

## Open questions
{open_questions}

## Task
Issue: {issue_ref}
Coordination ref: {coordination_ref}
```

Agents render these programmatically from the fields kept in `checkpoint.yaml`
(Ch.8), not through git's interactive editor. `swarm.py` also sets
`commit.template` to this file in every code repo clone, so a human committing
by hand gets the same prompt.

> **v1.1:**
>
> - In the rendered text, `{task_ref}` is the short key (e.g. `GH-7`) and
>   `{coordination_ref}` is the task's ledger path, e.g. `tasks/GH-1/GH-7`.
> - Empty risk and question lists render as "None.".


# 5. Bootstrapping — One Script to Join the Swarm

**Tool:** `bin/swarm.py`, committed in the coordination repo.

The whole onboarding procedure for a new machine is: clone the coordination
repo, run one script. Nothing else to install, configure or register centrally.

## 5.1 Why Python, not bash

Everything the bootstrap starts — `resolve.py`, `poll.py`, the Board — is
Python. The tool parses YAML on every record, computes a logical clock across
the whole repo, and shells out to git and gh constantly. A bash entry point
would only re-implement argument parsing and process supervision, then call
Python for everything that matters. So the entry point is Python too:
`bin/swarm.py`, directly executable (`#!/usr/bin/env python3`).

## 5.2 Dependencies

Four small, widely used libraries, declared once:

```text
# bin/requirements.txt
typer>=0.16     # command-line interface
rich>=13.7      # formatted terminal output
textual>=0.58   # the Swarm Board UI (Ch.10)
pyyaml>=6.0     # every claim, checkpoint and arbitration file
```

`swarm.py` installs these itself into an isolated virtual environment under
`.swarm/venv` the first time it runs. The equivalent manual command, for a
locked-down machine:

```bash
python3 -m venv .swarm/venv
.swarm/venv/bin/pip install -r bin/requirements.txt
```

> **v1.1 — D23:**
>
> - **Version.** The typer minimum was raised from 0.12 to 0.16; older typer
>   breaks with click 8.2 and later.
> - **Re-exec and rebuild.** `swarm.py` re-executes itself inside the venv. The
>   venv is stamped with the platform, CPU and Python version, and is rebuilt
>   automatically if it was created somewhere else (e.g. inside a Linux VM that
>   mounts the same folder).
> - **Options.**
>   - `DAGS_NO_VENV=1` skips the bootstrap.
>   - `bin/dev-setup.sh` builds the same venv with the test dependencies and
>     runs the suite; that venv also serves normal runs.

## 5.3 What the script does, in order

1. **Checks prerequisites** — git, the `gh` CLI, `gh auth status` and
   Python 3.10+ — and fails fast with a clear message if any are missing. This
   is also where it creates `.swarm/venv` if needed.
2. **Pulls the coordination repo** it runs from. The human clones it once, by
   hand; that is the only manual step.
   - For every code repo referenced by unfinished tasks, it runs
     `gh repo clone`, or fetches if the repo is already present.
   - On every run it idempotently sets `commit.template` in that repo to
     `templates/commit-message.txt`.
   - It also makes sure `.swarm-task/` is listed in the repo's shared
     `info/exclude` (Ch.7.3), checked rather than blindly appended.
3. **Establishes a stable machine identity** (`hostname-<4 hex>`) on first run
   and caches it, so this machine's claims are attributed consistently across
   restarts.
4. **Starts the local loops:**
   - the scheduler (Ch.7), which claims and dispatches ready tasks up to this
     machine's share of quota;
   - the poller (Ch.9.4), which syncs periodically and notifies humans;
   - and makes the Swarm Board (Ch.10) available.
5. **Prints a status panel** (5.4) and exits, or stays attached showing the
   live Board with `--attach`.

> **v1.1 — D8, D17, D6:**
>
> - **Step 2 — code repo location.** If `.swarm/local.yaml` maps the repo to an
>   existing checkout, that checkout is used in place. Otherwise the clone goes
>   to `.swarm/repos/OWNER/NAME`.
> - **Step 2 — plan sync.** Plan sync (Ch.11 step 1) runs here and on every
>   scheduler cycle.
> - **Step 3 — two more steps.**
>   - The operator is identified: `gh api user` is matched against
>     `humans.yaml`, or `human:` is set in `.swarm/local.yaml`. `start` refuses
>     to run for someone not listed there.
>   - Clock skew against GitHub is measured from the `Date` header of
>     `gh api -i /meta`.
> - **Step 4 — process model.** Threads die with their process, so `start` spawns a
>   **detached daemon** running the scheduler, heartbeat and poller loops. It
>   writes `.swarm/daemon.pid`, `.swarm/daemon.json` and `.swarm/swarm.log`,
>   then prints the panel and exits.
> - **Step 4 — the Board.** A terminal UI can't live inside a headless daemon,
>   so the Board is its own foreground process: `swarm.py board`, or
>   `swarm.py start --attach`. v1.0's `./bin/swarm.py --attach` becomes
>   `start --attach`.
> - **Step 4 — shared record.** `start` writes a shared `control/…-start-…`
>   record carrying the quota share and default worker.

## 5.4 Beautified output

```text
$ gh repo clone <org>/<coordination-repo>
$ cd <coordination-repo>
$ ./bin/swarm.py start --quota-share 3 --poll-interval 60s
╭─────────────────────────── swarm ───────────────────────────╮
│ identity      mbp-jane-7f2a (new)  ·  operator jane         │
│ coordination  synced -- 14 tasks ready                      │
│ scheduler     ● running   quota-share 3  ·  default worker … │
│ poller        ● running   interval 60s                      │
│ board         ● available  `swarm.py board` or --attach      │
│ quota         2/4 in use swarm-wide  ·  this machine 0/3    │
╰─────────────────────────────────────────────────────────────╯
```

`--quota-share` lets a human bound how much of the global quota N this machine
may consume. Stopping is symmetric: `./bin/swarm.py stop` signals every loop to
finish its current cycle and exit. Leases are released through the normal
heartbeat timeout rather than an abrupt kill.

> **v1.1:**
>
> - **Extra panel rows.** The panel also shows tasks waiting for a worker
>   choice, plans waiting for review, and PRs awaiting review.
> - **`status` command.** `swarm.py status` prints the same panel at any time.
> - **Throttling.** `swarm.py throttle N` changes the share live, without a
>   restart (Ch.10.2).

## 5.5 Preparing a code repo, once

Everything in 5.3 is local git config and self-heals on every run. One thing
doesn't fit that pattern: **branch protection** (Ch.9.1), the server-side
setting that makes "an agent may open a PR but never merge one" a structural
guarantee. It is global to the repo, and no machine should decide to set it. A
human does it once, before the first task against a repo is claimed.

On GitHub's Free plan this only works on a **public** repository. A private
repo needs GitHub Pro or Team, or the protection silently doesn't apply. Decide
this deliberately per repo.

> **v1.1 — D15:** v1.0's one-line `gh api … -f` call can't work. The
> protection endpoint requires all four of these fields, and `-f` cannot send
> `null`:
>
> - `required_status_checks`
> - `enforce_admins`
> - `required_pull_request_reviews`
> - `restrictions`
>
> Use:
>
> ```bash
> ./bin/swarm.py protect org/matchwire-backend            # prints the call (dry run)
> ./bin/swarm.py protect org/matchwire-backend --apply    # a human applies it
> ```
>
> The body sets:
>
> - one required approval, with stale approvals dismissed;
> - `enforce_admins: true`;
> - no required status checks (there is no hosted CI, Ch.9.1).
>
> With the bot account of D2, "agents can never merge" holds even for repo
> admins.


# 6. The Claim Protocol — Deterministic, Leaderless Coordination

**Tool:** `bin/resolve.py`, run locally by every machine.

This replaces a live scheduler. Given the same synced repo state, every machine
computes the same answer to "who owns this task", with no network calls beyond
git sync — and a human can always override the answer directly.

## 6.1 Logical clock

Wall clocks are not trusted for ordering. Every record carries a Lamport-style
counter, computed fresh at write time and never stored or incremented locally:

```text
next_clock() = 1 + max(logical_clock across every record in the repo)
```

A newly joined machine's first claim is automatically ordered after everything
it can see, and a machine returning from an offline period jumps forward rather
than reusing a stale low value.

## 6.2 Making a claim

- Sync (`git pull --rebase`).
- Confirm the task is ready: its dependencies are Done, there is no valid
  claim, no arbitration record, and no open PR.
- Write a uniquely named claim file with the freshly computed logical clock.
- Commit and push. On non-fast-forward rejection, pull/rebase (no textual
  conflict is possible, by construction) and push again.

## 6.3 Resolution — the default tiebreaker

```text
resolve(task):
    if arbitration_record_exists(task):
        return arbitration_record.winner      # human decision, absolute (6.6)
    claims = valid claims in task/claims/     # not withdrawn, not expired (6.5)
    return min(claims, key = (logical_clock, machine_id))
```

Both racing claims are preserved in the ledger; only the interpretation of who
owns the task is computed, and it is computed identically everywhere,
including by machines that sync hours later.

> **v1.1:**
>
> - **Which claims count.** A claim stops being valid once it is withdrawn or
>   its lease has expired. A claim whose PR is open stays attributed to its
>   task until the PR is merged, rejected or reopened.
> - **Arbitration order.** The latest arbitration record (by clock) is the one
>   in force. A record with `action: withdraw` lifts the arbitration.

## 6.4 What a losing agent sees

Git accepts a losing claim without error; the loss is only visible by
re-running `resolve()`. A machine must re-check `resolve()` before any
expensive or irreversible step, not only at claim time. On detecting a loss it
stops work, marks the claim withdrawn for the audit trail, and picks a
different ready task.

> **v1.1:**
>
> - **Where the re-check happens.** The scheduler re-checks right after each
>   claim, every cycle, and before every heartbeat. `swarm-task done` checks at the
>   start and again just before pushing; the PR is opened straight after the push.
> - **Withdrawal reasons.** Every withdrawal records one:
>   - `lost-race`, `arbitration`, `released` or `quota` — these are not counted as failures;
>   - any other reason is counted as a failure (Ch.8).

## 6.5 Lease expiry, evaluated lazily

There is no watchdog process. A claim is valid only if its heartbeat was
updated within the lease window as of the last sync; any machine, when next
picking work, treats a stale claim as expired. Expiry is a read-time
computation, not a background job.

> **v1.1 — D6:**
>
> - **Clock.** "Within the lease window" needs a clock. Heartbeats and claims
>   carry `wall_utc` in addition to the logical clock, and each process
>   corrects its own wall clock by its measured skew against GitHub (5.3).
> - **Timing.** Defaults: lease 15 minutes, heartbeat every 3 minutes. With
>   that much margin, leftover skew doesn't matter.
> - **Why not logical time alone.** Purely logical leases were rejected: they
>   break when only one machine is active.

## 6.6 Human arbitration overrides the algorithm

The tiebreak in 6.3 is a default, not a ceiling on human authority. Any
identity listed in `humans.yaml` may write an arbitration file that always wins
`resolve()`, regardless of logical clock:

```yaml
# tasks/EPIC-14/TASK-3/arbitration/human-jane-500.yaml
human: jane
winner: laptop-b-450          # or: none (freeze the task, no one owns it)
reason: "laptop-a's branch touches shared config, defer to laptop-b"
logical_clock: 500
```

This gives humans three low-friction levers: pick a winner outright, freeze a
task until the arbitration is withdrawn, or do nothing and let 6.3 stand.
Because arbitration is checked first, a human's decision is never a race
against agents. A repeatedly thrashing task (the same two machines racing
more than once) is what the poller surfaces as a suggested arbitration target
(Ch.9.4).

> **v1.1 — D16:**
>
> - **Unlisted names.** Records naming a human who isn't in `humans.yaml` are
>   ignored by every machine. The same rule applies to quota and plan-review
>   records.
> - **Not built yet.** The planned check that the arbitration commit's author
>   email matches that human's `emails:` entry in `humans.yaml`.
> - **Frozen tasks.** A machine holding a claim on a frozen task withdraws it
>   (reason `arbitration`) on its next cycle.
> - **Reason required.** The `reason` field is mandatory; the tooling refuses
>   an empty one.


# 7. Quota, Concurrency, and Multi-Machine Scheduling

**Tool:** the scheduler loop started by `swarm.py` on each machine.

There is no global scheduler process. Each machine runs an identical loop that
treats quota as just another precondition for claiming work. The loop never
writes code: for each task it wins, it hands off to exactly one worker (7.3).

## 7.1 The global cap

N is enforced as a counted resource. Before claiming, a machine counts
currently valid claims across the whole repo; if that count is already N, it
does not claim, whatever its local capacity. A machine's `--quota-share`
further caps how much of that room it takes for itself.

> **v1.1 — D9:**
>
> - **Where N lives.** N is the latest record in `quota/` written by a
>   recognised human (`swarm.py quota set N`, or `n` on the Board). Until one
>   exists, `swarm.default_quota` applies.
> - **What counts.** Only tasks that are claimed or in progress use up quota.
>   A task awaiting review releases its slot (Ch.9.1).
> - **Throttle.** A `throttle` control record overrides this machine's
>   `--quota-share` live.

## 7.2 Multi-machine parallelism

Each machine runs its own loop against the same repo and races safely
(Chapter 6). No machine needs to know about the others directly. A task is
"ready" purely from locally read data: its upstream dependencies show Done in
the coordination repo, and no arbitration freeze is in effect. Load is not
globally optimized — only locally correct decisions are guaranteed.

> **v1.1 — D11, D12, D10:**
>
> - **Readiness.** A task is ready when all of these hold:
>   - the backend lists it in `ready_tasks()`;
>   - the ledger shows all its dependencies done;
>   - no arbitration is in force (neither a freeze nor an award);
>   - it has no live claim and no open or rejected PR.
> - **Order.** Candidates are taken in import order. Tasks under an epic that
>   another machine has taken over come last (Ch.10.4).
> - **Skipped tasks.** A machine whose default worker is an AI never claims
>   `human-must-scope` tasks, and tasks without a target repo are never claimed.

## 7.3 What actually implements a task — choosing a worker

Everything so far is ordinary deterministic Python; none of it writes a line
of the actual task. A **worker** is whatever does: an autonomous coding CLI,
or equally a human at their own IDE. The scheduler depends on a small port,
and each worker type is a binding.

```python
# bin/workers/base.py
class Worker(Protocol):
    name: str
    def dispatch(self, task: ClaimedTask, worktree: Path) -> None: ...
```

Three bindings ship:

- **claude** — an interactive `claude` session in a new terminal, inside the
  task's worktree, pointed at the injected skill.
- **IntelliJ + Human** — IntelliJ opens on the worktree with
  `.swarm-task/README.md` as the first tab.
- **VSCode + Human** — the same via `code <worktree>`.

> **v1.1 — D3:** the launchers are macOS-only:
>
> - **claude:** `osascript` opens Terminal, or iTerm if `terminal_app: iTerm`
>   is set.
> - **IntelliJ:** `open -na "IntelliJ IDEA.app" --args <worktree> <README>`.
> - **VSCode:** `code -n <worktree> <README>`.
>
> Per-machine overrides live in `.swarm/local.yaml`: `claude_bin`,
> `claude_args`, `intellij_app` and `code_bin`.

**Selecting a worker, at claim time.** Claiming stays fully automatic. With a
default worker (`swarm.py start --default-worker claude`), dispatch follows
immediately. Otherwise the task sits in "claimed, awaiting worker" — without
blocking the loop — until a human answers on the Board (10.7):

```text
[swarm-board] I have claimed TASK-3 for completion, who is my worker?
  a) claude
  b) IntelliJ + Human
  c) VSCode + Human
```

The answer is written into `checkpoint.yaml`, so it survives a resume and
appears in the audit trail.

> **v1.1 — D12:**
>
> - **Scope-restricted tasks.** For a `human-must-scope` task the prompt offers
>   only the human workers.
> - **Answering elsewhere.** The same choice can be made without the Board:
>   `swarm.py task worker TASK-3 b`.
> - **Prepared in advance.** The worktree is prepared as soon as the claim is
>   won, so the choice takes effect at once.

**Inputs and isolation.** Before dispatch the scheduler assembles the ticket
(via `get_task`), the shared conventions document and, on a resumed task,
`checkpoint.yaml`. Every dispatch gets its own git worktree, so several workers
on one machine never collide.

> **v1.1 — D17:**
>
> - **Worktree and branch.** The worktree is `.worktrees/<TASK>`, on branch
>   `swarm/<TASK>`.
> - **Starting point.**
>   - A brand-new task starts from `origin/<base>`.
>   - A resumed task reuses the local or pushed `swarm/<TASK>` branch, so the
>     next worker continues from whatever the last one pushed.

**The injected skill — plan, implement, done.** Dispatch copies a small skill
package into the worktree — the same for every worker type — plus a fresh copy
of the ticket:

```text
.swarm-task/
  README.md        # plain-English instructions for a human
  spec.md          # the ticket's text, downloaded at dispatch
  conventions.md   # CONVENTIONS.md
  context.json     # task, claim, repo, branch, test command (v1.1)
  swarm-task       # the skill's CLI
```

- `swarm-task plan` reads the spec, conventions and (on a resume) the
  checkpoint, and produces `plan.md`. Nothing is implemented yet.
- A **review gate**, human or self-review, comes before anything proceeds.
- `swarm-task implement` proceeds against the reviewed plan and keeps
  `checkpoint.yaml` up to date.
- `swarm-task done` is the completion signal, and the only subcommand that
  touches anything outside the worktree.

> **v1.1 — D20, D21, D12:**
>
> - **Implementation.** The skill is stdlib-only Python, so it runs in any
>   worktree. It delegates ledger, backend and GitHub work to
>   `swarm.py task …`.
> - **Commands:**
>   - `plan` writes the `plan.md` template. `plan --submit` records the plan
>     text and its hash in `checkpoint.yaml`.
>   - `status` shows whether the plan has been approved.
>   - `implement` refuses until the plan is approved. Then it prints the plan,
>     the checkpoint, and any tagged reviewer feedback (`fix:`, `explain:`,
>     `reject-approach:`).
>   - `note --summary/--tried/--remaining/--question/--risk` records progress.
>   - `block "<question>"` records a question for a human and stops.
>   - `done` finishes the task (below).
> - **The review gate by tier:**
>   - `auto-pr`: the worker may approve its own plan.
>   - `human-must-review`: a human approves on the Board (`v`) or with
>     `swarm.py task approve-plan`. The approval is an append-only record in
>     `plan-reviews/`; submitting a changed plan needs a fresh approval.
>   - `human-must-scope`: only a human worker may take the task at all, and its
>     plan needs a human approval, as for `human-must-review`. `done` refuses
>     if the recorded worker is an AI.

**The output contract — the same for every worker.** Before the scheduler
considers a claim finished, it needs:

- a commit following `templates/commit-message.txt`;
- an updated `checkpoint.yaml`;
- autonomy permitting, a `gh pr create` using the PR template.

`swarm-task done` produces all three. A worker that cannot proceed says so in
`checkpoint.yaml` and never runs `done`. If a worker dies, hangs or never
fulfils the contract, nothing special happens: the heartbeat goes stale, the
lease expires (6.5), and the task returns to Ready.

> **v1.1 — D14, D2, D22:** `swarm-task done`, in order:
>
> 1. Checks the claim is still this machine's, the plan is approved, and a
>    summary has been recorded.
> 2. Runs the repo's `test_command` and stops on failure.
> 3. Commits all changes with the rendered template, authored as the bot if
>    one is configured. `.swarm-task/` stays out of the commit because the
>    repo's `info/exclude` lists it (Ch.5.3).
> 4. Re-checks ownership, then pushes `swarm/<TASK>`.
> 5. Opens the PR with the **bot token**, or comments on the existing PR if one
>    is still open (the request-changes loop).
> 6. Records `pr_url` in the checkpoint and writes a `pr-opened` completion.
> 7. Sets the ticket to `awaiting-review` and posts the same rendered text as a
>    ticket comment.

**Cleanup — none of this reaches the main repo.** `.swarm-task/` is excluded
from git before any worker touches the worktree, so cleanup is a disk
deletion. The moment the poller or Board observes the output contract
fulfilled, it — not the worker — runs `rm -rf .swarm-task/`, before reporting
the task finished. Nothing about the orchestration appears in the PR's diff or
commit log.

> **v1.1:**
>
> - **When the contract counts as fulfilled.** The `pr-opened` record exists
>   *and* the checkpoint carries the same PR URL.
> - **Removing the worktree.** A task's worktree is removed once its PR is
>   merged or closed: immediately by the Board's `m` / `swarm.py task merge`,
>   and on every machine by its poller, which sweeps the worktrees of tasks
>   that are done or rejected.

This is also the literal meaning of quota N: a hard cap on how many workers —
subprocesses or humans — may be actively dispatched across the whole swarm.


# 8. Pause, Resume, and Failure Recovery

**Tool:** `checkpoint.yaml` per task, plus the git branch/worktree it
references.

Every task is resumable by any machine, not just the one that started it. An
agent resuming a task never restarts from the original ticket; it reads
`checkpoint.yaml` — branch, what has been tried, what remains, open
questions — as its starting context. That one file serves crash recovery,
quota-driven pause/resume, and progress reporting to humans.

Failure modes and their handling:

- **Agent crash:** the heartbeat goes stale, the lease expires lazily
  (Ch.6.5), and the task returns to Ready.
- **Quota exhausted mid-task:** the task is checkpointed and not reclaimed
  until a slot frees. This is the same "blocked" state as an unmet dependency.
- **Human pause:** set the ticket to Blocked in the backend, or write an
  arbitration record with `winner: none`. Every machine's readiness check skips
  the task.
- **Repeated failure:** past a threshold of retries, the ticket's
  `swarm:autonomy` label is lowered one step and the task goes to human triage.
- **Machine goes offline:** leases expire naturally once the heartbeat window
  passes. No cleanup is needed anywhere else.

> **v1.1 — D7, D12, D13:**
>
> - **Quota exhausted mid-task.** When N or a machine's share is lowered below
>   what is running, the newest claims yield first; every machine computes
>   the same order, so together they release exactly the excess.
>   - A claim with no worker yet is withdrawn at once (reason `quota`).
>   - A running task gets `pause_requested` in its checkpoint and a
>     notification. The skill tells the worker to record progress with
>     `swarm-task note` and stop, and `implement` refuses to continue.
>   - One lease later (15 minutes by default) the claim is withdrawn
>     (reason `quota`) and the ticket goes back to `ready`.
>   - The task then waits until a slot frees and resumes from its checkpoint
>     and pushed branch. If the quota is raised during the grace period, the
>     request is lifted and the work carries on.
>   - A `quota` withdrawal doesn't count as a failure.
> - **Retry count.** It is computed from the ledger, not stored in
>   `meta.yaml`: the number of claims that expired, or were given up for a
>   reason other than `lost-race` / `released` / `arbitration` / `quota`.
> - **Downgrade.** When the count reaches `max_retries` (default 3), plan sync
>   lowers the autonomy one step in the backend, comments on the ticket, and
>   records the change as a meta revision. The next downgrade needs another
>   `max_retries` failures.
> - **Resuming.** A resumed checkpoint keeps the earlier history (`tried`,
>   `remaining`, `open_questions`, `previous_claims`), and the new machine
>   continues from the pushed `swarm/<TASK>` branch.
> - **Idle limit.** A worker that stays attached but makes no progress would
>   otherwise hold its lease forever, because the daemon heartbeats on its
>   behalf. So after `human_idle_hours` (default 8) with no new commit and no
>   checkpoint change:
>   1. The owner is asked "still working on TASK?". They answer with
>      `swarm.py task still-working TASK`.
>   2. If nobody answers within one more lease period, heartbeats stop and the
>      claim expires normally.


# 9. Human Control — Review, Merge, and Conflict Arbitration

**Tool:** GitHub pull requests via `gh`, branch protection rules,
`humans.yaml`, and the local poller.

This is the ultimate authority layer. Every mechanism above can be overridden
or halted by a human through ordinary issue-backend or GitHub actions, or
through a single arbitration file.

## 9.1 Mandatory gates

- **Opening, never merging.** The worker opens a pull request with
  `gh pr create --body-file <rendered pr-description.md> --base main`, and may
  never run `gh pr merge` in that step. Branch protection (Ch.5.5) enforces this
  server-side.
- **Awaiting review.** On completion, the task moves to Awaiting Review and
  stops using a quota slot.
- **Merging.** Merge requires an explicit human review:
  `gh pr review --approve` followed by `gh pr merge --squash` (one key on the
  Board), plus passing CI, checked with `gh pr checks`.
- **Overlapping files.** When a task's files overlap another in-flight task's,
  the poller flags both (via `gh pr diff`) and a human decides the merge order.

> **v1.1 — D2, D14, D11:**
>
> - **Who opens and who merges.** PRs are opened under the **bot account**;
>   humans review and merge under their own `gh auth`. That keeps GitHub's
>   "authors can't approve their own PR" rule from blocking the merge, and keeps
>   `enforce_admins` on.
> - **No hosted CI.** "CI passing" is the repo's `test_command`, run by
>   `swarm-task done` before the PR exists. Merge reads the PR's status checks
>   (`gh pr view --json statusCheckRollup`) and refuses only if some are
>   *failing* (`--force` overrides); a PR with no checks is fine. It also
>   refuses if the PR is no longer open.
> - **Done.** A task is Done when its PR is **merged**. The Board records that
>   immediately after Approve & merge. The poller records it for merges made
>   anywhere else (e.g. the GitHub web UI), sets the ticket to `done`, and
>   dependants become ready.

## 9.2 Human override paths

- **Halt everything:** block an epic in the issue backend. Every task under it
  becomes unready.
- **Arbitrate a conflict directly:** write an arbitration file (Ch.6.6) naming
  the winner, or freeze the task.
- **Reject an approach:** distinct from "needs revision". Close the PR with
  `gh pr close` and re-open planning in the backend, rather than looping the
  same worker on a rejected approach.
- **Throttle a machine:** lower its quota share without touching the global
  cap.
- **Autonomy tier:** set per task with the ticket's `swarm:autonomy` label:
  whether an AI worker may run unattended and open a PR, or must wait for
  scoping approval.

> **v1.1 — D22, D12:**
>
> - **What a closed PR does.** The poller records `rejected` and sets the
>   ticket to `blocked`, with a comment asking for re-planning. When a human
>   sets `swarm:status:ready` again, plan sync records `replanned` and the task
>   can be claimed. Add the lesson to `CONVENTIONS.md` so it doesn't come back.
> - **Throttle.** `swarm.py throttle N`, or `t` on the Board, applies without a
>   restart.
> - **Tiers.** The exact behaviour of each tier is in 7.3 (review gate) and
>   Appendix A (D12).

## 9.3 Information flow between humans and workers

Structured, not free-form:

- **What the worker writes:**
  - the PR description: `templates/pr-description.md`, rendered from
    `checkpoint.yaml`;
  - commits, following `templates/commit-message.txt`;
  - a ticket comment reusing the same rendered text.
- **What humans write back:** tagged review comments, e.g.
  `gh pr review --request-changes -b "fix: ..."`, using the tags `fix:`,
  `explain:` and `reject-approach:`. A resuming worker reads them with
  `gh pr view --json comments,reviews`.
- **Arbitration** carries a mandatory `reason`.
- **Conventions.** A shared conventions document is required reading for every
  worker, so a rejected approach doesn't resurface on another task.

> **v1.1 — D22:**
>
> - **What a request for changes does.** Each new CHANGES_REQUESTED review
>   makes the poller record `reopened` exactly once (keyed by review id). The
>   ticket goes back to `ready`.
> - **Who picks it up.** Any machine may claim it and resume from the
>   checkpoint. `swarm-task implement` lists the tagged feedback, and the next
>   `done` pushes to the same branch and comments on the same PR.

## 9.4 The local poller — notification without a hosted service

`bin/poll.py`, started by `swarm.py`, replaces hosted automation. Any human
who wants visibility runs it on their own machine. On each cycle it:

1. Syncs the coordination repo.
2. Diffs against its last-seen state. It looks for tasks that newly entered
   Awaiting Review, tasks whose heartbeat has gone stale, and tasks with more
   than one live claim.
3. For any task with an open PR, reads the live review and CI state with `gh`.
4. Emits a local notification for each: a desktop notification, a log line, or
   an outbound webhook. The notify step is a single pluggable function.
5. For a repeatedly racing task (the same conflict signature across several
   cycles), escalates the alert to "needs arbitration" instead of repeating it.

Because every human who wants alerts runs the poller, no single point must stay
up for the team to have visibility. The trade-off is that notification latency
equals each human's poll interval (Chapter 12).

> **v1.1 — D8, D24, D22:**
>
> - **State file.** The poller keeps its last-seen state in
>   `.swarm/poller-state.json`. On its first run it doesn't replay the feed or
>   old state changes. Conflicts, PR outcomes, overlaps and "finished"
>   announcements are still reported on the first cycle.
> - **More it does.**
>   - Watches the output contract (Ch.7.3) for this machine's tasks.
>   - Turns PR state into ledger records: merged → `done`; closed → `rejected`;
>     new CHANGES_REQUESTED → `reopened`.
>   - Removes this machine's worktrees for every task that is done or rejected.
>   - Reports overlapping files once per pair of PRs.
> - **Where notifications go.**
>   - They always go to `.swarm/notifications.log`.
>   - For macOS notifications, set `notify: {desktop: true}` in
>     `.swarm/local.yaml`.
>   - For a webhook, set `notify: {webhook: <url>}`.
> - **Where it runs.** Normally inside the daemon. The Board runs its own
>   poller when no daemon is running. It also runs standalone with the venv
>   interpreter: `.swarm/venv/bin/python bin/poll.py [--once]`.
> - **Escalation threshold.** `swarm.thrash_threshold` (default 2) sets how
>   many conflict cycles, or lost races between the same pair of machines,
>   count as "needs arbitration".


# 10. Swarm Board — A Local Command Centre for Humans

**Tool:** `bin/board.py`, a Textual dashboard.

The Board is built with Textual (from the Rich ecosystem). It reads the same
local clone the poller syncs, and shells out to `gh` only for what lives on
GitHub (PR status, CI, reviews). It replaces "read YAML in an editor" with
one-key controls and a plain-English feed of what every human's swarm is doing.

## 10.1 What it is, concretely

Textual needs no browser and runs anywhere a terminal does, over SSH included.
For anyone who prefers a browser, the same UI can be served on
`http://localhost:4590`. Most controls write an ordinary file into the local
clone and let the sync carry it; a few run the exact `gh` command a human would
otherwise type. The Board never bypasses the mechanisms of Chapters 6 and 9.

> **v1.1 — D8, D15:**
>
> - **How to open it.** `swarm.py board`, or `swarm.py start --attach`. Running
>   `bin/board.py` directly needs the venv interpreter:
>   `.swarm/venv/bin/python bin/board.py`.
> - **In a browser.** `swarm.py board --web` runs `textual serve` on port 4590,
>   installing `textual-dev` into the venv on first use.
> - **Refresh.** The Board refreshes every 5 seconds.

## 10.2 Commands over this machine's swarm

| Key | Command | Effect |
|---|---|---|
| `p` | Pause | Scheduler stops claiming; in-flight work carries on. Writes a shared control record. |
| `r` | Resume | Reverses Pause. |
| `t` | Throttle | Sets this machine's quota share live (control record). |
| `s` | Stop | Full shutdown, like `swarm.py stop`; leases expire normally. |
| `f` | Freeze / unfreeze | Writes an arbitration record with `winner: none`, or lifts it. |
| `a` | Reassign | Picks a winner from the task's claimants; arbitration record with a reason. |
| `e` | Take over / release epic | Soft priority for this machine on the selected task's epic (10.4). |
| `m` | Approve & merge | `gh pr review --approve`, then `gh pr merge --squash`, then records Done. |
| `o` / `O` | Open ticket / PR | Opens the ticket or the PR in the browser. |
| `n` | Set global N | Writes a `quota/` record (v1.1). |
| `v` | Review plan | Approve or send back a submitted plan (v1.1). |
| `w` | Choose worker | Re-opens the worker prompt for a task waiting on one (v1.1). |
| `q` | Quit | Closes the Board only; the daemon keeps running. |

Everything except Approve & merge follows the append-only pattern: a new,
uniquely named file carried by the normal sync. Approve & merge runs real `gh`
commands, because that is where the merge gate lives.

> **v1.1 — D16, D14:**
>
> - **Who may use the human-only commands.** Commands that exercise human
>   authority require the operator to be listed in `humans.yaml`: freeze,
>   reassign, set N, plan review and merge. Anyone else gets an error, and
>   nothing is written.
> - **Failing checks.** Merge refuses if the PR's status checks are failing,
>   or if the PR is no longer open.

## 10.3 Making local actions visible to everyone — the activity feed

Commands that matter to the rest of the team write into the coordination repo,
using the same naming convention as claims:

```yaml
# control/laptop-a-pause-503.yaml
human: joe
machine: laptop-a
action: pause
logical_clock: 503
```

Every Board, each cycle, turns new records into plain-English lines:

- "Joe paused their swarm (laptop-a)"
- "Priya's swarm claimed TASK-7 (laptop-p)"
- "Jane arbitrated TASK-3: laptop-b wins — 'branch touches shared config'"
- "Joe's swarm took over EPIC-14"

> **v1.1 — D24:** the feed also covers:
>
> - withdrawals, set-quota records, and completions (finished, merged, changes
>   requested, rejected, re-planned);
> - the daemon's notifications, prefixed `[swarm-board]`. For example: "claude
>   has finished TASK-3. The PR can be found at …", or "Still working on
>   TASK-5?".

## 10.4 Epic takeover, defined precisely

"Took over an epic" writes a `priority/` record scoped to an epic. Every
scheduler, when choosing among ready tasks, treats a task under an epic with an
active takeover from a *different* machine as lower priority. It claims such a
task only if it has no other ready work. The signal stays soft and revocable,
and a matching `release` record revokes it.

> **v1.1:** the machine holding the takeover ranks that epic's tasks first.

## 10.5 Read-only panels

- **Live claims:** task, owning machine and human, logical clock, claim age,
  worker, state. The state is flagged "needs human" or "pausing (quota)" when
  one applies.
- **Awaiting review:** tasks with an open PR, with live review and CI status.
- **Needs arbitration:** tasks flagged by the poller's thrash detection, plus
  live conflicts.
- **Quota gauge:** global N used of total, and this machine's own share.
- **Activity feed:** the running plain-English log from 10.3.

> **v1.1 — D20:**
>
> - A **Plans awaiting review** panel lists submitted plans that still need a
>   human decision.
> - A status line shows whether this machine is running, paused or stopped,
>   and the state of the other machines.

## 10.6 Attribution and trust

Every command the Board issues is tagged with the operator's entry in
`humans.yaml`, so the audit trail and the feed name a real person.

> **v1.1:**
>
> - **Which commands check.** Commands that exercise human authority refuse to
>   run for anyone not in `humans.yaml`: arbitration, quota, plan review,
>   merge, `start`, and the `--apply` variants. Machine controls (pause,
>   resume, throttle, stop, epic takeover) record whatever operator name
>   resolves, which can be `unknown`.
> - **Operator resolution order:**
>   1. `human:` in `.swarm/local.yaml`;
>   2. `DAGS_HUMAN`;
>   3. the cached name in `.swarm/operator` (delete it if your login changes);
>   4. `gh api user`, matched against the `github:` field in `humans.yaml`,
>      then cached.

## 10.7 Choosing a worker, worked example

```text
[swarm-board] I have claimed TASK-3 for completion, who is my worker?
  a) claude
  b) IntelliJ + Human
  c) VSCode + Human
> a
[swarm-board] Ok, you have selected claude. Handing over TASK-3 to it —
              when done, it will announce with the PR link here.
...opens claude in a new terminal, in TASK-3's worktree, .swarm-task/ injected...
...the Board and poller watch for the output contract (Ch.7.3)...
...contract fulfilled: rm -rf .swarm-task/ (never tracked, no commit needed)...
[swarm-board] claude has finished TASK-3. The PR can be found at
              https://github.com/org/matchwire-backend/pull/42
```

Choosing `c` instead opens VS Code on the worktree with
`.swarm-task/README.md` already open; the second half of the transcript is
identical. The Board doesn't know or care whether claude or a human produced
the PR — only that the output contract was fulfilled.

> **v1.1:**
>
> - **When it appears.** The prompt pops up automatically for each task this
>   machine is holding for a worker choice.
> - **Postponing.** `Esc` postpones it; `w` brings it back.
> - **Fixed letters.** The letters always mean a = claude, b = IntelliJ,
>   c = VSCode. For `human-must-scope` tasks the claude option is not offered.


# 11. Gluing It Together — The End-to-End Flow

A single task's lifecycle, drawing on every chapter in order:

1. **A human plans the work.** They decompose an epic into tasks and
   dependencies in the issue backend (Ch.3). A matching task file appears in
   the coordination repo.
2. **A machine joins.** It runs `./bin/swarm.py start` once (Ch.5), which
   starts its scheduler and poller.
3. **A task becomes eligible.** The scheduler (Ch.7) syncs and finds a task
   that is Ready (dependencies Done, no freeze) and within both the global N
   and this machine's share.
4. **The machine claims it.** It computes the next logical clock and writes a
   claim (Ch.6.2).
   - A machine that loses the race detects it via `resolve()` and withdraws.
   - If the same pair keeps racing, the poller flags the task for arbitration.
5. **A worker takes over.** The winner hands the task to a worker (Ch.7.3) in
   an isolated branch and worktree: the default worker, or a human's choice on
   the Board. Heartbeats and `checkpoint.yaml` are updated throughout, so any
   machine can resume the task.
6. **The PR is opened.** The worker runs `swarm-task done`: PR opened with the
   template, completion written, ticket moved to Awaiting Review, quota slot
   released.
7. **A human reviews.** Every poller surfaces the review-ready task with live
   `gh` status. The human either:
   - approves and merges (one key on the Board);
   - requests changes (the task resumes from the checkpoint);
   - or rejects the approach (PR closed, re-planning in the backend).
8. **Dependants start.** Downstream tasks become Ready as soon as the merge is
   recorded, and the cycle repeats on whichever machine picks one up.

No step needs a process that isn't one of these:

- an agent invoked on demand;
- a human using the issue backend or GitHub normally;
- a local script (`swarm.py`, `poll.py`, `board.py`) on a machine a human
  already controls.

> **v1.1 — D11:** in step 1 the task file is `tasks/<EPIC>/<TASK>/meta.yaml`,
> created by **plan sync** from the tracker. Nobody writes it by hand.


# 12. Limitations and Operating Boundaries

- **Latency, not real-time.** Every decision is only as fresh as the last sync;
  notification latency equals each human's poll interval. Fine for hour-scale
  tasks, unsuitable for sub-minute churn.
- **Local correctness, not global optimality.** No duplicate work is
  guaranteed; perfectly balanced load is not.
- **Task granularity matters.** Size tasks coarse enough (roughly hour-scale)
  that sync and resolution latency never dominate the work.
- **GitHub's push ordering is the one implicit serialization point.** The
  remote's atomic accept/reject of pushes is what the leaderless scheme leans
  on.
- **Visibility requires someone's poller to be running.** Without one,
  conflicts and review-ready work still resolve correctly in the ledger, but
  nobody is notified until someone looks.
- **Human review remains the true rate limiter.** Nothing reaches a protected
  branch without a human decision.

> **v1.1 — D10, D3, D16, D4:** boundaries of the first implementation:
>
> - **One repo per task.** Cross-repo work is split into tasks linked by
>   dependencies.
> - **macOS-only worker launchers.**
> - **Arbitration trust is by name only.** The commit-author check is not built yet (K4, Appendix A).
> - **No Jira adapter yet.**
> - **Branch protection needs a public repo** (or a paid plan). On a private
>   repo with GitHub Free, "agents can never merge" rests only on workers
>   following the skill.


# Appendix A. Decisions log (v1.0 → v1.1)

"Plan §" refers to `claude/DAGS-implementation-plan.md` in the DAGS project.
**Status** describes the implementation as of 2026-09-16.

| # | Decision | Why | Chapters | Status |
|--|------------------------------------------|-------------------------|------|------|
| D1 | All DAGS code lives in the coordination repo's `bin/` (plan §2.15). | "Installing the protocol is cloning the repo" (Ch.4). | 2, 4 | Done |
| D2 | Worker PRs are opened by a separate **bot account**. Its token comes from `DAGS_WORKER_GH_TOKEN` if set, else the macOS Keychain (`dags-worker-token`); `worker_token: none` opts out. Humans approve and merge with their own `gh auth`. Commits are authored as the bot if `bot:` is set (plan §2.1). | GitHub won't let a PR's author approve it. With one shared identity, the review rule blocks every merge, or admins bypass it and the "agents never merge" guarantee is gone. | 7.3, 9.1 | Done |
| D3 | Worker launchers are **macOS only**: Terminal/iTerm via `osascript`, `open -na` for IntelliJ, `code -n` for VSCode (plan §2.14). | The POC runs on one Mac; the Worker port leaves room for other systems. | 1, 7.3 | Done |
| D4 | The **Jira adapter is deferred** (`FutureWork.md`). | No Jira site to build and test against, and the API is changing (search, Epic Link, ADF). | 3.2 | Deferred |
| D5 | Two builders share the repo with a `BATON` file: Claude in Cowork writes code, Claude Code on the Mac runs what needs the Mac. Hand-offs are local commits. | Cowork's sandboxes can't install typer/rich/textual or reach GitHub. This is a development arrangement, not part of the protocol. | — | Done |
| D6 | Leases use skew-corrected wall-clock time: offset measured from GitHub's `Date` header; lease 15 min; heartbeat every 3 min (plan §2.3). | A Lamport clock orders events but can't measure 15 minutes. Purely logical leases fail when only one machine is active. | 6.5 | Done |
| D7 | Heartbeats (`heartbeats/<machine>.yaml`) and `checkpoint.yaml` have a single writer, which re-checks `resolve()`. Heartbeats go in one commit per cycle. The retry count is computed. `meta.yaml` never changes; changes go to `meta/` revisions (plan §2.4). | Keeps "append-only, no textual conflicts" true where v1.0 implied rewriting files in place. | 4, 8 | Done |
| D8 | `start` spawns a **detached daemon** (scheduler, heartbeat and poller threads) with a pidfile and log. The **Board is a separate process** (`board` or `start --attach`). `stop` = shared stop record + SIGTERM (plan §2.2). | Threads die with their process, and a terminal UI can't run inside a headless daemon. | 5.3, 10.1 | Done |
| D9 | Global N lives in `quota/` records; latest clock from a recognised human wins; default `swarm.default_quota` (plan §2.5). | v1.0 called N "time-varying" but didn't say where it lives. | 7.1 | Done |
| D10 | A task's code repo is its `repo:` label, else `epic_repos:`, else `default_repo:`. Epics have none; one repo per task; `repos:` holds `base` and `test_command` (plan §2.6). | Neither tracker has a field for the target repo, and v1.0 doesn't say how `meta.yaml` gets one. | 3.3, 3.4 | Done |
| D11 | **Plan sync** mirrors the tracker into `tasks/` every cycle. Done = PR merged (or closed by hand in the tracker). Readiness = backend not holding it back **and** ledger checks (plan §2.7). | v1.0 doesn't say who creates task files or who marks a task Done. | 7.2, 9.1, 11 | Done |
| D12 | **Autonomy tiers:** `auto-pr` self-approves its plan; `human-must-review` needs a human plan approval; `human-must-scope` is never given to an AI worker. After `max_retries` failed claims, the tier drops one step (plan §2.8). | v1.0 names the tiers but doesn't define their behaviour. | 7.3, 8, 9.2 | Done |
| D13 | **Idle limit:** no progress for `human_idle_hours` → "still working?"; no answer within one lease → heartbeats stop. Applies to every worker type (plan §2.9 said humans only). | The daemon heartbeats on the worker's behalf, so an abandoned IDE or a dead terminal would otherwise hold the lease forever. | 8 | Done |
| D14 | **Local tests instead of CI:** `done` runs the repo's `test_command` and refuses on failure. Merge refuses only if the PR's status checks (read with `gh pr view`) are *failing* (plan §2.10). | There's no hosted CI (Ch.1), so a PR usually has no checks at all. | 7.3, 9.1 | Done |
| D15 | **Command corrections** (plan §2.11). | The v1.0 commands don't work as written. | 3.3, 5.5, 10.1 | Done |
| D16 | **Arbitration trust:** records from names not in `humans.yaml` are ignored. The commit-author email check is **not built yet** (plan §2.12). | Anyone with push access can write a record naming any human. | 6.6, 10.2 | Partly done |
| D17 | Worktrees at `.worktrees/<TASK>` on branch `swarm/<TASK>`. Machine state in `.swarm/` (both git-ignored). Code repos: a path mapped in `.swarm/local.yaml`, else `.swarm/repos/OWNER/NAME` (plan §2.13). | v1.0's `cp` commands imply these locations. The mapping lets a human reuse an existing checkout. | 5.3, 7.3 | Done |
| D18 | GitHub reads use one paginated `gh api graphql` query. Without type labels, an issue with sub-issues counts as an epic. | Doesn't depend on which `--json` fields a `gh` release supports; one call per sync. | 3.3 | Done |
| D19 | GitHub task keys are `OWNER/REPO#N`, short key `GH-N`. Ledger path `tasks/<EPIC>/<TASK>`, epics at `…/_epic`, loose tasks at `tasks/_no-epic/`. Commands accept the full key, the short key or the task's folder name. | Human-friendly names on the Board and in branches, while keeping the permanent reference. | 3.3, 4 | Done |
| D20 | **Plan gate records:** the plan text and its hash live in `checkpoint.yaml`; human approvals are append-only `plan-reviews/` records; a changed plan needs a new approval. | The checkpoint has a single writer, but the approving human may be on another machine. | 7.3, 10.5 | Done |
| D21 | The **skill is stdlib-only** and delegates to `swarm.py task …`. It is configured by `.swarm-task/context.json` (the plan said `context.yaml`). | It must run in any worktree without the venv on its path. | 7.3 | Done |
| D22 | **Outcome records** (`completions/`): `pr-opened`, `done`, `reopened` (once per review), `rejected` (PR closed → ticket `blocked`), `replanned` (ticket set back to `ready`). | Makes the request-changes and reject-approach paths of Ch.9 computable from the ledger. | 9.2, 9.3 | Done |
| D23 | **Venv:** self-installing, stamped by platform and Python version, rebuilt if it came from elsewhere. `typer>=0.16`; works whether typer bundles its own click (0.17+) or uses the real package. | Folders shared with VMs can hold a venv built for the wrong system, and older typer breaks with current click. | 5.2 | Done |
| D24 | **Notifications** always go to `.swarm/notifications.log`; desktop and webhook are opt-in in `.swarm/local.yaml`. The Board shows the log in its feed. | Ch.9.4's notify step is pluggable; the log is the one channel that always works. | 9.4, 10.3 | Done |
| D25 | **Plan seeding:** `swarm.py backend seed FILE` creates a YAML plan's labels, issues, parents and "blocked by" links. Dry run by default; `--apply` is for humans. Issues carry a `dags-seed` marker, so re-runs only add what is missing and never rewrite. GitHub only. | The Phase 9 plan has 21 issues and 47 links. Typing them by hand is error-prone, and a failed run must be safe to repeat. | 3.3 | Done (first real run pending) |

**D15 corrections in full:**

- `gh issue edit … --add-blocked-by`; the prerequisite check verifies the flag exists.
- Branch protection is set with a full JSON body through `swarm.py protect`.
- `textual serve` comes from `textual-dev`, installed on demand, on port 4590.
- `start --attach` replaces `swarm.py --attach`.

**Known gaps** (found while checking this spec against the code)

| # | Gap | Status |
|--|--------------------------------------------|--------------------|
| K1 | Merging with the Board's `m` or `task merge` left the task's worktree on disk. | **Fixed:** merge removes it, and every poller sweeps worktrees of done or rejected tasks. |
| K2 | Lowering N or a share didn't pause running tasks (v1.0 Ch.8 says they are checkpointed). | **Fixed:** pause request, then a `quota` release one lease later (Ch.8 note). |
| K3 | `--identity` wasn't remembered between commands. | **Fixed:** it names a fresh clone for good; `swarm.py identity show/set`. |
| K4 | The D16 commit-author check isn't built. | **Open.** Arbitration is trusted by name only; review arbitration commits in git history. |

**Open questions**

- Are the MatchWire repos public or private? This decides whether branch
  protection works on GitHub Free.
- Whether to build K4.


# Appendix B. Primer — DAGS in ten minutes

## B.1 What DAGS is

DAGS lets several machines, each running AI coding agents or humans in their
IDEs, work through a shared plan of tasks without a central server. Three
things make that work:

- **The plan** lives in your issue tracker (GitHub Issues today). Humans write
  it: epics, tasks, dependencies, and how much autonomy each task gets.
- **The ledger** is a plain git repo, the *coordination repo*. Machines write
  small, uniquely named YAML files into it: "I claim this task", "still alive",
  "PR opened". Every machine reads the same files and computes the same
  answer to "who owns what".
- **The code** lives in normal GitHub repos. Workers open pull requests there,
  and only humans merge them.

## B.2 Vocabulary

| Term | Meaning |
|---|---|
| Coordination repo | The git repo holding the ledger and the DAGS scripts (`bin/`). Every participant clones it. |
| Code repo | A repo where the actual work lands (e.g. `matchwire-backend`). |
| Machine | One running swarm, identified by a name like `mbp-jane-7f2a`. |
| Operator | The human attending a machine, as listed in `humans.yaml`. |
| Task / epic | Tracker issues. A task is hour-sized work in one code repo; an epic groups tasks. |
| Claim | A machine's bid for a task. The lowest logical clock wins unless a human arbitrates. |
| Logical clock | A counter, one more than the highest number anywhere in the ledger. It orders events without trusting wall clocks. |
| Lease / heartbeat | A claim stays valid only while its machine keeps writing heartbeats (every 3 minutes; it expires after 15). |
| Worker | Whatever implements a claimed task: `claude` in a terminal, or a human in IntelliJ or VSCode. |
| Worktree | A private checkout of the code repo for one task (`.worktrees/<TASK>`, branch `swarm/<TASK>`). |
| Skill | The `.swarm-task/` folder dropped into the worktree: instructions plus the `swarm-task` command. |
| Checkpoint | `checkpoint.yaml`: the plan, what was tried, what remains, open questions. It is how any machine resumes a task. |
| Quota N | The maximum number of tasks worked at once across the whole swarm. Each machine also has its own **quota share**. |
| Arbitration | A human's file that overrides who owns a task, or freezes it. |
| Board | The terminal dashboard where humans watch and steer. |
| Daemon | The background process a machine runs: scheduler, heartbeats, poller. |

## B.3 The life of a task

```text
tracker issue ──plan sync──▶ tasks/E/T/meta.yaml           (open, ready)
      scheduler claims ─────▶ claims/<machine>-<clock>.yaml (claimed)
      worker chosen ────────▶ checkpoint.yaml worker: …     (in-progress)
      swarm-task plan / implement / done
      PR opened by bot ─────▶ completions/…-pr-opened.yaml  (awaiting-review)
      human merges ─────────▶ completions/…-done.yaml       (done) → dependants ready
         ├─ changes requested → reopened → back to ready, resumed from the checkpoint
         └─ PR closed         → rejected → re-plan in the tracker → ready again
```

Other states you may see:

- `frozen`: a human froze the task.
- `rejected`: waiting for re-planning.
- `arbitrated-stale`: a human awarded the task to a claim whose lease has
  since expired. Reassign it, or unfreeze.

## B.4 Who does what

| Actor | Does | Never does |
|---|---|---|
| Human planner | Writes epics and tasks in the tracker; sets labels | Edits `tasks/` by hand |
| Scheduler (per machine) | Claims ready tasks within quota; prepares worktrees; dispatches workers | Writes code; merges |
| Worker | Plans, implements, runs `swarm-task done` | Merges; edits the ledger directly |
| Poller (per human) | Notices review-ready, stale and conflicting work; records merges and rejections | Makes judgment calls |
| Human reviewer | Approves plans; reviews, merges or rejects PRs; arbitrates | — |

## B.5 Where things live

| What | Where |
|---|---|
| The plan | Tracker issues (labels `swarm:status:*`, `swarm:autonomy:*`, `repo:*`, `type:*`) |
| Shared settings | `backend.yaml`, `humans.yaml`, `CONVENTIONS.md`, `templates/` in the coordination repo |
| Your machine's settings | `.swarm/local.yaml` (never committed) |
| Ledger records | `tasks/`, `control/`, `priority/`, `quota/` |
| Your machine's state | `.swarm/`: venv, identity, daemon pid and log, notifications, cloned repos |
| Work in progress | `.worktrees/<TASK>` on branch `swarm/<TASK>` |
| Finished work | A PR in the task's code repo |

## B.6 Five rules worth remembering

1. **Edit the plan in the tracker, never in `tasks/`.**
2. **Only humans merge.** The bot opens PRs; branch protection enforces the rest.
3. **When in doubt, freeze.** `f` on the Board stops every machine from
   touching a task until you lift it.
4. **Tag your review comments** `fix:`, `explain:` or `reject-approach:` so the
   next worker can act on them.
5. **Workers that are stuck should say so** (`swarm-task block "…"`), not guess.


# Appendix C. How-to guides

Commands run from the coordination repo clone unless stated otherwise.
`./bin/swarm.py` bootstraps its own venv on first use.

## C.1 Set up a new swarm (once per team)

1. **Create the repos.**
   - The coordination repo, e.g. `org/project-swarm-ensemble`. Copy in `bin/`,
     `templates/`, `backend.yaml`, `humans.yaml`, `CONVENTIONS.md` and
     `.gitignore` from `poc-swarm-ensemble`.
   - The plan repo that will hold the issues, e.g. `org/project-swarm`.
2. **Edit `backend.yaml`.**
   - `github.repo`: the plan repo.
   - `repos:`: every code repo, each with `base` and `test_command`.
   - `default_repo`.
   - Adjust the `swarm:` settings if needed.
   - On an organization with issue types configured, set
     `use_issue_types: true`.
3. **Edit `humans.yaml`.** Add one entry per person who may arbitrate, set
   quota, approve plans or merge: `name`, `github` login, and the `emails`
   they commit with.
4. **Create the labels.** First check the list, then apply:

   ```bash
   ./bin/swarm.py backend init            # review the list
   ./bin/swarm.py backend init --apply    # create/update them
   ```

5. **Create the bot account.**
   1. Make a separate GitHub user, e.g. `project-dags-bot`.
   2. Add it as a collaborator with **Write** access to every code repo, and
      accept the invitation as the bot.
   3. Create a token for it. On personal-account repos use a classic token with
      `repo` scope. On organization repos, a fine-grained token with Contents,
      Pull requests and Issues set to read/write.
6. **Protect the base branch** of every code repo. Remember that GitHub Free
   only enforces this on public repos.

   ```bash
   ./bin/swarm.py protect org/project-backend            # dry run: shows the call
   ./bin/swarm.py protect org/project-backend --apply
   ```

7. **Commit and push** the coordination repo.

## C.2 Join the swarm from a new machine

1. Install the prerequisites: Python 3.10+, git, and gh. On macOS:
   `brew install python@3.12 gh`, then `gh auth login`.
2. Clone the coordination repo and `cd` into it.
3. Store the bot token in the Keychain. It prompts for the token, which stays
   off the command line:

   ```bash
   security add-generic-password -a dags-bot -s dags-worker-token -w
   ```

4. Create `.swarm/local.yaml` with anything machine-specific (all keys are
   optional; see D.4):

   ```yaml
   human: jane                      # if your GitHub login isn't in humans.yaml
   repos:                           # reuse checkouts you already have
     org/project-backend: ~/code/project-backend
   bot:
     login: project-dags-bot
     email: 12345+project-dags-bot@users.noreply.github.com
   notify:
     desktop: true
   ```

5. Start:

   ```bash
   ./bin/swarm.py start --quota-share 2                          # ask me which worker each time
   ./bin/swarm.py start --quota-share 2 --default-worker claude  # unattended AI worker
   ./bin/swarm.py start --attach                                 # and open the Board
   ```

6. Check at any time with `./bin/swarm.py status`. Stop with
   `./bin/swarm.py stop`.

## C.3 Write a plan in GitHub Issues

```bash
R=org/project-swarm
gh issue create --repo $R --title "E1: sports-data ingestion" --label type:epic
gh issue create --repo $R --title "Poll the feed" --parent $R#1 \
  --label type:task --label repo:org/project-backend \
  --label swarm:status:ready --label swarm:autonomy:human-must-review \
  --body "What to build, acceptance criteria, pointers."
gh issue create --repo $R --title "Show live scores" --parent $R#1 \
  --label type:task --label repo:org/project-frontend --label swarm:status:ready
gh issue edit 3 --repo $R --add-blocked-by $R#2      # frontend waits for backend
```

- **Omitted labels.** No autonomy label means `human-must-review`. No `repo:`
  label means `epic_repos` / `default_repo` applies.
- **Sizing.** Keep each task to about an hour of work in **one** repo.
- **Check.** `./bin/swarm.py plan sync` then `./bin/swarm.py task list` shows
  what the swarm sees. `(ready)` means the ledger's checks pass;
  `./bin/swarm.py backend ready --ledger` lists the tasks the tracker also
  holds ready, which is exactly what a scheduler may claim.
- **Web UI.** Sub-issues and "blocked by" can also be set in GitHub's web UI.

**From a plan file (D25).** A whole plan can be written as one YAML file and
seeded in one go. `poc/matchwire/plan.yaml` is a worked example.

```yaml
repo: org/project-swarm          # must match backend.yaml github.repo
status: ready                    # status label for new tasks
epics:
  - {id: BE, title: "Backend", body: "..."}
tasks:
  - id: T1
    epic: BE
    title: "Poll the feed"
    repo: org/project-backend    # must be listed under repos: in backend.yaml
    autonomy: human-must-review  # the default
    depends_on: []
    body: |
      What to build, acceptance criteria, pointers.
```

```bash
./bin/swarm.py backend seed plan.yaml            # dry run: what would change
./bin/swarm.py backend seed plan.yaml --apply    # humans only; asks first (--yes skips)
./bin/swarm.py plan sync
```

- **Idempotent.** Each issue gets a hidden `<!-- dags-seed: ID -->` marker.
  A re-run creates only the missing labels and issues, and adds only the
  missing parent and "blocked by" links.
- **Never rewrites.** Existing titles, bodies and labels are left alone.
  Differences, and links the plan doesn't list, are reported as notes.
- **Interrupted runs.** If a run stops part-way, run the same command again.
  After applying, the command re-reads the tracker and fails if anything
  still differs.

## C.4 Pick a worker for a claimed task

- **On the Board:** the prompt appears by itself. Press `a` (claude),
  `b` (IntelliJ + Human) or `c` (VSCode + Human). Press `Esc` to postpone and
  `w` to bring it back.
- **From the shell:** `./bin/swarm.py task worker GH-7 c`.

## C.5 Work a task yourself (IntelliJ or VSCode)

The IDE opens on `.worktrees/GH-7` with `.swarm-task/README.md` open. In the
IDE's terminal, from the worktree root:

```bash
.swarm-task/swarm-task plan                 # writes .swarm-task/plan.md; edit it
.swarm-task/swarm-task plan --submit        # auto-pr: approved; otherwise wait for a human
.swarm-task/swarm-task status               # shows when the plan is approved
.swarm-task/swarm-task implement            # prints the plan, checkpoint, reviewer feedback
.swarm-task/swarm-task note --tried "cached client" --remaining "parser" "tests"
.swarm-task/swarm-task note --summary "Adds a 60s feed poller" --risk "rate limits"
.swarm-task/swarm-task done                 # tests → commit → push → PR (by the bot)
```

If you need a decision, run `.swarm-task/swarm-task block "question"` and stop.
If you're busy but not stuck when asked "still working?", answer with
`./bin/swarm.py task still-working GH-7` (from the coordination repo). To give
a task back: `./bin/swarm.py task release GH-7`.

## C.6 Review a plan

- **On the Board:** select the task in *Plans awaiting review*, press `v`, then
  **Approve** or **Request changes**.
- **From the shell:**

  ```bash
  ./bin/swarm.py task show GH-7              # plan status, checkpoint
  ./bin/swarm.py task approve-plan GH-7
  ./bin/swarm.py task approve-plan GH-7 --reject --note "use the push feed"
  ```

The plan is also posted as a comment on the ticket.

## C.7 Review, merge, request changes, or reject a PR

- **Merge:** select the task in *Awaiting review* and press `m`, or run
  `./bin/swarm.py task merge GH-7`. This approves and squash-merges under your
  own GitHub account, then records Done. Merging in the GitHub web UI works
  too; the poller records it.
- **Request changes** with tagged lines:

  ```bash
  gh pr review 42 --repo org/project-backend --request-changes \
    -b $'fix: handle cancelled matches\nexplain: why a second cache?'
  ```

  The task goes back to the queue and resumes from its checkpoint. The next
  `done` updates the same PR.
- **Reject the approach:**
  1. `gh pr close 42 --repo org/project-backend`. The ticket becomes `blocked`.
  2. Rewrite the ticket and add the lesson to `CONVENTIONS.md`.
  3. Set `swarm:status:ready` again.

## C.8 Control machines and the quota

| Goal | Board | Shell |
|---|---|---|
| Stop claiming new work here | `p` | `./bin/swarm.py pause` |
| Resume | `r` | `./bin/swarm.py resume` |
| Change this machine's share | `t` | `./bin/swarm.py throttle 1` |
| Pause another machine | — | `./bin/swarm.py pause --machine mbp-joe-1a2b` |
| Stop this machine | `s` | `./bin/swarm.py stop` |
| Change global N | `n` | `./bin/swarm.py quota set 4 --reason "budget"` |
| Halt a whole epic | — | Set `swarm:status:blocked` on the epic in the tracker |

## C.9 Settle conflicts

- **Freeze a task:** `f`, or
  `./bin/swarm.py task freeze GH-7 --reason "spec changing"`. Nobody may own it
  until you lift the freeze.
- **Unfreeze:** `f` again, or `./bin/swarm.py task unfreeze GH-7`.
- **Award a task:** `a` in *Needs arbitration*, or
  `./bin/swarm.py task reassign GH-7 laptop-b --reason "…"`. A machine name
  picks that machine's latest claim.
- **Give one machine priority on an epic:** `e`, or
  `./bin/swarm.py epic takeover GH-1`. Undo with `e` or `epic release`.

## C.10 Run two "machines" on one computer (the POC set-up)

```bash
git clone https://github.com/org/project-swarm-ensemble ~/dags/mac-a
git clone https://github.com/org/project-swarm-ensemble ~/dags/mac-b
cd ~/dags/mac-a && ./bin/swarm.py --identity mac-a start --quota-share 2
cd ~/dags/mac-b && ./bin/swarm.py --identity mac-b start --quota-share 2
./bin/swarm.py quota set 3
./bin/swarm.py --identity mac-a board        # one Board per clone
```

On a fresh clone, `--identity` is remembered in `.swarm/identity`, so later
commands in that clone need no flag. On a clone that already has a name, the
flag applies to that one command only, with a warning. Use
`./bin/swarm.py identity set mac-a` to rename a clone; it refuses while the
old name still holds live claims. Both clones can map the same code repos in
their `.swarm/local.yaml`; worktrees are separate per clone.

## C.11 Troubleshooting

| Symptom | Fix |
|---|---|
| `'x' is not listed in humans.yaml` | Add yourself to `humans.yaml`, or set `human:` in `.swarm/local.yaml`. |
| `prerequisites missing: gh features` | `brew upgrade gh`: `gh issue edit` needs `--add-blocked-by`. |
| `no worker (bot) GitHub token found` | Store it (C.2 step 3), or export `DAGS_WORKER_GH_TOKEN`. |
| `'swarm:status:…' not found … backend init` (in `.swarm/swarm.log`) | `./bin/swarm.py backend init --apply`. |
| `backend.yaml github.repo must be OWNER/NAME` | Replace the `OWNER/…` placeholders. |
| `.swarm/venv looks broken` | Delete `.swarm/venv` and run again. Manual install is in Ch.5.2. |
| `the plan isn't approved yet` | Submit the plan (`plan --submit`) and get it approved (C.6). |
| `tests failed … not opening a PR` | Fix the tests; `done` is safe to run again. |
| `no changes to submit` | Nothing was changed or committed on `swarm/<TASK>`. |
| `this machine (…) does not own …` | You lost the task (race, freeze or lease). Stop; see `task show`. |
| A task sits in *claimed, awaiting worker* | Choose a worker (C.4) or release it. |
| A task never becomes ready | `task show`: look at `ready`, dependencies and `arbitration`. `backend ready --ledger`: is the tracker holding it back? Does it have a target repo? |
| Nothing happens at all | `status`: is the daemon running? Read `.swarm/swarm.log` and `.swarm/notifications.log`. Set `DAGS_DEBUG=1` for tracebacks. |
| Branch protection "set" but merges still allowed | The repo is private on GitHub Free (Ch.5.5). |


# Appendix D. Reference

## D.1 Commands (`./bin/swarm.py`)

Global options go **before** the command: `--root PATH`, `--identity NAME`,
`-v`.

| Command | Purpose |
|---|---|
| `start [--quota-share N] [--poll-interval 60s] [--cycle-interval 30s] [--default-worker claude\|intellij\|vscode] [--attach] [--no-poller]` | Join the swarm and start the daemon. `--quota-share` defaults to 1 |
| `stop [--wait S]` | Stop this machine's daemon (stop record + SIGTERM) |
| `pause` / `resume [--machine M]` | Stop or resume claiming |
| `throttle N [--machine M]` | Set the quota share live |
| `status [--json]` | Status panel |
| `board [--web] [--port 4590]` | Swarm Board |
| `protect REPO [--branch main] [--approvals 1] [--apply]` | Branch protection (dry run by default) |
| `plan sync` | Mirror the tracker into `tasks/` |
| `backend get-task T` / `backend ready [--ledger]` / `backend set-status T S` / `backend init [--apply]` | Talk to the tracker |
| `backend seed FILE [--apply] [--yes]` | Create a plan file's epics, tasks and links in the tracker (dry run by default) |
| `quota set N [--reason]` / `quota show` | Global N |
| `epic takeover E` / `epic release E` | Soft epic priority |
| `identity show` / `identity set NAME [--force]` | Show or rename this clone's machine identity |
| `task list [--all]` / `task show T` | Inspect tasks |
| `task worker T a\|b\|c` | Choose a worker |
| `task freeze T --reason` / `task unfreeze T` / `task reassign T WINNER --reason` | Arbitration |
| `task approve-plan T [--reject] [--note]` | Plan review |
| `task merge T [--force]` | Approve & merge |
| `task open T [--pr]` | Open the ticket or PR |
| `task release T` / `task still-working T` | Give a task back / answer the idle prompt |
| `task context\|note\|submit-plan\|block\|done …` | Used by the skill |

**Other entry points:**

- `.swarm/venv/bin/python bin/poll.py [--once] [--interval S] [--identity M]`:
  standalone poller.
- `.swarm/venv/bin/python bin/board.py [--root] [--identity] [--refresh S]`:
  the Board.
- `bin/dev-setup.sh [pytest args]`: developer venv and test suite.

**The skill:** `.swarm-task/swarm-task plan [--submit] [--force] | status |
implement | note [--summary] [--tried …] [--remaining …] [--question …]
[--risk …] | block "Q" | done`.

## D.2 `backend.yaml`

| Key | Meaning | Default |
|---|---|---|
| `backend` | `github` (or `fake` for demos; `jira` deferred) | — |
| `github.repo` | Plan repo `OWNER/NAME` | — |
| `github.use_issue_types` | Use GitHub issue types instead of `type:` labels | `false` |
| `github.cache_seconds` | How long an issue listing is reused | `20` |
| `fake.path` | YAML file backing the fake tracker | `fake-backend.yaml` |
| `repos.<OWNER/NAME>.base` | Base branch for worktrees and PRs | `main` |
| `repos.<OWNER/NAME>.test_command` | Run by `done` before the PR | none |
| `default_repo` | Repo for tasks without a `repo:` label | none |
| `epic_repos.<epic key or short>` | Per-epic default repo | none |
| `swarm.default_quota` | Global N without a `quota/` record | `3` |
| `swarm.lease_minutes` / `heartbeat_minutes` | Lease window / heartbeat interval | `15` / `3` |
| `swarm.max_retries` | Failures before the autonomy downgrade | `3` |
| `swarm.human_idle_hours` | Idle limit | `8` |
| `swarm.thrash_threshold` | Conflicts before "needs arbitration" | `2` |

## D.3 `humans.yaml`

```yaml
humans:
  - name: jane                 # the name used in records and the feed
    github: jane-gh            # matched against `gh api user`
    emails: [jane@example.com] # commit author emails (for the D16 check)
```

## D.4 `.swarm/local.yaml` (per machine, never committed)

| Key | Meaning |
|---|---|
| `human` | Operator name, if your GitHub login isn't in `humans.yaml` |
| `repos.<OWNER/NAME>` | Path of an existing checkout to use |
| `worker_token` | `{keychain_service: dags-worker-token}` (default), `{env: VAR}`, or `none` |
| `bot.login`, `bot.email` | Author identity for worker commits |
| `terminal_app` | `Terminal` (default) or `iTerm` |
| `claude_bin`, `claude_args` | How the claude worker is launched |
| `intellij_app` | e.g. `IntelliJ IDEA CE` |
| `code_bin` | Path to VS Code's `code` |
| `notify.desktop`, `notify.webhook` | Extra notification channels |

**Environment variables:**

| Variable | Effect |
|---|---|
| `DAGS_NO_VENV=1` | Skip the venv bootstrap |
| `DAGS_DEV=1` | Include dev requirements in the venv |
| `DAGS_ROOT` | Coordination repo path (`swarm.py` only) |
| `DAGS_IDENTITY` | Machine identity (`swarm.py` only) |
| `DAGS_HUMAN` | Operator name |
| `DAGS_WORKER_GH_TOKEN` | Bot token |
| `DAGS_DEBUG=1` | Show full tracebacks |

## D.5 Labels (tracker)

| Label | Values |
|---|---|
| `swarm:status:` | `ready`, `claimed`, `in-progress`, `awaiting-review`, `blocked`, `done` |
| `swarm:autonomy:` | `auto-pr`, `human-must-review` (default), `human-must-scope` |
| `repo:` | `OWNER/NAME` of the target code repo |
| `type:` | `epic`, `task` (when issue types aren't used) |

## D.6 Ledger records

| Record | Key fields |
|---|---|
| `meta.yaml` | `key`, `short`, `title`, `epic`, `dependencies`, `autonomy`, `repo`, `is_epic`, `issue_url`, `coordination_ref` |
| `meta/<m>-<c>.yaml` | Changed fields only; `downgrades` after an autonomy downgrade |
| `claims/<m>-<c>.yaml` | `claim_id`, `task`, `machine`, `human`, `worker`, `logical_clock`, `wall_utc` |
| `withdrawals/<m>-<c>.yaml` | `claim_id`, `reason` (`lost-race`, `arbitration`, `released`, `quota`, or a failure), `winner` |
| `heartbeats/<m>.yaml` | `claim_id`, `logical_clock`, `wall_utc` |
| `checkpoint.yaml` | `claim_id`, `machine`, `worker`, `worker_label`, `branch`, `summary`, `tried`, `remaining`, `open_questions`, `risks`, `plan_md`, `plan_sha`, `plan_self_approved`, `needs_human`, `pause_requested`, `pr_url`, `previous_claims`, `dispatched_utc`, `finished_utc`, `human_confirmed_utc` |
| `arbitration/human-<n>-<c>.yaml` | `human`, `winner` (claim id or `none`), `reason`, optional `action: withdraw` |
| `plan-reviews/human-<n>-<c>.yaml` | `human`, `plan_sha`, `decision` (`approved` / `changes-requested`), `note` |
| `completions/<m>-<kind>-<c>.yaml` | `kind` (`pr-opened`, `done`, `reopened`, `rejected`, `replanned`), `pr_url`, plus: `claim_id`, `worker`, `commit` (pr-opened); `merged_by`, `merged_utc`, `imported`, `reason` (done); `review_id`, `reviewer` (reopened) |
| `control/<m>-<action>-<c>.yaml` | `machine`, `human`, `action` (`start`, `pause`, `resume`, `throttle`, `stop`), `quota_share`, `default_worker` (start) |
| `priority/<m>-<epic>-<action>-<c>.yaml` | `epic`, `machine`, `human`, `action` (`takeover` / `release`) |
| `quota/<human>-<c>.yaml` | `human`, `n`, `reason` |

(`<m>` = machine, `<c>` = logical clock, `<n>` = human name)

## D.7 Task states (derived, never stored)

| State | Meaning | Uses quota |
|---|---|---|
| `open` | No live claim. Marked `(ready)` when the ledger's checks pass (dependencies done, no arbitration in force, no open or rejected PR). The scheduler also needs the tracker to list it as ready: `backend ready --ledger` shows both | no |
| `claimed` | Won by a machine; no worker yet | yes |
| `in-progress` | Worker dispatched | yes |
| `awaiting-review` | PR open | no |
| `done` | PR merged (or closed in the tracker) | no |
| `frozen` | Arbitration `winner: none` | no |
| `rejected` | PR closed; waiting for re-planning | no |
| `arbitrated-stale` | Awarded to a claim whose lease has expired | no |

## D.8 Files on a machine

| Path | Content |
|---|---|
| `.swarm/venv/` | Python environment (`.dags-stamp.json` records what it was built for) |
| `.swarm/identity`, `.swarm/operator` | Cached machine name and operator |
| `.swarm/local.yaml` | Machine settings (D.4) |
| `.swarm/daemon.pid`, `.swarm/daemon.json`, `.swarm/swarm.log` | Daemon process ID, thread status, log |
| `.swarm/notifications.log` | Every notification |
| `.swarm/poller-state.json` | The poller's memory between cycles |
| `.swarm/git.lock` | Serialises git operations between processes |
| `.swarm/repos/OWNER/NAME` | Code repos cloned by DAGS |
| `.worktrees/<TASK>/` | Task worktrees; `.swarm-task/` inside while a worker is active. Removed once the task is done or rejected |
