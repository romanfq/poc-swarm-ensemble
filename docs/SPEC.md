---
title: "DAGS — Distributed AGent Swarm"
subtitle: "Human-governed execution on an issue backend and a GitHub coordination layer — Specification v2.0"
date: "2026-10-03"
---

# About this version

This is **version 2.0** of the DAGS specification. It describes the system as a
whole, in one voice, rather than as a set of changes to an earlier draft.

v1.0 was a design document written before anything was built. v1.1 kept v1.0's
text and attached a marked note wherever the build had changed the design. That
worked while the deltas were few. By the time there were twenty-seven of them the
notes were harder to read than the thing they annotated, and the base text had
started to describe a system that no longer existed — a Jira adapter that was
never written, commands without the flags they had grown, a decisions table that
stopped eleven decisions before the end.

So v2.0 is a rewrite. Three things follow from that:

- **It describes the target state, not a snapshot.** Where a behaviour is
  specified but not yet built, the chapter says so and cites the issue that will
  build it. Chapter 13 lists all of them in one place. Nothing here is
  aspirational by accident: if the text does not mark a behaviour as outstanding,
  it is in `main` and has a test.
- **The decisions survive as rationale, not as a diff.** Appendix A is the
  decisions log, D1–D33, each with the reason it was taken and the chapter it
  governs. It no longer pretends to be a changelog against v1.0.
- **Every factual claim was checked against the code**, not inherited from the
  previous draft. File and line references are to `main` at the time of writing.

The v1.0 whitepaper remains the origin document and is archived in the DAGS
project. v1.1 is superseded.

\newpage

# Contents

**Part I — What the system is**

1. Problem statement and design constraints
2. Architecture overview

**Part II — The three substrates**

3. The task graph — a pluggable issue backend
4. The coordination repository — git as the state store
5. Bootstrapping — one script to join the swarm

**Part III — Coordination**

6. The claim protocol — deterministic and leaderless
7. Quota, concurrency and multi-machine scheduling
8. Pause, resume and failure recovery

**Part IV — Humans**

9. Human control — review, merge and arbitration
10. The Swarm Board — a local command centre
11. Verification — tests, scope and continuous integration

**Part V — Operating it**

12. End-to-end flow
13. Specified but not yet built
14. Limitations and operating boundaries

**Appendices**

- A. Decisions log (D1–D33)
- B. Primer — DAGS in ten minutes
- C. How-to guides
- D. Reference

\newpage

# 1. Problem statement and design constraints

## 1.1 The problem

Several coding agents, on several machines, should be able to work a shared
backlog at the same time without a server to coordinate them, without two of them
doing the same task, and without a human losing the ability to say no.

The hard part is not making an agent write code. It is the three questions that
appear the moment there is more than one of them:

- **Who is working on what?** Two agents that claim the same ticket waste both
  their efforts and produce two conflicting branches.
- **How much is running at once?** An unbounded number of agents on one laptop is
  slower than one, and on several laptops it is a way to exhaust a quota nobody
  is watching.
- **Where does a human intervene?** An agent that cannot be stopped, redirected
  or overruled is not a tool.

## 1.2 Constraints

These are the constraints the design accepted, and they explain most of what
follows.

**No server.** There is no DAGS service, no database and no leader election. A
machine joins the swarm by cloning a git repository and running one script. This
is the constraint that shapes everything else: coordination state has to live
somewhere every machine can read and write with tools it already has.

**Nothing trusted to an agent's memory.** Every fact that matters — who claimed
what, when, what the plan was, who approved it — is a file in a repository.
An agent that dies mid-task loses nothing that another agent needs.

**Determinism over negotiation.** Machines do not talk to each other. Given the
same synced repository, every machine computes the same answer to "who owns this
task?" by the same pure function. Disagreement is impossible without divergent
state, and divergent state is a git problem with a git solution.

**A human is in the loop by construction, not by convention.** The gates are in
the protocol: a plan is reviewed before code is written, a pull request is merged
by a person, and a running swarm can be paused from any machine. An agent cannot
route around them, because the thing it must do to make progress — write a ledger
record — is the thing a human can refuse.

**Boring tools.** git, the GitHub CLI, Python's standard library and four
packages. No message queue, no scheduler daemon of its own beyond a local
process, no custom protocol on the wire. Every piece of swarm state can be read
with `cat` and fixed with `git revert`.

## 1.3 What DAGS is not

It is not a CI system: it does not run your builds, and since the swarm's own
verification now leans on CI (Chapter 11), it assumes one exists.

It is not an agent framework: it does not decide how an agent reasons, and it
makes no assumptions about which one you use beyond the worker port in §7.3.

It is not a replacement for review. The swarm's output is pull requests. Who
merges them, and on what evidence, is Chapter 9.

\newpage

# 2. Architecture overview

DAGS is three substrates and a protocol that connects them.

![The three substrates, and the three places a human enters.](figures/fig1-substrates.pdf){width=159mm}

| Substrate | What it holds | Who writes it |
|---|---|---|
| **The task graph** | What work exists, its shape and its permissions | Humans, through a tracker |
| **The coordination repository** | What is happening right now, and what happened | Machines, through `swarm.py` |
| **The code repositories** | The product | Workers, on branches, through pull requests |

The protocol is the rules by which a machine moves work from the first to the
third, recording every step in the second.

## 2.1 The three substrates

**The task graph** lives in an issue tracker — GitHub Issues today. Epics,
tasks, dependencies and permissions are issues and labels. A human's entire
interface for *what should be built* is the tracker: they file, order and label
there, and nothing requires them to learn the ledger. Chapter 3.

**The coordination repository** is a git repository holding append-only records:
claims, withdrawals, completions, plan reviews, arbitrations, machine control and
quota. It is the swarm's shared memory, and git's merge semantics are its
concurrency control. One coordination repository per swarm, holding no product
code (D27). Chapter 4.

**The code repositories** are whatever the work targets. A task names its repo;
the machine that owns the claim prepares a worktree on a branch named for the
task, and the worker changes code only there. Chapter 7.3.

## 2.2 What runs on a machine

![One machine: a single daemon, three loops, one local clone.](figures/fig2-loops.pdf){width=159mm}

One process, three loops, started by `swarm.py start` and detached as
`swarm.py _daemon` (`bin/dags/daemon.py`):

- **The scheduler**, every 30 seconds by default: pull, sync the plan, honour
  control records, tidy its own claims, claim ready work within quota, prepare a
  worktree, dispatch a worker.
- **The heartbeat**, every `min(heartbeat_s, lease_s / 3)`: prove that this
  machine still owns the claims it holds, and discover the ones it has lost.
- **The poller**, every 60 seconds: notice what changed, read pull request state
  from GitHub, and notify a human.

Plus two things a human runs directly: the **Swarm Board**, a terminal UI over
the same local clone (Chapter 10), and `swarm.py` itself for everything the Board
does not cover.

Nothing on a machine upgrades itself. A machine runs the protocol code it was
started with until a human stops it and starts it again (D33, §5.6).

## 2.3 The shape of the code

`bin/` is committed to the coordination repository and is what every machine
executes. The division that matters is between pure computation and effects:

| Layer | Modules | Rule |
|---|---|---|
| **Pure** | `resolve.py`, `snapshot.py`, `testscope.py`, `boardview.py`, `feed.py`, `panel.py` | No network, no writes. Time enters as an argument. Identical inputs give identical answers on every machine. |
| **Effects** | `ledger.py`, `gitsync.py`, `gh.py`, `worktree.py`, `repos.py` | One git transaction or one `gh` call per function. |
| **Policy** | `scheduler.py`, `work.py`, `actions.py`, `plan.py`, `poll.py` | Decides what to do, then calls the two layers above. |
| **Ports** | `backends/`, `workers/` | The two places a different implementation can be plugged in. |
| **Surfaces** | `cli.py`, `board.py`, `skill/swarm-task` | Argument parsing and presentation only. |

`resolve.py` is the heart of it: claim resolution, readiness and quota, as pure
functions over the ledger. Every machine and every surface answers "who owns
this?" by calling the same code on the same synced files, which is why there is
no leader.

## 2.4 Two ports, and why only two

**The issue backend** (`bin/backends/base.py`) is the boundary to a tracker.
Nothing outside `bin/backends/` talks to a tracker directly. Two adapters exist:
`github` and `fake` (file-backed, for tests and offline demos). A Jira adapter is
designed for but not written — see `FutureWork.md` and §3.5.

**The worker** (`bin/workers/base.py`) is the boundary to whatever implements a
task. A worker is an AI CLI in a terminal or a human in an IDE; the scheduler only
ever calls `dispatch`, and completion is detected from the output contract — a
ledger record and a pull request — never from the worker's process. That is what
lets a human and an agent be the same kind of thing to the scheduler. Three
adapters: `claude`, `intellij`, `vscode`, all macOS launchers (D3).

Everything else is deliberately not a port. There is one state store (git), one
clock scheme (§6.1), one notification path (§9.4). Pluggability was spent where
it buys something and refused where it would only buy indirection.

\newpage

# 3. The task graph — a pluggable issue backend

Humans describe work in a tracker. The swarm reads it and never invents work of
its own. This chapter is the contract between the two.

## 3.1 The port

`bin/backends/base.py` defines the interface. Nothing outside `bin/backends/`
may talk to a tracker, which is what makes the fake adapter a faithful stand-in
and a second adapter a contained piece of work.

The port's vocabulary is small and fixed:

```python
SWARM_STATUSES  = ("ready", "claimed", "in-progress",
                   "awaiting-review", "blocked", "done")
AUTONOMY_TIERS  = ("auto-pr", "human-must-review", "human-must-scope")
DEFAULT_AUTONOMY = "human-must-review"
PLAN_SCOPES     = ("labelled", "all")
DEFAULT_PLAN_SCOPE = "labelled"
```

A `Task` carries its ref, title, body, status, autonomy tier, target repo, epic,
dependencies and labels. A `TaskRef` is a backend-native identifier — an issue
reference like `owner/repo#7` for GitHub, a key like `MW-14` for a tracker that
uses them.

Two rules in the port rather than in any adapter, so every backend behaves the
same way:

**Readiness** (`ready_from`): a task is ready when it is open, is not an epic, is
not `blocked` or `done`, and its epic is not blocked. The last clause is the
"halt everything" lever of §9.2 — blocking an epic stops all its children without
touching them individually.

**Membership** (`split_plan`): see §3.4.

## 3.2 Labels are the human-facing contract

On GitHub the task graph is expressed in labels, and those labels are what a
human reads. Four families:

| Label | Meaning | Lifecycle |
|---|---|---|
| `swarm:status:<s>` | Where the task is | Changes as work proceeds |
| `swarm:autonomy:<t>` | How much review the task requires | Set by a human; may be lowered by the swarm (§8.3) |
| `repo:OWNER/NAME` | Which repository the task changes | Fixed |
| `type:epic` / `type:task` | What the thing is | Fixed |

**`swarm:status:ready` means "a human says this may be worked" — nothing more**
(D28). It is permission, not availability. Whether a task can be claimed *right
now* also depends on its dependencies, and that is computed in the ledger
(`resolve.deps_done`) and deliberately **not** written back to the tracker.

This is a decision with a visible cost: the tracker alone will not tell you what
the swarm will pick up next, because an issue can read `ready` while a dependency
is still open. The alternative — having `plan sync` write computed state back —
makes the tracker self-explanatory at the price of a write loop that can drift
when sync fails, and of many more API writes. The Board and `swarm.py backend
ready --ledger` answer the availability question instead.

The corollary, also D28: **labels that have stopped applying are retired.** When
a task is done, `swarm:autonomy:*` and `swarm:status:done` are removed — the
tier only governs the review gate while a task is being worked, and a closed
issue already shows its state. `type:` and `repo:` are lifecycle-independent and
stay. This is specified and not yet built (`#8`).

## 3.3 The GitHub adapter

`bin/backends/github.py` reads through one paginated GraphQL query issued by
`gh api graphql`, so that sub-issues (`parent`, `subIssues`), dependencies
(`blockedBy`) and issue types arrive together in a single call. That matters
because the `--json` fields exposed by `gh issue list` vary between gh releases,
while the GraphQL shape does not.

Writes are narrow: `_swap_label` adds a label and removes the stale ones sharing
its prefix, so a status change is one operation and cannot leave two statuses on
an issue. `set_status("done")` also closes the issue as completed.

Epics and tasks are distinguished by `type:epic` / `type:task` labels, or by
GitHub issue types where the repository has them configured
(`use_issue_types: true`).

## 3.4 Plan membership is a label, not an accident

Under `plan_scope: labelled`, the default, an issue is part of the plan when it
carries any `swarm:` label or a `type:` label, **or is an ancestor of an issue
that does** — so an epic seeded without a status is not dropped. Everything else
is invisible: not imported, not listed by `backend ready`, never claimed.

The alternative, `plan_scope: all`, treats every open issue as plan membership
and exists for a repository that is used only by the swarm.

The default is `labelled` because of a concrete failure. While the meta swarm's
plan lived in a repository that people also used, every stray issue became
claimable work with the default autonomy and the `default_repo` from
`backend.yaml` — a thought filed at midnight would have been work the next
morning, with a worker opening a pull request against a repository nobody meant
(D26).

`plan sync` reports what it skipped, so nothing goes missing silently.
`swarm.py backend adopt <issue>` adds the labels that bring one issue into the
plan, for the common case of filing something and then handing it over.

## 3.5 Selecting a backend, and the Jira question

`backend.yaml` at the coordination repository root names the adapter and carries
the settings every machine shares:

```yaml
backend: github                 # or: fake

github:
  repo: OWNER/plan-repo         # the repo whose issues are the plan
  use_issue_types: false        # true only where issue types are configured
  cache_seconds: 20

plan_scope: labelled            # or: all

repos:                          # code repositories tasks may target
  OWNER/backend:
    base: main
    test_command: ./mvnw -q verify
    checks_timeout: 300         # seconds `done` waits for CI (§11.3)
    worktrees: inside           # §7.3
default_repo: OWNER/backend

swarm:
  default_quota: 3              # global N (§7.1)
  lease_minutes: 15             # claim expiry (§6.5)
  heartbeat_minutes: 3
  max_retries: 3                # before autonomy is lowered (§8.3)
  human_idle_hours: 8
  thrash_threshold: 2           # conflict cycles before arbitration (§9.5)
```

**There is no Jira adapter.** v1.1 documented one, including a `swarm.yaml`
attachment format, and it was never written: `bin/backends/` contains `base`,
`fake` and `github`. The port exists and is deliberately tracker-agnostic — the
readiness and membership rules live in it precisely so a second adapter inherits
them — but Jira is future work (`FutureWork.md`), and this specification no
longer describes it as though it shipped.

## 3.6 Seeding a plan from a file

Typing a large plan into a tracker by hand is error-prone, and a half-finished
run must be safe to repeat. `swarm.py backend seed FILE` takes a YAML plan of
epics and tasks with their parents and dependencies, and creates the labels,
issues, parent links and "blocked by" links to match (D25).

It is idempotent by construction: each created issue carries a hidden marker
`<!-- dags-seed: ID -->` in its body, so a re-run creates only what is missing
and never rewrites what exists. It is a dry run unless given `--apply`, and
`--apply` requires a recognised human (`--yes` skips only the confirmation, not
the human check).

\newpage

# 4. The coordination repository — git as the state store

The ledger is a git repository. There is no database because there does not need
to be one: the problem is a small number of small facts that many writers append
and everyone must agree on, and that is what git already does well.

**One coordination repository per swarm, holding no product code** (D27). This
was learned the hard way — see §4.6.

## 4.1 Layout

```
tasks/<EPIC>/<TASK>/
    meta.yaml              the task as the tracker describes it
    meta/<machine>-<n>.yaml later revisions (autonomy, title, deps)
    claims/<machine>-<n>.yaml
    withdrawals/<machine>-<n>.yaml
    heartbeats/<machine>.yaml       single-writer, rewritten in place
    completions/<machine>-<kind>-<n>.yaml
    plan-reviews/<human>-<n>.yaml
    arbitration/<human>-<n>.yaml
    events/<machine>-<kind>-<n>.yaml
    test-scope/<...>.yaml
    checkpoint.yaml         single-writer, rewritten in place
control/<machine>-<action>-<n>.yaml
priority/<human>-<n>.yaml
quota/<human>-<n>.yaml
```

`<n>` is the logical clock (§6.1), which makes every filename unique and every
directory sortable into the order events happened.

## 4.2 Append-only, and the two exceptions

**The rule: records are new files, never edits** (`records.write_new`). Two
writers appending different files to the same directory cannot conflict, which is
what allows the sync in §4.3 to be as simple as it is.

**The exceptions are single-writer files**, rewritten in place
(`records.write_replace`): a machine's own `heartbeats/<machine>.yaml`, and
`checkpoint.yaml`, which the machine owning the claim is alone in writing. These
can conflict, and §4.3 says what happens when they do.

## 4.3 One transaction, and how conflicts resolve

![One ledger transaction, and the three layers of concurrency control.](figures/fig3-transaction.pdf){width=159mm}

Every write is `Coord.transaction` (`bin/dags/gitsync.py`): take the lock, pull
with rebase, run the function that writes the files, commit, push. The pull
happens before the write so that any logical clock the write computes has seen
everything synced so far.

Concurrency control is in three layers, because a single clone is shared by the
daemon's threads, the Board, and a worker's `swarm-task done`:

- an in-process re-entrant lock;
- an OS file lock on `.swarm/git.lock`, across processes;
- git itself, across machines.

A push that loses a race comes back non-fast-forward; the transaction pulls and
retries with exponential backoff, up to six attempts. A rebase conflict can only
be in a single-writer file, and there the **remote wins** (`-X ours` after a
failed rebase, which in rebase terms is the upstream side): a stale writer
discovers it lost on its next `resolve()`.

**Reading is not writing.** `Coord.pull()` is called at the top of every
scheduler and poller cycle regardless of whether anything will be written
(`scheduler.py:114`), and `Coord.commit()` returns false when nothing is staged.
An idle machine therefore stays within one cycle of current while contributing
nothing to the history — a distinction that matters in §4.5.

## 4.4 Attribution

A record written by a machine carries that machine's identity. A record that
represents a human decision — a plan review, an arbitration, a quota change —
carries the human's name from `humans.yaml`, and the surfaces that act on those
records refuse to accept one whose author is not a recognised human (§10.6).
`humans.yaml` is committed, so every machine agrees on who may decide.

## 4.5 Liveness does not live in the history

A heartbeat is not history. It says one thing — "this machine still holds this
claim, as of now" — and the previous value is of no interest to anyone. But it is
the only record whose volume is a function of wall-clock time rather than work
done, and in the original design it was a commit.

Measured on the meta swarm's ledger over its first fortnight: **493 commits, 238
of them heartbeat-only.** A machine holding a claim beats every
`min(heartbeat_s, lease_s / 3)` — 3 minutes with the default lease — so roughly
480 commits a day each. Four machines across two swarms is some 2,000 a day,
around 700,000 a year, none of which anybody will ever read.

The mitigations that look obvious are not available. Skipping the beat when a
machine holds no claim is already the behaviour (`ledger.heartbeat` returns early
on an empty list). And beat frequency is pinned to lease length on purpose:
`lease / 3` gives three beats per lease so that one missed beat does not expire a
claim that is alive. Lengthening the lease to beat less often directly slows
recovery when a machine dies, which is the wrong trade.

**So liveness is specified to live outside the branch**, in a git ref per
machine: `refs/dags/live/<machine>`, written with `git update-ref` and pushed on
its own. No commit on the default branch, ever. Each machine writes only its own
ref, so there is no contention, no rebase and no conflict rule to reason about;
readers fetch `refs/dags/live/*` and read the blobs. Nothing is lost, because
heartbeats are already rewritten in place and no history of them exists today.

Two consequences are worth stating, because they are the actual reasons to do it:

- **Commit volume becomes a function of work, not time.** Everything still
  committed — claims, checkpoints, completions, events, control — is
  event-driven. The ledger stops growing while the swarm is merely switched on.
- **Expiry detection gets better rather than cheaper.** Once a beat costs no
  commit, a machine can beat every 30 seconds with a *shorter* lease, so a dead
  machine's claim returns to the pool sooner.

This is specified and not yet built (D31; the issue is drafted, not yet filed). Migration must be dual-read, not a
cutover: under the pinned-copy model (§5.6) old and new machines coexist, and a
new machine that writes only a ref would look dead to an old one, which would
then expire its claims — the exact failure the change exists to prevent.

## 4.6 One ledger per swarm

A coordination repository never doubles as a repository the swarm writes code to
(D27).

This was learned by violating it. The meta swarm's ledger lived in the same
repository as the DAGS source for two weeks, with three consequences. Ledger
commits swamped the code history — 328 of 369 commits on the default branch in
one month. Branch protection, added so that a human reviews every merge, blocked
the daemon's direct ledger pushes outright, so the swarm could not run at all
until the ledger moved. And a worker's worktree sat inside the live ledger, which
is the mechanism behind `#1`.

The rule also makes the "one ledger per swarm" shape explicit: two swarms do not
share a ledger, so volume is per-swarm, and a repository that is a swarm's plan
and code can never be its ledger.

\newpage

# 5. Bootstrapping — one script to join the swarm

A machine joins by cloning the coordination repository and running
`./bin/swarm.py start`. There is nothing to install first beyond git, the GitHub
CLI and Python 3.10 or newer.

## 5.1 Why a committed script, in Python

The bootstrap has to run before any dependency is installed, which rules out
anything that needs a package. It is Python rather than shell because it is the
same language as the rest of the system, so a reader has one language to follow
and the logic is testable; and because `bin/dags/venv.py` has to be importable
before any third-party module exists, it is strictly standard library.

The script is **committed to the coordination repository**, so every machine runs
the same code by construction. That is also why upgrading is explicit (§5.6).

## 5.2 The virtual environment

`bin/swarm.py` creates `.swarm/venv`, installs `bin/requirements.txt` and
re-executes itself inside it. The dependency list is deliberately four packages
— `typer`, `rich`, `textual`, `pyyaml` — and everything else is standard library
(D23). `bin/skill/swarm-task` has no dependencies at all, because it must run in
any worktree whether or not a venv is present.

The venv is **stamped** with the platform, machine architecture, Python
minor version and a hash of the requirements files, and rebuilt when the stamp no
longer matches. This matters more than it sounds: a folder shared with a virtual
machine can hold a venv built for the wrong system, and a requirements change
must not leave a machine running stale packages.

The stamp has a cost worth knowing about. A coordination repository running a
pinned older `bin/` (§5.6) has a different requirements hash from the code
repository its worktrees come from, so a worktree's environment will not match
the coordination repository's and will be rebuilt — a multi-minute install that,
under `-q`, is indistinguishable from a slow test run. Sharing one environment
(`#49`) only helps when the stamps agree, which under the pinned-copy model they
often will not. `DAGS_NO_VENV=1` skips the whole mechanism for a hand-managed
environment.

## 5.3 What `start` does, in order

1. **Prerequisites** (`bin/dags/prereqs.py`): fail fast and completely rather
   than half-configure. git, `gh` and an authenticated `gh` session, Python
   version, a writable coordination repository, a readable `backend.yaml`.
2. **Identity.** A machine's identity is derived from its hostname plus a random
   suffix, stored in `.swarm/identity`, and never changes afterwards unless
   `identity set --force` is used. It is the second half of the claim tiebreak
   (§6.3), so it must be stable and unique.
3. **Clock calibration** (`bin/dags/timeutil.py`): measure this machine's offset
   from GitHub's clock once, and route every timestamp it writes or compares
   through the correction. The logical clock orders events, but lease expiry is
   about real elapsed time, and a machine with a skewed clock would otherwise
   expire live claims or keep dead ones.
4. **Code repositories** (`bin/dags/repos.py`): for every repository named by an
   unfinished task, use the checkout mapped in `.swarm/local.yaml` or clone it,
   point `commit.template` at the coordination repository's template, and keep
   the swarm's scratch paths in the clone's `info/exclude` — resolved through
   `--git-common-dir`, so every linked worktree inherits it and nothing has to be
   committed to anybody's code repository.
5. **The daemon.** `swarm.py start` writes a `start` control record and spawns
   `swarm.py _daemon` detached, which runs the three loops of §2.2, writes
   `.swarm/daemon.pid` and logs to `.swarm/swarm.log`.

## 5.4 Machine-local state

Everything under `.swarm/` is per-machine and never committed:

| Path | Contents |
|---|---|
| `identity` | This machine's name |
| `local.yaml` | Human name, repository checkout paths, worker token source, bot identity, terminal and IDE choices, notification settings |
| `venv/` | The environment of §5.2 |
| `daemon.pid`, `daemon.json` | The running daemon and its liveness |
| `swarm.log` | The daemon's log |
| `notifications.log` | Every notification, always (§9.4) |
| `poller-state.json` | What the poller has already seen |
| `git.lock` | The cross-process lock of §4.3 |

The separation is strict on purpose: anything a second machine would need to know
is a ledger record, and anything only this machine needs is here. A machine can
be rebuilt by deleting `.swarm/` and starting again.

## 5.5 The worker token

Worker pull requests are opened by a bot account, not by the human running the
machine, so that a human can review and merge them — a GitHub account cannot
approve its own pull request, which is the mechanical reason the bot exists
(D2). Its token is read per call (`bin/dags/gh.py`) from the macOS Keychain, an
environment variable, or not at all, as configured in `.swarm/local.yaml`. It is
never written to the ledger, never logged, and never placed in a file in the
repository.

## 5.6 Nothing upgrades itself

A coordination repository holds a **pinned copy** of `bin/`, and a running
machine executes the code it was started with. Merging a change to the protocol
changes nothing anywhere until a human copies the new `bin/` into the
coordination repository, commits it, and stops and starts each machine (D33).

This is deliberate. A swarm whose task is to change the protocol it is running
would otherwise rewrite itself mid-flight, and a broken merge would be discovered
by every machine simultaneously. With an explicit upgrade, a bad change is
discovered on restart, by one machine, with a human watching.

The cost is that a merged fix can sit unused. It has already happened: the
command that stops workers polling for plan approval merged on one day and was
still not running on the next, because the coordination repository had not been
upgraded. An upgrade is a deliberate act, and it needs to be a routine one.

\newpage

# 6. The claim protocol — deterministic and leaderless

This is the core. Every machine, given the same synced repository, computes the
same answer to "who owns this task?" by the same pure function. There is no
negotiation because there is nothing to negotiate.

## 6.1 The logical clock

Every record carries a `logical_clock`. A new record's clock is
`1 + max(logical_clock across every synced file)` — computed by reading the
ledger, never stored locally (`resolve.next_clock`).

That definition is the whole trick. It is a Lamport clock whose state *is* the
repository, so a machine that has just pulled cannot pick a clock value that
ignores what others have done, and a machine that has not pulled cannot write at
all, because every write begins with a pull (§4.3). The clock also names the
files: `claims/<machine>-<clock>.yaml` sorts a directory into the order events
happened.

A consequence worth noting: the clock is derived, so anything that removes
records — a compaction of old history, for instance — must preserve a floor for
it, or a machine could reuse clock values and break the tiebreak below.

## 6.2 Making a claim

A claim is a new file in the task's `claims/` directory, written inside one
transaction: pull, compute the clock, write, commit, push. Nothing is reserved
beforehand and nothing is locked. Two machines may claim the same task at the
same moment, and both claims are valid records; §6.3 decides which one counts.

A claim is accompanied by nothing else. The worktree is prepared and the worker
dispatched only after the claim has been pushed and re-resolved, because until
then this machine does not know it won.

## 6.3 Resolution

![Claim resolution: a pure function every machine computes identically.](figures/fig4-resolution.pdf){width=159mm}

`resolve.resolve(task_dir, now, lease_s, humans)` reads the claims, withdrawals,
heartbeats, completions and arbitration for one task and returns the winner,
with the reason. Its logic, in order:

1. **A claim is live** unless it was withdrawn, or its work already produced a
   completion that is no longer current, or its lease has expired (§6.5).
2. **An active human arbitration wins outright** (§9.5). If it names a winner,
   that claim owns the task; if it names none, the task is *frozen* and nobody
   owns it.
3. **Otherwise the live claim with the lowest `(clock, machine)` wins.**

The tiebreak is the logical clock first, and the machine identity as a
deterministic tiebreaker when two claims share a clock. Both are strings and
numbers in files, so every machine sorts them identically. "First to write the
record wins" is not quite the rule — "lowest clock wins" is — and the difference
only shows when two machines write concurrently from the same pulled state,
which is exactly the case the tiebreak exists for.

## 6.4 What a losing machine sees

Nothing happens to it. Its claim record stays in the ledger as an accurate
statement that it tried, and on its next cycle `resolve()` tells it that it does
not own the task. The scheduler then withdraws its own claim
(`scheduler.tidy_own_claims`) and notifies the human, and the worker — if one was
already dispatched — is told its claim is gone rather than discovering it when
`done` fails.

This is why claims are never deleted: the ledger is a record of what happened,
not a projection of what is true now, and "who tried and lost" is useful when a
task starts thrashing (§9.5).

## 6.5 Lease expiry, evaluated lazily

![Liveness and expiry. Nothing sweeps; the reader computes it.](figures/fig5-lease.pdf){width=159mm}

A claim is live while its machine keeps proving it. `is_expired` compares the
claim's last heartbeat against `now` and the lease — fifteen minutes by default.

Nothing sweeps expired claims. Expiry is **computed at read time**, by whoever is
reading, which means there is no reaper to run, no clock to be authoritative and
nothing to go wrong while every machine is asleep. A laptop that closes its lid
stops heartbeating; its claims become expired to every reader fifteen minutes
later, and the work returns to the pool without anybody doing anything.

Two details matter in practice:

**A claim with no heartbeat at all is expired**, not pending. A machine that
claims and then dies before its first beat holds nothing.

**Expiry is not failure.** A claim that expires because a machine went away
should not count toward the retry budget that lowers a task's autonomy (§8.3) —
a sleeping laptop is not a worker that cannot do the job. This distinction is
specified and only partly built: the withdrawal reasons now distinguish a
no-worker-chosen expiry from a failure (`#12`, merged), but lease expiry caused
by a machine disappearing is still counted (`#14` territory).

## 6.6 Human arbitration overrides the algorithm

A recognised human can write an `arbitration/` record that names the winning
claim, or names none to freeze the task. It is checked before the tiebreak, so it
is not advice the algorithm may weigh — it is a decision the algorithm obeys.

An arbitration record is signed by a human from `humans.yaml` and carries a
reason. The surfaces refuse to write one on behalf of anybody else, and refuse to
honour one whose author is not recognised (§10.6).

\newpage

# 7. Quota, concurrency and multi-machine scheduling

Claiming decides *who* works a task. Quota decides *how many* are worked at once,
which is the difference between a swarm and a stampede.

## 7.1 Two numbers

**The global cap, N.** How many tasks the whole swarm may have in flight.
It comes from `backend.yaml`'s `swarm.default_quota`, and any human may change it
at runtime by writing a `quota/` record; the latest record from a recognised
human wins (`resolve.global_quota`). It is a ledger value, so every machine
agrees on it without being told.

**The per-machine share.** How many of those N this machine may hold. It is a
local choice — `start --quota-share 2`, or `throttle` at runtime — because the
right number depends on the machine's CPU, not on the swarm's policy.

Room to claim is the smaller of the two gaps:

```python
room = max(0, min(N - len(active_claims), share - mine))
```

Both terms read the same synced ledger, so two machines cannot each believe they
have the last slot unless they are working from different state — and the
transaction in §4.3 is what stops that.

## 7.2 Lowering the quota mid-flight

Raising N is uneventful. Lowering it below what is already running is the
interesting case, and it is resolved without any machine talking to another.

`resolve.over_quota` answers "which of *my* claims must yield?" — and it does so
by sorting **all** active claims in the swarm by `(clock, machine)` descending,
so the newest yield first. Every machine computes that same global ordering from
the same ledger and then looks up only its own claims in it. Together they
release exactly the excess: no more, no fewer, no negotiation.

What yielding means depends on how far the task has got
(`scheduler.enforce_quota`):

- **A claim with no worker yet** is withdrawn immediately. Nothing is lost.
- **A running one is asked to stop politely**: `pause_requested` is written into
  its checkpoint, the injected skill shows it, and the worker is expected to
  record what it has tried and stop. One lease later the claim is withdrawn
  whether or not it complied.
- The task then sits ready until a slot frees, and the next worker **resumes from
  the checkpoint** rather than starting over.

**Share 0 is drain and release, not freeze.** Setting a machine's share to zero
releases what it holds through the same path rather than keeping it alive. That
is a deliberate decision, and it is worth stating because the opposite is
intuitive enough that it was once written down wrongly and a worker planned
against it.

## 7.3 What implements a task — the worker port

Once a machine owns a claim it prepares a worktree and dispatches a worker.

**The worktree.** A git worktree of the task's code repository, on a branch named
`swarm/<TASK>`, at `.worktrees/<TASK>` **inside the code repository's checkout**.
A resumed task reuses the existing local or remote branch, so the next worker
continues from whatever the last one pushed.

The location matters more than it sounds. Worktrees used to live inside the
*coordination* repository, and an agent started there would walk up the directory
tree, find the coordination repository's own `CLAUDE.md` and `bin/`, and conclude
that maintaining the swarm was part of its task (`#1`). Putting them in the code
repository both removes that and gets project ancestry right — `.editorconfig`,
`.nvmrc`, Maven settings discovery, a language server's project root all resolve
the way they would for a human working in that repository. A location outside
both repositories resolves none of them and eventually walks up into `$HOME`.
`repos.configure` keeps `.worktrees/` in the clone's `info/exclude`, so nothing
has to be committed to anybody's code repository, and the dot-prefixed name is
already skipped by pytest's default `norecursedirs`. The move is specified and
not yet built (D32; the issue is drafted, not yet filed); a per-repo `worktrees:` setting covers the case where a
repository must not hold them.

**The worker.** `bin/workers/base.py` is the port. A worker is whatever
implements one claimed task: an AI CLI in a terminal, or a human in an IDE.
Three adapters ship, all macOS launchers (D3): `claude` opens an interactive
Claude Code session in the worktree pointed at the injected skill; `intellij` and
`vscode` open the worktree with `.swarm-task/README.md` as the first tab and then
get out of the way.

Two properties of the port are what make a human and an agent interchangeable:

- **The scheduler only ever calls `dispatch`.** It does not supervise, read
  output or know whether a process is still alive.
- **Completion is detected from the output contract** — a ledger record and a
  pull request — **never from the worker's process.** A worker that crashes, is
  closed, or goes to lunch is indistinguishable from one that is thinking, and
  the lease (§6.5) is what eventually resolves it.

**Autonomy restricts the choice.** `workers.allowed_for` returns only human
workers for a `human-must-scope` task: a task whose scope a human must set is
never handed to an AI. This is a filter on the offer, not a check at the end —
which is why a `human-must-scope` task set to `ready` will sit claimable and
unclaimed if no human picks it up.

**The injected skill.** Each worktree gets a `.swarm-task/` directory containing
the ticket, the house conventions, a `context.json` describing the task, claim,
repository and branch, and the `swarm-task` command itself (§9.3). It is removed
when the pull request opens.

## 7.4 Several machines

Machines do not coordinate; they converge. Each pulls, computes the same
readiness and quota answers from the same files, and claims what is left. Two
that claim the same task at the same moment both write valid records and §6.3
decides. The only shared state is the repository.

**Epic takeover** is the one exception to pure first-come ordering. A human may
write a `priority/` record claiming an epic for one machine, after which other
machines leave that epic's tasks alone (`resolve.active_takeovers`). It exists
for the case where one machine has the right toolchain, or where a human wants a
whole epic worked in one place rather than spread across three laptops.

**Two machines on one computer** is the normal development set-up: two clones of
the coordination repository, each with its own `.swarm/identity`, both pushing to
the same remote. It exercises the racing properly, and it is how most of the
protocol's behaviour was observed.

That configuration does have a sharp edge today. If both clones map the *same*
code repository checkout, a branch can only be checked out in one worktree, so a
task that moves between those machines cannot be dispatched by the second — the
first still holds the branch. That breaks resume-on-another-machine for lease
expiry, arbitration, reassignment and freeze alike. Two changes fix it, both
specified and not yet built (`#13`): the machine that loses a task releases its
worktree, and a machine does not claim work whose branch it cannot check out.

\newpage

# 8. Pause, resume and failure recovery

A swarm that cannot be stopped is not governable, and an agent that fails must
fail in a way the next one can pick up. Both are ledger records.

## 8.1 Machine control

`control/` records change a machine's behaviour, and because they are ledger
records any machine can write one about any other — pausing a laptop from the
Board on a different laptop is the normal case.

| Record | Effect |
|---|---|
| `start` | The daemon is running; sets the share |
| `stop` | The daemon shuts down; its claims lapse when their leases expire |
| `pause` | Claim nothing new; keep heartbeating what is held |
| `resume` | Undo a pause |
| `throttle` | Change this machine's share, live |

`ledger.machine_control` folds the records in clock order into the machine's
current state. Pause and resume are idempotent — pausing an already-paused
machine writes nothing, which was not always true and produced duplicate feed
entries.

**Which of these survive a restart is specified explicitly**, because leaving it
implicit produced two opposite bugs. A `pause` currently outlives `stop` and
`start`, so a restarted machine looks healthy and silently claims nothing
(`#30`); a `throttle` currently does *not* outlive them, because `start` rewrites
the share from its flag, so a deliberate throttle is silently lost (`#61`). Both
are the same unanswered question. The specified behaviour: **a deliberate `start`
clears a pause**, and **`start` without an explicit share keeps the machine's
last throttled share**, and in both cases `start` says which it used and why.
Neither is built yet.

## 8.2 What a worker leaves behind

`checkpoint.yaml` is the task's working memory, written by the machine that owns
the claim: the approved plan and its hash, a one-line summary, what has been
tried, what remains, open questions, risks, the dispatched worker, and the pull
request once there is one.

It exists so that a failure costs the work, not the understanding. A new worker
on a resumed task is given the plan, what the last one tried and what it left —
`swarm-task implement` prints exactly that — so the second attempt starts where
the first stopped rather than from the ticket.

## 8.3 Failure, and lowering autonomy

A claim that ends badly is withdrawn with a reason, and the reasons are
distinguished because they mean different things: `lost-race`,
`no-worker-chosen`, `quota` and `dispatch-failed`. Expiry is the exception — it
writes nothing, because it is computed rather than performed (§6.5).

Repeated failure on one task lowers its autonomy tier by one step — `auto-pr` to
`human-must-review` to `human-must-scope` — on the grounds that a task three
agents could not finish is a task whose framing needs a human. The threshold
scales with how many times it has already been lowered
(`failures >= max_retries * (downgrades + 1)`), so a downgraded task gets a
longer rope before being downgraded again. The tier is changed in the backend
first and the ledger records it only on success.

Three refinements are specified here, none built:

**Expiry caused by a machine going away is not failure.** A sleeping laptop is
not a worker that cannot do the job, and counting it lowered a real task's
autonomy overnight. A claim withdrawn as `no-worker-chosen` is already exempt
(`#12`); lease expiry still needs the same treatment.

**A downgrade can be undone.** Today it cannot: `read_meta` lays every `meta/`
revision over `meta.yaml`, so a recorded downgrade beats whatever the tracker
says, and the only CLI reset writes the tracker label. The specified behaviour is
a `task set-autonomy` command, with **the ledger as the source of truth and the
tracker label following it** (D30).

**A plan-level question drops a task out of `auto-pr` for that run** (D30,
`#45`). When a worker records `needs_human` about its plan, the self-approval is
cleared and the plan goes through the normal review gate, because a plan written
against a wrong assumption should not reach a pull request unreviewed. The
downgrade is **scoped to the run** and cleared when the task completes.

## 8.4 Thrashing, and when to stop trying

A task that is claimed, lost and re-claimed repeatedly is not making progress,
and the protocol is capable of doing that forever. Two guards:

**An unanswered worker prompt is not a claim.** A task claimed but never given a
worker is withdrawn after one lease as `no-worker-chosen` — a non-failure — and
parked until someone unparks it, rather than being re-claimed on the next cycle.
Without this, one task was claimed nine times with no worker ever chosen.

**Conflict cycles are counted.** Past `swarm.thrash_threshold`, the task is
flagged as needing arbitration and a human is notified (§9.5) rather than left in
the loop.

## 8.5 Dispatch failure is a state, not a silence

A claim whose worker could not be launched used to leave a task claimed and
apparently in progress forever. A failed dispatch is now recorded in the
checkpoint, so every Board and `status` shows `not started · dispatch failed ×N`;
after three attempts the claim is withdrawn as `dispatch-failed` — explicitly not
a failure for the autonomy count — and the machine backs off for one lease.

## 8.6 The idle check

An AI worker that is thinking and a human worker who has gone home look identical
to the scheduler. After `swarm.human_idle_hours` without progress, the human is
asked whether the task is still being worked (`task still-working` confirms it).
This is a prompt, not a timeout: the lease is what actually releases a claim.

\newpage

# 9. Human control — review, merge and arbitration

The swarm's output is pull requests, and a human decides what becomes of them.
This chapter is the set of places a person can say yes, no, or not like that —
and the reason each one is in the protocol rather than in a convention.

## 9.1 The mandatory gates

Two gates cannot be bypassed by an agent, because what the agent must do to make
progress is write a record a human can refuse.

**The plan gate.** On a `human-must-review` or `human-must-scope` task, a worker
writes `plan.md`, submits it, and may not implement until a recognised human has
approved it. `work.finish` refuses outright if the plan is not approved, so the
gate is enforced at the end as well as at the start — an agent that ignored the
instruction still cannot open a pull request. An `auto-pr` task self-approves on
submit, which is what that tier means.

**The merge gate.** The swarm never merges. `done` pushes a branch and opens a
pull request; a human merges it. On GitHub this is reinforced mechanically: the
pull request is opened by the bot account and an account cannot approve its own
pull request, so a human review is structurally required rather than merely
expected (D2).

A third restriction is narrower but worth naming: on a `human-must-scope` task,
`finish` refuses to open a pull request at all if the recorded worker is not a
human. The tier is enforced twice — once as a filter on who may be offered the
work (§7.3), once as a check on who may finish it.

## 9.2 Override paths

Everything here is a ledger record written by a recognised human, so it works
from any machine and every machine sees it.

| Lever | Record | Effect |
|---|---|---|
| Approve or send back a plan | `plan-reviews/` | Opens or closes the plan gate |
| Freeze a task | `arbitration/` with no winner | Nobody owns it; nobody may claim it |
| Reassign a task | `arbitration/` naming a claim | That claim owns it, whatever the clock says |
| Release a task | `withdrawals/` | Back to the pool |
| Block an epic | `swarm:status:blocked` on the epic | Halts every child task at once |
| Change the global cap | `quota/` | §7.1 |
| Pause, stop, throttle a machine | `control/` | §8.1 |
| Take over an epic | `priority/` | One machine works it |
| Answer a worker's question | `events/` | §9.3 |

**Blocking an epic is the big red button.** Because readiness excludes a task
whose epic is blocked (§3.1), one label stops an entire branch of the plan
without touching its children, and unblocking restores them in whatever state
they were.

## 9.3 Telling the worker

A gate is only useful if the worker learns the gate has opened. This was the
weakest part of the original design: approval wrote a record, the Board and the
poller read it, and nothing told the worker session anything. A plan approved
while a worker was waiting could sit for hours.

The specified behaviour has three parts, of which the first two are built:

**`swarm-task wait` blocks until something happens** — a plan approved or sent
back with its note, an answer to a question, a pause requested, a claim lost. It
is a cheap polling loop over the ledger after a pull, which is what a worker
would otherwise improvise badly.

**Every `swarm-task` command prints pending news first**, so a worker that
forgets to wait still cannot miss a pause or a lost claim.

**`swarm-task block "question"` has an answer path.** The question goes to
`checkpoint.needs_human` and to the tracker; a human answers with
`task answer` or from the Board, and `wait` returns with the answer.

The third part is the contract itself, and it is **not** built. The injected
`README.md` still tells a worker to poll `status`, which is the instruction a
worker actually follows — so `wait` exists and nothing invokes it, and plan
pickup is still a human round-trip (`#58`). Specified: the contract says to
`wait` after submitting a plan, `plan --submit` prints that invocation, and the
same idiom covers waiting on an answer, a test-scope decision, and checks after
`done`.

## 9.4 Pull request feedback

Feedback reaches a worker through the poller, which reads each open pull request
and records what changed.

**A "Request changes" review binds.** It records a `reopened` completion, the
task returns to the queue, and the next worker is shown the feedback by
`swarm-task implement`. Lines tagged `fix:`, `explain:` or `reject-approach:` are
the actionable items; a `reject-approach:` tells the worker to stop and re-plan
rather than patch.

**A comment from a recognised human surfaces but does not reopen** (D29). It is
shown on the Board and to the worker, and it does not by itself put the task back
in the queue. The distinction is deliberate: discussion and instruction are
different acts, and conflating them turns every passing remark into work. The
cost is that a comment-only review can be missed, which has happened — so the
Board shows comment and unresolved-thread counts, and a "new since you looked"
marker, rather than relying on the reviewer to use the right control.

Inline review threads are read through GraphQL `reviewThreads`, with path, line
and resolution state, and passed to the worker with the file and line. Most of
§9.4's richer behaviour — the detail screen, sending work back from the Board,
resolving threads — is specified and not yet built (`#6`).

## 9.5 Arbitration

When a task has been claimed, lost and re-claimed past
`swarm.thrash_threshold` cycles, the protocol stops trying and asks. The task is
flagged as needing arbitration and a human is notified; they write an
`arbitration/` record naming the winning claim, or naming none to freeze it.

Arbitration is checked before the tiebreak (§6.3), so it is not an input to the
algorithm — it replaces it. That is the point: the deterministic rule is right
almost always, and when it is not, a person should not have to fight it.

## 9.6 Notification without a service

There is no hosted notifier. The poller runs on each machine and its notify step
is pluggable, with one channel that always works: every notification is appended
to `.swarm/notifications.log`, which a human can `tail -f`. A macOS desktop
notification and an outbound webhook are opt-in in `.swarm/local.yaml`.

Notifications are emitted on state transitions the poller detects by diffing
against `.swarm/poller-state.json`: newly awaiting review, stale heartbeats, more
than one live claim, dispatch failures, daemon errors, and pull request events.
Daemon errors reach the feed rather than only `swarm.log`, which is what makes a
silent failure visible.

One gap in the poller is worth stating because it is a correctness bug rather
than a missing feature: it refreshes a pull request's state only while the task
is `awaiting-review`, and records the merge from inside that same loop. A failed
`gh pr view` is logged and dropped, so a transient network failure during the one
poll that would have seen a merge loses it permanently, and the pull request shows
as open forever. Specified: keep querying until a terminal record exists, treat a
failed fetch as "not yet checked", and surface a pull request whose state has not
been confirmed (`#56`).

\newpage

# 10. The Swarm Board — a local command centre

![The Swarm Board: panels, actions, and the one action that is not a ledger record.](figures/fig6-board.pdf){width=159mm}

The Board is a terminal application over the same local clone everything else
reads. It is not a dashboard onto a service; it is a view of files, which is why
it works offline and why two Boards on two machines agree.

`swarm.py board`, or `swarm.py board --web` to serve it on localhost.

## 10.1 What it is made of

`bin/dags/boardview.py` computes what the Board shows, as plain data, with no
Textual import. `bin/board.py` lays those rows out and binds keys to
`dags.actions` and `dags.work`. The split is why the Board's logic is tested
everywhere, including in environments with no terminal.

Every button is an ordinary ledger record or an ordinary `gh` call. The Board has
no privileges and no private state: anything it can do, `swarm.py` can do, and
anything it shows, `status` can show.

## 10.2 Panels

- **Machines** — each machine, its share, its state (running, paused, share 0,
  stopped), uptime and awake share, with the key that undoes the current state.
- **Live claims** — what is claimed, by whom, how old the claim is, the worker
  and the task's state.
- **Plans awaiting review** — submitted plans, plus plans written but *not*
  submitted, and plans changed since submission. The Board reads the worktree to
  find them, which is how it can offer "submit and review" for a plan a human
  worker left behind.
- **Awaiting review** — open pull requests, their review decision, their check
  state, and comment counts.
- **Activity feed** — ledger records in plain English, attributed to a person.
- **Daemon log** — the tail of `swarm.log`, filtered by level, so a failure that
  would otherwise be invisible is one keypress away.

## 10.3 Actions

`p` pause, `r` resume, `t` throttle, `s` stop; `w` choose a worker, `f`
freeze or unfreeze, `a` reassign, `e` take over an epic; `v` review a plan, `x`
answer a test-scope question, `y` answer a worker's question, `m` approve and
merge; `o` open the ticket, `O` the pull request; `n` set the global cap, `l`
cycle the log level, `?` help.

Three of these deserve a note:

**`m` is the only action that is not a ledger record.** It runs the real `gh`
approve-and-merge, and it warns on red or pending checks, merging over red only
on explicit confirmation.

**`w` offers only the workers the task's tier allows** (§7.3), and offers "Not
now" and "Not now, and pause this machine" as first-class choices rather than
leaving Escape as the undocumented way out.

**Deferral is specified and not built.** A claim a human is not ready to staff
should be snoozeable — remind me in ten minutes, back in an hour, park it — with
a Board section listing parked and snoozed tasks and a ledger record so the
snooze survives a restart (`#4`). Today the only options are to choose a worker
or leave the claim held.

## 10.4 Attribution and trust

Every human action the Board takes is recorded under the human's name from
`humans.yaml`, taken from `.swarm/local.yaml` or matched through `gh api user`.
The Board refuses to act as a human it cannot identify, and the resolver ignores
plan reviews and arbitrations whose author is not a recognised human — so an
agent cannot approve its own plan by writing the record directly.

`humans.yaml` is committed, which means the set of people who may decide is
itself reviewed.

\newpage

# 11. Verification — tests, scope and continuous integration

![Three places work is verified, and the only one that tests what will actually land.](figures/fig7-verification.pdf){width=159mm}

This chapter did not exist in v1.1, because when v1.1 was written a worker ran
the whole test suite and that was the whole story. It is now the part of the
system with the most moving parts, and the part where the swarm's throughput is
won or lost.

## 11.1 Three places work is verified

| Where | What it proves | Who waits for it |
|---|---|---|
| The worker's worktree | This change does not break what it touches | The worker, before `done` |
| CI, on the pull request | The **merge result** is sound | The human, before merging |
| CI, on the default branch | The branch is sound after the merge | Everyone, continuously |

The middle one is the one that matters most and is easiest to get wrong. A
`pull_request` workflow does not test the branch — it tests `refs/pull/N/merge`,
the merge of the branch into the base. That is the only place a conflict between
two independently green branches is caught, and it is exactly the failure that
once left the default branch unable to even collect its tests: two branches each
passed, their merge resolved a conflict badly, and nothing had run the merged
result.

## 11.2 Test scope

Running the full suite for a three-line change is the wrong default when a worker
is holding a claim whose lease is ticking.

`bin/dags/testscope.py` computes a scope from the diff, as a pure function:
`none`, `targeted`, `neighbours` or `full`. A table (`tests/map.yaml`, glob to
test files) is consulted first; for a changed module with no table entry, the
tests that import it are the fallback, found by a static import scan;
"neighbours" adds the tests of modules that import a changed module, one hop out.
Anything it cannot map escalates to `full` — the safe direction — and files shared
by every test (`conftest.py`, the fakes) always mean `full`.

The flow is a conversation, not a guess: `swarm-task test --propose` records a
proposal and a question, a human answers from the Board (`x`) or with
`task answer-tests --scope`, and `swarm-task test` then runs only that scope and
refuses to run before an answer exists. An `auto-pr` task may accept its own
proposal.

**The default is still the full suite**, and that is specified to change (`#59`).
`done` runs the complete `test_command` unless a human has already approved a
narrower scope for the same changed files. Specified: `done` runs the `targeted`
scope by default, with `--scope` to widen or narrow it, the chosen scope recorded
in the checkpoint and stated in the pull request body, and the hidden
`--skip-tests` replaced by `--scope none`.

Two mechanical traps belong in the same change, because they make a "targeted"
run no cheaper than a full one. A test command that hardcodes the test directory
and appends the caller's arguments turns a scope into a *filter* — the whole suite
is still collected and then deselected, and collection is a large share of the
cost. And parallel execution across all cores makes a small selection worse, not
better, because every worker process imports and collects the whole suite
independently. Scopes must be passed as paths, and parallelism should apply only
to the full scope.

## 11.3 `done`, end to end

`work.finish` is the single path from "the work is finished" to "a human has
something to review", and its order is deliberate:

1. Pull, and confirm this machine still owns the claim.
2. Refuse if the task is `human-must-scope` and the worker is not a human.
3. Refuse if the plan is not approved.
4. Refuse if no summary has been recorded.
5. Run tests — the agreed scope if one was approved, otherwise the full command.
6. Commit with the house template, authored as the bot.
7. **Refuse if the branch is no new commits ahead of its base.** A reopened task
   whose worker produced nothing must not re-announce itself as finished, which it
   once did, posting the same commit twice.
8. Push the branch and open the pull request.
9. **Wait for the checks**, up to `checks_timeout` (300 seconds by default). Red
   keeps the task in progress with `ci_failure` in the checkpoint, so the session
   that wrote the code is still the one that fixes it. Pending or no checks
   configured finishes as before.

Step 9 is why §11.1's middle row has a waiter. Before it existed, `done` opened a
pull request and walked away: the worker exited, the Board said ready, and a red
build reached nobody.

## 11.4 The merge gate on the default branch

A protected default branch is what makes the merge gate real rather than
customary. The configuration that works with DAGS is: no force-push, no deletion,
the test workflow required, one approving review, and no bypass actors — combined
with pull requests opened by the bot (D2) so that a human review is
mechanically necessary.

Two interactions are worth recording, because both were discovered the hard way.

**A protected branch and a ledger cannot share a repository.** The daemon pushes
ledger commits directly, hundreds a day; protection forbids exactly that. This
is one of the reasons for D27 (§4.6), and `swarm.py status` should warn when a
coordination repository's default branch is protected (`#41`).

**Requiring branches to be up to date is a real trade.** Without it, merging one
pull request makes every other open one's green check stale — computed against
the older base. With it, every merge invalidates the others and they must absorb
the base again. With several pull requests open at once the safe procedure is to
merge one at a time and let the default branch's own workflow go green between
merges.

\newpage

# 12. End-to-end flow

![One task end to end, and the two gates an agent cannot pass.](figures/fig8-lifecycle.pdf){width=159mm}

One task, from a human's idea to merged code, naming the chapter that governs
each step.

1. **A human files an issue** in the plan repository and labels it: a
   `swarm:status`, an autonomy tier, and `repo:`. Without a swarm label it is
   invisible to the swarm (§3.4). When the plan is large, `backend seed` creates
   the whole shape from a file (§3.6).
2. **A machine's scheduler pulls and syncs the plan** into `tasks/` (§3, D11).
   New tasks get a `meta.yaml`; later tracker changes arrive as `meta/`
   revisions.
3. **It computes readiness and quota room** (§3.1, §7.1), both pure functions over
   the synced ledger.
4. **It claims a ready task** — a new file in `claims/`, written in one
   transaction (§6.2) — then pulls again and re-resolves to find out whether it
   won (§6.3).
5. **It prepares a worktree** on `swarm/<TASK>` in the code repository and
   **dispatches a worker** from the tiers that task allows (§7.3). The Board asks
   a human which worker, unless a default is configured.
6. **The worker plans.** `swarm-task plan` writes `plan.md` from the ticket and
   from whatever a previous attempt recorded; `--submit` puts it in the
   checkpoint and, on an `auto-pr` task, self-approves it (§9.1).
7. **A human reviews the plan** on the Board, or sends it back with a note
   (§9.2). The worker waits on that outcome rather than polling (§9.3).
8. **The worker implements**, recording what it tried and what remains as it goes,
   and asking rather than guessing when it needs a decision (§8.2, §9.3).
9. **It agrees a test scope** if the repository is set up for it, and runs that
   scope (§11.2).
10. **`done` verifies and publishes**: the plan is approved, a summary exists, the
    branch is actually ahead, tests pass, commit with the house template as the
    bot, push, open the pull request, wait for the checks (§11.3).
11. **The poller notices** and tells a human the task is awaiting review (§9.6).
12. **A human reviews the pull request.** Request changes sends it back to the
    queue with the feedback attached (§9.4); approve and merge finishes it (§9.1).
13. **The poller records the merge**, the task becomes done, its worktree is
    swept, and the quota slot frees — at which point step 3 happens again.

Everything in that list except steps 1, 7 and 12 is a machine acting on files.
Everything in steps 1, 7 and 12 is a human, and none of them can be skipped by an
agent.

\newpage

# 13. Specified but not yet built

This chapter is the honest index of the document. Everything listed here is
specified in the chapters above as though it worked; none of it is in `main` at
the time of writing. Each entry names the issue that will build it.

| Area | Specified behaviour | Issue | Chapter |
|---|---|---|---|
| Worker contract | The contract tells a worker to `wait` after submitting a plan, instead of telling it to poll | `#58` | §9.3 |
| Sandboxing | `done` refuses a diff touching ledger files, `BATON`, `CLAUDE.md`, `backend.yaml` or `humans.yaml`; per-worktree worker rules | `#1` | §7.3 |
| Worktrees | Worktrees live in the code repository's checkout, excluded via `info/exclude`, with a per-repo override | drafted, not filed (D32) | §7.3 |
| Worktrees | The machine that loses a task releases its worktree; a machine does not claim work whose branch it cannot check out | `#13` | §7.4 |
| Labels | `ready` means human permission; computed readiness is never written back; labels that stop applying are retired | `#8` | §3.2 |
| Board | Deferral: snooze, back in an hour, park, with a Board section and a ledger record that survives a restart | `#4` | §10.3 |
| Pull requests | Comment and thread counts, a detail screen, sending work back from the Board, resolving threads | `#6` | §9.4 |
| Pull requests | The poller keeps querying until a terminal record exists; a failed fetch is "not yet checked" | `#56` | §9.6 |
| Machine control | A deliberate `start` clears a pause | `#30` | §8.1 |
| Machine control | `start` without an explicit share keeps the last throttle | `#61` | §8.1 |
| Autonomy | `task set-autonomy`; the ledger is authoritative and the label mirrors it | `#45` | §8.3 |
| Autonomy | A plan-level question drops a task out of `auto-pr` for that run only | `#45` | §8.3 |
| Autonomy | Lease expiry caused by a machine going away does not count as failure | `#14` territory | §6.5, §8.3 |
| Tests | `done` runs the `targeted` scope by default; `--scope` widens it; the scope is recorded and stated in the pull request | `#59` | §11.2 |
| Tests | Scopes are passed as paths, not filters; parallelism only on the full scope; a venv build announces itself | `#59` | §11.2, §5.2 |
| Liveness | Heartbeats move to `refs/dags/live/<machine>`; dual-read migration | drafted, not filed (D31) | §4.5 |
| Plan review | A plan review can carry an answer, so discussion happens on the issue and the gate stays on the Board | `#33` | §9.3 |
| Commit messages | The summary becomes a commit *subject*, not the whole body | `#35` | §11.3 |
| Diagnostics | `status` answers from the ledger without the network, and every `gh` call has a timeout | `#60` | §9.6 |
| Diagnostics | `status` warns when a coordination repository's default branch is protected | `#41` | §11.4 |
| Trust | The arbitration commit-author check (D16 is only partly done) | — | §6.6, §10.4 |

Two observations about this table, rather than about its entries.

**Most of it is about telling someone something.** The protocol's correctness was
largely right early; what was missing was making state visible to whoever needed
it next — the worker, the reviewer, the operator. A leaderless design makes every
participant responsible for reading state, and reading is the part that is easy
to leave out.

**Several entries exist because a merged fix was not reachable.** `wait` shipped
and nothing invoked it; the test scope shipped and the default did not change.
Under the pinned-copy model (§5.6) that gap is structural, not accidental: a
feature is not usable until the contract, the defaults and the running copy all
move. The specification counts a behaviour as built only when all three have.

\newpage

# 14. Limitations and operating boundaries

Things that are true by design, as opposed to Chapter 13's things that are true
for now.

**It is macOS-only in practice.** The worker launchers drive Terminal, iTerm,
IntelliJ and VS Code through `osascript` and `open` (D3). The worker port makes
another platform a contained piece of work, and nothing else in the system cares.

**It assumes GitHub for more than the tracker.** The adapter boundary covers the
issue tracker, but pull requests, reviews, checks, branch protection and the
`gh` CLI are assumed throughout Chapters 9 and 11. A different forge is a larger
change than a different tracker.

**One task, one repository.** A task names a single `repo:`, and a change
spanning two repositories is two tasks with a dependency between them. There is
no cross-repository atomic change.

**The ledger grows.** Until liveness moves off the branch (§4.5), commit volume
is a function of how long the swarm has been switched on rather than how much it
has done. Even afterwards the ledger only grows; there is no compaction, and the
options for one — archiving terminal tasks, or rolling over to a fresh repository
per period — are recorded in §4.5 rather than implemented. Any compaction must
preserve a floor for the logical clock (§6.1) and must reckon with being the
first non-append-only operation in the system.

**A swarm is as parallel as its merge queue.** Workers scale by adding machines;
merging does not, because a human merges one pull request at a time and each
merge can stale the others' checks (§11.4). Past a handful of concurrent tasks on
one repository, the bottleneck is review, not execution.

**Lease length is a trade, not a tuning knob.** A shorter lease recovers a dead
machine's work sooner and expires a slow one's work wrongly; a longer one does
the reverse. It is coupled to heartbeat frequency by `lease / 3`, which is why
§4.5 matters for more than disk.

**Nothing prevents a worker from doing something stupid inside its worktree.**
The guard rails are the path guard on `done`, the plan gate, and human review.
DAGS constrains where a worker writes and who approves the result; it does not
constrain what the worker thinks.

**It is a proof of concept.** It has run two swarms on one developer's machines,
not a team's. The parts exercised hardest — claiming, leases, quota, the plan
gate — are the parts most likely to be right. The parts exercised least are
multi-human review, a second platform, and anything involving a tracker that is
not GitHub.

\newpage

# Appendix A. Decisions log

Each decision records what was chosen and why. **Status** is `Done` when it is in
`main` with tests, `Specified` when this document describes it and Chapter 13
lists it, `Partly done` when some of it shipped, `Deferred` when it was chosen
not to build it.

| # | Decision | Why | Ch. | Status |
|---|---|---|---|---|
| D1 | All DAGS code lives in the coordination repository's `bin/`. | "Installing the protocol is cloning the repository." | 2, 4 | Done |
| D2 | Worker pull requests are opened by a separate **bot account**; its token comes from an environment variable, else the macOS Keychain; `worker_token: none` opts out. Humans approve and merge with their own `gh auth`, and commits are authored as the bot. | Keeps worker writes separable from a human's. It later turned out to be the mechanism that makes the merge gate real: a GitHub account cannot approve its own pull request, so a bot-opened pull request *requires* a human reviewer. | 5.5, 9.1, 11.4 | Done |
| D3 | Worker launchers are **macOS only**: Terminal and iTerm via `osascript`, `open -na` for IntelliJ, `code -n` for VS Code. | The proof of concept runs on Macs; the worker port leaves room for other systems. | 1, 7.3 | Done |
| D4 | The **Jira adapter is deferred.** | No Jira site to build and test against, and a moving API. The port stays tracker-agnostic so the work is contained when it happens. | 3.5 | Deferred |
| D5 | Two builders share the repository with a `BATON` file; hand-offs are local commits. | A development arrangement for building DAGS, not part of the protocol. | — | Done |
| D6 | Leases use **skew-corrected wall-clock time**: the offset is measured from GitHub's `Date` header; lease 15 minutes, heartbeat every 3. | A logical clock orders events but cannot measure fifteen minutes, and a purely logical lease never expires when only one machine is active. | 5.3, 6.5 | Done |
| D7 | `heartbeats/<machine>.yaml` and `checkpoint.yaml` are **single-writer** and re-check `resolve()`; heartbeats are one commit per cycle; `meta.yaml` never changes, later changes become `meta/` revisions. | Keeps "append-only, no textual conflicts" true for everything else. | 4.2, 4.3 | Done |
| D8 | `start` spawns a **detached daemon** with scheduler, heartbeat and poller threads, a pidfile and a log; the **Board is a separate process**; `stop` is a shared record plus a signal. | Threads die with their process, and a terminal UI cannot run inside a daemon. | 2.2, 5.3 | Done |
| D9 | The **global cap N lives in `quota/` records**; the latest from a recognised human wins; the default comes from `backend.yaml`. | N has to be changeable at runtime by any human, from any machine. | 7.1 | Done |
| D10 | A task's code repository is its `repo:` label, else the epic's, else `default_repo`. One repository per task. | Neither tracker has a field for a target repository. | 3.3, 3.5 | Done |
| D11 | **Plan sync** mirrors the tracker into `tasks/` every cycle. Done means the pull request merged. Readiness is the backend's rule **and** the ledger's. | Nothing in the original design said who creates task files or who marks a task done. | 3, 7.4, 12 | Done |
| D12 | **Autonomy tiers:** `auto-pr` self-approves its plan, `human-must-review` needs a human approval, `human-must-scope` is never given to an AI worker. Repeated failure drops the tier one step. | The tiers were named but their behaviour was undefined. | 7.3, 8.3, 9.1 | Done |
| D13 | **Idle limit:** no progress for `human_idle_hours` prompts "still working?", for every worker type. | The daemon heartbeats on the worker's behalf, so an abandoned IDE would otherwise hold a claim indefinitely. | 8.6 | Done |
| D14 | `done` runs the repository's tests and refuses on failure; merge refuses only on *failing* checks. | Written when there was no CI, so a pull request usually had no checks at all. Superseded in part by Chapter 11. | 11.3 | Done |
| D15 | Corrections to the originally published commands. | The commands as first written did not run. | — | Done |
| D16 | **Arbitration trust:** records naming a human not in `humans.yaml` are ignored. The commit-author email check is not built. | Anyone with push access can write a record naming any human. | 6.6, 10.4 | Partly done |
| D17 | Machine state lives in `.swarm/`; code repositories are a mapped path or a clone under `.swarm/repos/`. | Lets a human reuse an existing checkout. Worktree location superseded by D32. | 5.4, 7.3 | Done |
| D18 | GitHub reads use **one paginated GraphQL query**; without type labels, an issue with sub-issues counts as an epic. | Independent of which `--json` fields a `gh` release supports, and one call per sync. | 3.3 | Done |
| D19 | GitHub keys are `OWNER/REPO#N` with short key `GH-N`; ledger paths are `tasks/<EPIC>/<TASK>`, epics at `_epic`, loose tasks at `_no-epic`. | Human-friendly names on the Board and in branch names, with permanent keys underneath. | 4.1 | Done |
| D20 | **Plan gate records:** the plan and its hash live in the checkpoint; approvals are append-only `plan-reviews/`; a changed plan needs a new approval. | The checkpoint is single-writer, but the approving human may be on another machine. | 9.1, 10.2 | Done |
| D21 | The **injected skill is stdlib-only** and delegates to `swarm.py task …`, configured by `.swarm-task/context.json`. | It must run in any worktree without a venv on its path. | 7.3, 9.3 | Done |
| D22 | **Outcome records:** `pr-opened`, `done`, `reopened`, `rejected`, `replanned`. | Makes the request-changes and reject-approach paths computable from the ledger. | 9.4 | Done |
| D23 | **The venv is self-installing and stamped** by platform, architecture, Python version and a requirements hash, and rebuilt when the stamp does not match. | A folder shared with a virtual machine can hold a venv built for the wrong system. | 5.2 | Done |
| D24 | **Notifications** always append to `.swarm/notifications.log`; desktop and webhook are opt-in. | The log is the one channel that always works, with no service. | 9.6 | Done |
| D25 | **Plan seeding** from a YAML file, idempotent through a hidden `dags-seed` marker, dry run by default. | A twenty-one issue plan with forty-seven links is not worth typing, and a failed run must be safe to repeat. | 3.6 | Done |
| D26 | **The plan is opt-in:** `plan_scope: labelled` by default. Membership is any `swarm:` or `type:` label, or being the ancestor of something that has one. | A plan repository that people also use turned every stray issue into claimable work. | 3.4 | Done |
| D27 | **One coordination repository per swarm, holding no product code.** | Ledger commits swamped a code history, branch protection blocked the daemon's pushes outright, and a worker's worktree sat inside the live ledger. | 4.6 | Done |
| D28 | **The tracker is a human input surface.** `swarm:status:ready` means "a human says this may be worked"; computed readiness is never written back; labels that have stopped applying are retired on completion. | The alternative makes the tracker self-describing at the cost of a write loop that drifts when sync fails. The tracker stays the place humans state intent. | 3.2 | Specified |
| D29 | **A "Request changes" review binds; a comment from a recognised human surfaces without reopening.** | Discussion and instruction are different acts. Conflating them makes every passing remark into work; ignoring comments entirely loses real feedback, so they are shown instead. | 9.4 | Partly done |
| D30 | **The ledger is the source of truth for autonomy**, and the tracker label mirrors it. A plan-level question drops a task out of `auto-pr` **for that run only**; a downgrade can be undone. | A downgrade recorded in the ledger already beat the label, with no way to reverse it — so a single question could permanently change a task's tier. | 8.3 | Specified |
| D31 | **Liveness lives outside the branch**, in `refs/dags/live/<machine>`, migrated by dual-read. | Heartbeats were half of all ledger commits and have no historical value. Moving them makes commit volume a function of work rather than time, and lets beats get *more* frequent with a shorter lease. | 4.5 | Specified |
| D32 | **Worktrees live in the code repository's checkout**, kept out of git by `info/exclude`, with a per-repo override. | Inside the coordination repository, an agent walked up the tree into the swarm's own rules. Outside both repositories, project ancestry resolves to nothing and then to `$HOME`. | 7.3 | Specified |
| D33 | **Nothing upgrades itself.** `bin/` in a coordination repository is a pinned copy; a merged change takes effect only when a human copies it in and restarts each machine. | A swarm that edits the protocol it is running would otherwise rewrite itself mid-flight, and a bad merge would break every machine at once. | 5.6 | Done |

\newpage

# Appendix B. Primer — DAGS in ten minutes

## B.1 What it is

A way for several coding agents, on several machines, to work one backlog without
a server and without stepping on each other. Work is described in an issue
tracker. Coordination happens in a git repository. Code lands as pull requests
that a human merges.

## B.2 Vocabulary

| Term | Meaning |
|---|---|
| **Task** | One unit of work: an issue in the tracker, mirrored as a folder in the ledger |
| **Epic** | A parent of tasks. Blocking an epic halts all of them |
| **Plan repository** | The tracker whose issues are the plan |
| **Coordination repository** (ledger) | The git repository holding claims, checkpoints and control records. One per swarm, no product code |
| **Code repository** | What a task changes. A task names exactly one |
| **Machine** | One clone of the coordination repository with its own identity, running one daemon |
| **Claim** | A record saying a machine intends to work a task. Several may exist; one wins |
| **Lease** | How long a claim survives without a heartbeat. Fifteen minutes by default |
| **Quota** | N in flight across the swarm; a share per machine |
| **Worker** | Whatever implements a task: an AI CLI, or a human in an IDE |
| **Plan** | What the worker says it will do, written before any code, reviewed by a human |
| **Checkpoint** | The task's working memory: plan, summary, what was tried, what remains |
| **Autonomy tier** | How much human review a task needs |
| **The Board** | A terminal UI over the local clone |

## B.3 The life of a task

A human files and labels an issue. A machine mirrors it, finds it ready, claims
it, and dispatches a worker into a fresh worktree. The worker writes a plan; a
human approves it; the worker implements, records what it tried, runs the agreed
tests, and opens a pull request as the bot. A human reviews and merges. The
poller notices, the task is done, the slot frees.

If anything goes wrong — the machine sleeps, the agent dies, the human goes home
— the claim's lease expires and the task returns to the pool with its checkpoint
intact, so the next attempt resumes rather than restarts.

## B.4 Who does what

| | Humans | Machines | Workers |
|---|---|---|---|
| Decide what to build | ✓ | | |
| Decide what to work next | | ✓ | |
| Approve a plan | ✓ | | |
| Write code | | | ✓ |
| Run tests | | | ✓ |
| Open a pull request | | | ✓ (as the bot) |
| Merge | ✓ | | |
| Pause everything | ✓ | | |

## B.5 Where things live

| | Path |
|---|---|
| The plan | Issues in the plan repository |
| The ledger | `tasks/`, `control/`, `priority/`, `quota/` |
| The protocol | `bin/`, committed, pinned per swarm |
| Shared settings | `backend.yaml`, `humans.yaml`, `templates/` |
| This machine only | `.swarm/` |
| A task's code | `<code repo>/.worktrees/<TASK>` on `swarm/<TASK>` |
| A worker's instructions | `<worktree>/.swarm-task/` |

## B.6 Six rules worth remembering

1. **Records are files, and files are append-only.** Two writers never conflict,
   except on the two single-writer files, where the remote wins.
2. **Nobody is in charge.** Every machine computes the same answer from the same
   synced ledger; the lowest `(clock, machine)` wins.
3. **Expiry is computed, not swept.** Nothing has to be running for a dead
   machine's work to come back.
4. **A human gate is a record an agent cannot forge.** The plan gate and the
   merge gate are enforced where the work ends, not only where it starts.
5. **Nothing upgrades itself.** A merged change does nothing until someone copies
   it in and restarts.
6. **If it isn't in the ledger, it didn't happen.** An agent's memory is not
   state.

\newpage

# Appendix C. How-to guides

## C.1 Set up a new swarm

1. Create the coordination repository. It holds `bin/`, `templates/`,
   `backend.yaml`, `humans.yaml` and empty ledger folders — and **no product
   code** (D27).
2. Write `backend.yaml` (§3.5): the plan repository, `plan_scope`, the code
   repositories with their `base` and `test_command`, and the `swarm:` settings.
3. Write `humans.yaml`: everyone whose decisions the swarm will honour.
4. `swarm.py backend init` to see what labels it would create, then `--apply`.
5. Give the bot account write access to each code repository.
6. Protect each code repository's default branch (§11.4). Do **not** protect the
   coordination repository's — the daemon pushes to it directly.
7. Add the test workflow to each code repository (§11.1).

## C.2 Join from a new machine

```bash
git clone <coordination repo> && cd <it>
./bin/swarm.py start --quota-share 1
./bin/swarm.py board
```

Write `.swarm/local.yaml` first if you want to reuse existing checkouts or set
the worker token (§5.4). Start with a share of 1 until a task has gone all the
way through.

## C.3 Write a plan as issues

One issue per task, with `type:task`, `repo:OWNER/NAME`, an autonomy tier, and
`swarm:status:blocked` until you mean it to be worked. Use GitHub sub-issues for
epic membership and "blocked by" for dependencies. For anything large, write a
plan file and `backend seed` it (§3.6).

Remember that `ready` is permission, not availability (§3.2): a task labelled
`ready` whose dependency is still open will not be claimed, and the tracker will
not tell you that. `swarm.py backend ready --ledger` will.

## C.4 Release work to the swarm

```bash
./bin/swarm.py backend ready --ledger          # what is claimable now
./bin/swarm.py backend set-status GH-12 ready  # release one task
```

A `human-must-scope` task will never be claimed by an AI worker, so setting it
`ready` without a human to take it leaves it claimable and idle (§7.3).

## C.5 Work a task yourself

Choose `vscode` or `intellij` when the Board asks. Your IDE opens on the worktree
with `.swarm-task/README.md` as the first tab. Then:

```bash
.swarm-task/swarm-task plan           # writes plan.md — edit it
.swarm-task/swarm-task plan --submit
.swarm-task/swarm-task wait           # blocks until the plan is reviewed
.swarm-task/swarm-task implement
.swarm-task/swarm-task note --summary "..." --tried "..." --remaining "..."
.swarm-task/swarm-task done
```

Nothing reaches a reviewer until `plan --submit` runs. If you forget, the Board
spots the unsubmitted plan and offers to submit it for you (§10.2).

## C.6 Review a plan

`v` on the Board. Approve, or send it back with a note — the note reaches the
worker (§9.3). A plan edited after approval needs approving again (D20).

## C.7 Review a pull request

`O` opens it. Request changes to send it back to the queue with the feedback
attached; `fix:`, `explain:` and `reject-approach:` lines are the actionable
items (§9.4). `m` approves and merges, warning first if the checks are red or
pending. With several pull requests open, merge one at a time and let the default
branch's workflow go green between merges (§11.4).

## C.8 Control the swarm

```bash
./bin/swarm.py pause                  # claim nothing new; keep what is held
./bin/swarm.py resume
./bin/swarm.py throttle 2             # this machine's share, live
./bin/swarm.py quota set 4            # the global cap, for everyone
./bin/swarm.py stop
```

Share 0 drains and releases rather than freezing (§7.2). After a `stop` and
`start`, check whether the machine is still paused — until `#30` lands, a pause
outlives a restart and a restarted machine claims nothing while looking healthy.

## C.9 Settle a conflict

```bash
./bin/swarm.py task freeze GH-12 --reason "wait for the schema decision"
./bin/swarm.py task unfreeze GH-12
./bin/swarm.py task reassign GH-12 <claim-id|machine> --reason "has the toolchain"
./bin/swarm.py epic takeover E1      # one machine works a whole epic
```

## C.10 Two machines on one computer

Two clones of the coordination repository, each with its own identity, both
pushing to the same remote. Give each its own code-repository checkouts in
`.swarm/local.yaml`: if both map the *same* checkout, a task cannot move between
them, because one branch can only be checked out in one worktree (§7.4, `#13`).

## C.11 Upgrade a swarm after a merge

```bash
git -C <code repo> pull --ff-only
cd <coordination repo> && ./bin/swarm.py stop
tools/upgrade-from-source.sh <code repo>      # copies bin/ and templates/
git diff --stat && git add -A && git commit -m "Upgrade to <sha>"
./bin/swarm.py identity show                  # forces the venv rebuild visibly
./bin/swarm.py start --quota-share 2
./bin/swarm.py resume                         # until #30 lands
```

The venv rebuild is silent and takes minutes (§5.2), which is why it is worth
triggering on purpose rather than discovering it inside a test run.

## C.12 Troubleshooting

| Symptom | Likely cause |
|---|---|
| Machine runs, claims nothing | A pause survived a restart (§8.1); or everything is `blocked`; or no share |
| Nothing claimable though issues say `ready` | Dependencies are not done — `ready` is permission (§3.2) |
| `status` hangs | A `gh` call waiting on an unreachable GitHub; no timeout yet (`#60`) |
| A merged pull request still shows as open | The poller's one fetch failed (`#56`) |
| Dispatch fails every cycle | Another clone holds the branch's worktree (`#13`) |
| A "targeted" test run takes as long as a full one | A silent venv rebuild, or a scope passed as a filter (§11.2) |
| A worker never notices its plan was approved | The contract still says to poll (`#58`) |
| `git` refuses: `index.lock` exists | A tool left a stale lock; remove it when no git is running |

\newpage

# Appendix D. Reference

## D.1 Commands

Forty-two commands, derived from `bin/dags/cli.py`. Anything marked **human**
refuses to run unless the operator matches a name in `humans.yaml`.

### The machine

| Command | What it does |
|---|---|
| `start [--quota-share N] [--poll-interval] [--cycle-interval]` | Write a `start` record and spawn the daemon |
| `stop [--wait S]` | Shared stop record plus a signal |
| `pause [--machine M]` / `resume [--machine M]` | Claim nothing new / undo. Idempotent |
| `throttle N [--machine M]` | Change a machine's share, live |
| `status [--json]` | The status panel |
| `log [--task T] [--machine M] [--limit N] [--json] [--heartbeats]` | The ledger's history in plain English, not `git log` |
| `board [--web]` | The Swarm Board |
| `identity show` / `identity set NAME [--force]` | This machine's identity |

### The plan and the tracker

| Command | What it does |
|---|---|
| `backend ready [--ledger]` | What the backend offers; `--ledger` also applies ledger readiness |
| `backend get-task KEY` | One task as the backend sees it |
| `backend set-status KEY STATUS` | Set the `swarm:status` label |
| `backend adopt KEY [--autonomy T]` | Bring one issue into the plan |
| `backend init [--apply]` | Create the swarm labels (**human** for `--apply`) |
| `backend seed FILE [--apply] [--yes]` | Seed a plan file (**human** for `--apply`) |
| `protect REPO [--branch] [--approvals] [--apply] [--yes]` | Branch protection (**human** for `--apply`) |

### Quota and priority

| Command | What it does |
|---|---|
| `quota show` / `quota set N [--reason]` | The global cap (**human** to set) |
| `epic takeover EPIC` / `epic release EPIC` | Reserve an epic for this machine (**human**) |

### Tasks — inspecting

| Command | What it does |
|---|---|
| `task list [--all] [--json]` | Every task, as a table or one object per row |
| `task show KEY` | One task in detail, as JSON |
| `task context KEY` | What the injected skill reads |
| `task events KEY --claim ID` | Pending news for one claim |

### Tasks — human levers

| Command | What it does |
|---|---|
| `task worker KEY a\|b\|c` | Choose the worker for a claim this machine owns |
| `task approve-plan KEY [--reject] [--note]` | The plan gate (**human**) |
| `task answer KEY "..."` | Answer a worker's question (**human**) |
| `task answer-tests KEY --scope S` | Answer a test-scope proposal (**human**) |
| `task freeze KEY --reason` / `task unfreeze KEY` | Nobody owns it / undo (**human**) |
| `task reassign KEY WINNER --reason` | Award a task to one claimant (**human**) |
| `task merge KEY [--force]` | Approve and merge (**human**) |
| `task release KEY` | Give the claim back |
| `task still-working KEY` | Answer the idle prompt |
| `task open KEY [--pr]` | Open the ticket or the pull request |

### Tasks — the worker side

Called by `.swarm-task/swarm-task`, rarely by hand.

| Command | What it does |
|---|---|
| `task submit-plan KEY --file F` | Record the plan; self-approve if `auto-pr` |
| `task note KEY [--summary] [--tried] [--remaining] [--question] [--risk]` | Update the checkpoint |
| `task block KEY "question"` | Ask a human and stop |
| `task propose-tests KEY --worktree W [--accept]` | Propose a test scope from the diff |
| `task test-run KEY --worktree W` | Run the agreed scope |
| `task done KEY --worktree W [--skip-tests]` | The pipeline of §11.3 |

## D.2 `backend.yaml`

| Key | Default | Meaning |
|---|---|---|
| `backend` | — | `github` or `fake` |
| `github.repo` | — | The plan repository |
| `github.use_issue_types` | `false` | Use GitHub issue types instead of `type:` labels |
| `github.cache_seconds` | `20` | Read cache |
| `plan_scope` | `labelled` | `labelled` or `all` (§3.4) |
| `repos.<R>.base` | `main` | The branch tasks target |
| `repos.<R>.test_command` | — | What `done` runs |
| `repos.<R>.test_scope_command` | — | How to run a subset, if different |
| `repos.<R>.checks_timeout` | `300` | Seconds `done` waits for checks |
| `repos.<R>.worktrees` | `inside` | Where worktrees go (§7.3). **Specified, not built** (D32) |
| `default_repo` | — | For tasks with no `repo:` label |
| `swarm.default_quota` | `3` | Global N |
| `swarm.lease_minutes` | `15` | Claim expiry |
| `swarm.heartbeat_minutes` | `3` | Capped at `lease / 3` |
| `swarm.max_retries` | `3` | Failures before the tier drops |
| `swarm.human_idle_hours` | `8` | Before "still working?" |
| `swarm.thrash_threshold` | `2` | Conflict cycles before arbitration |

## D.3 `humans.yaml`

The people whose records the swarm honours. Committed, so the set is reviewed.
A plan review or arbitration naming anyone else is ignored (D16).

## D.4 `.swarm/local.yaml` — per machine, never committed

| Key | Meaning |
|---|---|
| `human` | This operator's name; otherwise matched via `gh api user` |
| `repos.<R>` | An existing checkout to use instead of cloning |
| `worker_token.keychain_service` / `.env` / `none` | Where the bot token comes from |
| `bot.login`, `bot.email` | Commit authorship for worker commits |
| `terminal_app`, `intellij_app` | Which application a launcher drives |
| `notify.desktop`, `notify.webhook` | Opt-in notification channels |

## D.5 Labels

| Label | Values |
|---|---|
| `swarm:status:` | `ready`, `claimed`, `in-progress`, `awaiting-review`, `blocked`, `done` |
| `swarm:autonomy:` | `auto-pr`, `human-must-review`, `human-must-scope` |
| `repo:` | `OWNER/NAME` |
| `type:` | `epic`, `task` |

Default autonomy is `human-must-review`. `ready` is permission, not availability
(§3.2).

## D.6 Ledger records

| Directory | Written by | Appended or replaced |
|---|---|---|
| `tasks/<E>/<T>/meta.yaml` | Plan sync, once | Never rewritten |
| `…/meta/` | Plan sync | Appended; later revisions win per key |
| `…/claims/` | A machine | Appended |
| `…/withdrawals/` | A machine | Appended |
| `…/heartbeats/<machine>.yaml` | That machine only | Replaced |
| `…/checkpoint.yaml` | The claim's owner only | Replaced |
| `…/completions/` | A machine or the poller | Appended |
| `…/plan-reviews/` | A human | Appended |
| `…/arbitration/` | A human | Appended |
| `…/events/` | A machine | Appended |
| `…/test-scope/` | A worker and a human | Appended |
| `control/` | A machine or a human | Appended |
| `priority/` | A human | Appended |
| `quota/` | A human | Appended |

Withdrawal reasons written by the scheduler: `lost-race`, `no-worker-chosen`,
`quota`, `dispatch-failed`. A worker-side release passes its own reason
(`work.py:211`). Expiry writes no record at all — it is computed (§6.5). Completion kinds: `pr-opened`, `done`,
`reopened`, `rejected`, `replanned`.

## D.7 Task states — derived, never stored

`open`, `claimed`, `in-progress`, `awaiting-review`, `done`, `rejected`,
`frozen`, `arbitrated-stale`. Active for quota purposes: `claimed` and
`in-progress`.

Every state is computed by `resolve.task_state` from the records above. No file
holds a task's state, which is why two machines cannot disagree about it without
disagreeing about the ledger itself.

## D.8 Files on a machine

| Path | Committed |
|---|---|
| `bin/`, `templates/`, `backend.yaml`, `humans.yaml` | Yes |
| `tasks/`, `control/`, `priority/`, `quota/` | Yes |
| `.swarm/` | No |
| `<code repo>/.worktrees/<TASK>` | No (`info/exclude`) |
| `<worktree>/.swarm-task/` | No (`info/exclude`), removed when the pull request opens |
