# DAGS — implementation handover

Read this together with the implementation plan (`claude/DAGS-implementation-plan.md`
in the claude.ai project) and the whitepaper. Where the whitepaper's text and
figures disagree, the text wins.
Copies: claude.ai project `claude/DAGS-handover.md` (primary), `docs/HANDOVER.md`
in the coordination repo (for Claude Code), and `~/Documents/DAGS/DAGS-handover.md`.

## Target locations (Román's Mac)
- Coordination repo (DAGS code lives here, in `bin/`): `~/Documents/DAGS/swarm/poc-swarm-ensemble`
  (remote `github.com/romanfq/poc-swarm-ensemble`)
- MatchWire backend: `~/Documents/DAGS/matchwire/be/matchwire-backend`
- MatchWire frontend: `~/Documents/DAGS/matchwire/fe/matchwire-frontend`

## Decisions made

Reviewed against the code on 2026-09-16. The full rationale, and how each
decision changes the whitepaper, is in the spec v1.1 (`claude/DAGS-spec.md`,
Appendix A; PDF at `~/Documents/DAGS/DAGS-spec-v1.1.pdf`). "Plan §x" refers to `claude/DAGS-implementation-plan.md`.

**Settled by Román**
- **D1: code location.** The code lives inside the coordination repo, in `bin/` (plan §2.15).
- **D2: bot account for worker PRs** (plan §2.1).
  - Token: macOS Keychain, service `dags-worker-token`. Alternatives are the
    `DAGS_WORKER_GH_TOKEN` env var, or `worker_token: none` in `.swarm/local.yaml`
    (PRs under your own account; admin-bypass mode, no longer agent-proof).
  - Only the PR step (`gh pr create` / `gh pr comment` in `swarm-task done`)
    uses the bot token. Pushes use the human's normal git credentials.
  - Worker commits are authored as the bot when `.swarm/local.yaml` has
    `bot: {login, email}`.
  - Humans approve and merge with their own `gh auth`.
- **D3: macOS only** for worker launchers (plan §2.14): Terminal/iTerm via
  `osascript`, `open -na "IntelliJ IDEA.app"`, `code -n`.
- **D4: Phase 8 (Jira adapter) deferred.** There's no Jira instance (`FutureWork.md`).
  `backend: jira` fails with a pointer there.
- **D5: two agents, one baton.**
  - Cowork writes code; Claude Code runs what needs the real Mac.
  - `BATON` names the only agent allowed to edit.
  - Hand-offs are local commits, never pushed. `CLAUDE.md` holds Claude Code's rules.

**Plan defaults adopted** (no objection raised)
- **D6: leases (§2.3).**
  - Leases use wall-clock time, corrected for clock skew: each process
    measures its offset from GitHub's `Date` header (`gh api -i /meta`).
  - Lease 15 min, heartbeat every 3 min (`backend.yaml` → `swarm:`).
- **D7: single-writer files (§2.4).**
  - `heartbeats/<machine>.yaml` and `checkpoint.yaml` are written only by the
    current winner, which re-checks `resolve()` after pulling.
  - One heartbeat commit per cycle for all of a machine's claims.
  - The retry count is computed, never stored. `meta.yaml` never changes;
    later tracker changes are stored as `meta/` revisions.
- **D8: daemon and Board (§2.2).**
  - `start` spawns a detached daemon (scheduler, heartbeat and poller
    threads), with `.swarm/daemon.pid`, `.swarm/daemon.json` and `.swarm/swarm.log`.
  - The Board is a separate foreground process: `swarm.py board` or
    `start --attach`. With no daemon running, the Board runs its own poller.
  - `stop` writes a shared stop record, then sends SIGTERM.
- **D9: global quota (§2.5).** Global N lives in `quota/<human>-<clock>.yaml`;
  the latest clock wins. Only `humans.yaml` names count. The default is
  `swarm.default_quota`.
- **D10: target repo (§2.6).** First match wins:
  1. the `repo:OWNER/NAME` issue label;
  2. `epic_repos:` in `backend.yaml` (keyed by epic key or short key);
  3. `default_repo:`.
  - Epics have no repo. A task without one is never claimed.
  - `repos:` holds per-repo `base` and `test_command`; it doesn't whitelist repos.
- **D11: plan sync and Done (§2.7).**
  - Every scheduler cycle mirrors the tracker into `tasks/`.
  - Done means the PR was merged, recorded by the poller or by the Board's
    Approve & merge. A task closed by hand in the tracker imports as done too.
  - Readiness: the backend doesn't hold the task back, and the ledger says its
    dependencies are done, it's not frozen, and there's no live claim or open PR.
- **D12: autonomy tiers (§2.8).**
  - `auto-pr`: the AI worker may self-approve its plan.
  - `human-must-review`: a human approves the plan before `implement`/`done`.
  - `human-must-scope`: never goes to an AI worker.
  - After `max_retries` failed claims (expired or given up, not lost races),
    the tier drops one step. The backend label changes first, then the ledger.
- **D13: idle limit (§2.9)** — after `human_idle_hours` with no progress, the
  owner is asked "still working?". With no answer within one more lease,
  heartbeats stop and the claim expires.
  - The plan applied this to human workers only; the build applies it to
    every worker, since an AI terminal can die silently too.
- **D14: tests instead of CI (§2.10).**
  - `done` runs the repo's `test_command` and refuses to open a PR if it fails.
  - Merge refuses only if the PR's status checks (`gh pr view --json
    statusCheckRollup`) are failing (`--force` overrides), or if the PR is no
    longer open. No checks at all is fine.
- **D15: command corrections (§2.11).**
  - Use `gh issue edit --add-blocked-by`; the prerequisite check looks for it.
  - Branch protection is set with a full JSON body via `swarm.py protect`
    (a dry run unless `--apply`).
  - `swarm.py board --web` installs textual-dev when needed and serves on port 4590.
- **D16: arbitration trust (§2.12), only partly implemented.**
  - Arbitration, quota and plan-review records from names not in `humans.yaml`
    are ignored.
  - The planned check that the commit author's email matches `humans.yaml` is
    **not built yet** (`Coord.author_email` exists but nothing uses it).
- **D17: locations (§2.13).**
  - `.worktrees/<TASK>` on branch `swarm/<TASK>`, and `.swarm/`, both
    git-ignored in the coordination clone.
  - Code repos: the path mapped in `.swarm/local.yaml` → `repos:`, otherwise
    `.swarm/repos/OWNER/NAME`, cloned with `gh`.

**Build decisions (made during implementation)**
- **D18: GitHub reads** use one paginated `gh api graphql` query (parent,
  subIssues, blockedBy, issueType). Writes use `gh issue edit/close/comment`.
  Without type labels, an issue with sub-issues counts as an epic.
- **D19: task keys.**
  - GitHub keys look like `OWNER/REPO#N`, with short name `GH-N`.
  - Ledger directories: `tasks/<EPIC-short>/<TASK-short>`, epics at
    `tasks/<EPIC>/_epic`, tasks without an epic under `tasks/_no-epic/`.
  - Every command accepts the full key, the short key or the directory name.
- **D20: plan review gate.**
  - The plan text and its hash live in `checkpoint.yaml` (`plan_md`, `plan_sha`).
  - Approvals are append-only `plan-reviews/` records, so any human on any
    machine can approve.
  - A new plan needs a new approval.
- **D21: the skill stays stdlib-only.** `.swarm-task/swarm-task` delegates to
  `swarm.py task …`, using the venv Python recorded in `.swarm-task/context.json`.
  (The plan said `context.yaml`; JSON keeps the skill stdlib-only.)
- **D22: outcome records** in `completions/`: `pr-opened`, `done`, `reopened`,
  `rejected` and `replanned`.
  - A new CHANGES_REQUESTED review → `reopened`. The task returns to the queue
    and any machine may resume it from the checkpoint and PR feedback; the next
    `done` comments on the same PR.
  - A PR closed without merging → `rejected`. The backend status becomes
    `blocked` until a human sets `swarm:status:ready`, which records `replanned`.
- **D23: venv.**
  - `swarm.py` creates `.swarm/venv` on first run and re-executes itself inside it.
  - The venv is stamped with platform and Python version and rebuilt if it came
    from elsewhere. A dev venv (`bin/dev-setup.sh`) also serves normal runs.
  - `typer>=0.16`, because older typer breaks with click 8.2+.
  - typer 0.17+ bundles its own click (`typer._click`); `dags/cli.py` uses
    whichever is present (fix found on the Mac).
- **D25: plan seeding.** `swarm.py backend seed FILE [--apply] [--yes]`
  (`bin/dags/seed.py`, plus seeding helpers on `GitHubBackend`).
  - Reads a YAML plan (`poc/matchwire/plan.yaml`) and creates the missing
    labels, issues, parents (`gh issue edit --parent`) and dependencies
    (`--add-blocked-by`).
  - Each issue body carries `<!-- dags-seed: ID -->`, so re-runs are no-ops
    and an interrupted run resumes. Existing issues are never rewritten;
    differences are printed as notes.
  - After `--apply` it re-reads the tracker and fails if anything still differs.
- **D24: notifications** always go to `.swarm/notifications.log`.
  `notify: {desktop, webhook}` in `.swarm/local.yaml` adds macOS notifications
  and a webhook. The Board shows the log in its activity feed.

**Still open**
- Whether the MatchWire repos are public or private. On GitHub Free, branch
  protection only applies to public repos.
- Whether to build K4 (below).

**Known gaps** (found when the spec was checked against the code on 2026-09-16)
- **K1, fixed:** Board `m` / `task merge` used to leave the task's worktree
  behind. Merge now removes it, and every poller sweeps the worktrees of tasks
  that are done or rejected.
- **K2, fixed:** lowering N or a quota share used to leave running tasks alone.
  - The newest claims now yield: a claim with no worker is withdrawn at once
    (reason `quota`).
  - A running task gets `pause_requested`, which the skill shows and which
    blocks `implement`. It is withdrawn one lease later.
  - The task resumes from its checkpoint when a slot frees. Raising the quota
    during the grace period lifts the request.
  - `quota` withdrawals don't count as failures.
- **K3, fixed:** `--identity` on a fresh clone is written to `.swarm/identity`.
  On a named clone it applies to that command only, with a warning.
  `swarm.py identity show|set` shows or renames it.
- **K4, open:** D16's commit-author check isn't built.


## Environment findings (2026-09-16)
- The Cowork "device shell" on the Mac is an isolated **Linux** VM (Python 3.10)
  with **no network**: PyPI and GitHub both return 403, and it has no `gh` or `pytest`.
- The cloud sandbox also can't reach PyPI (org egress policy), so typer, rich and
  textual can't be installed anywhere from Cowork.
- Cowork runs tests in its cloud sandbox (Python 3.11, pytest, pyyaml).
- `.swarm/venv` must be created on the Mac itself: `./bin/dev-setup.sh`.
  - `swarm.py` stamps the venv with platform and Python version.
  - A venv built elsewhere (for example in that Linux VM) is rebuilt automatically.

## Build status

- **On the Mac** (Claude Code, 2026-09-16, commit d4dd546): 179 tests pass,
  none skipped, in 11m54s. That includes `tests/test_cli.py`,
  `tests/test_board.py` and the end-to-end skill test. Details are below.
- **In the Cowork cloud sandbox** (2026-09-16, Phase 9 seeder): 193 pass,
  3 skipped (typer). The new `tests/test_cli.py::test_backend_seed_dry_run_then_apply`
  first runs on the Mac.
- **In the Cowork cloud sandbox** (2026-09-16, after the K1–K3 fixes): the
  suite passes; the typer/rich/textual modules skip there. `ruff check`
  (F, E9, B) is clean. The K1–K3 fixes added tests; the new CLI identity test
  will first run on the Mac.
- **Speed:** the daemon test no longer waits 20 s for a heartbeat cycle; it
  now sets `heartbeat_interval`. The Mac run is slower than the cloud (about
  2 min) because the gitsync and poller tests do real git work under
  `~/Documents`.

| Phase | Status |
|---|---|
| 0 Scaffolding | done |
| 1 Deterministic core | done, tested |
| 2 Git sync + gh wrapper | done, tested |
| 3 Issue backend port | done, tested (Jira deferred) |
| 4 `swarm.py` bootstrap | done, tested (Mac run for the typer CLI) |
| 5 Scheduler, workers, skill | done, tested (Mac run for the end-to-end skill test) |
| 6 Poller + notifications | done, tested |
| 7 Swarm Board | done, tested (Mac run for the Textual pilot tests); smoke-tested by hand |
| 8 Jira adapter | deferred (`FutureWork.md`) |
| 9 POC on MatchWire | in progress: plan file, seeder and config done; GitHub steps with Claude Code (below) |

The spec v1.1 is `claude/DAGS-spec.md` in the project, `docs/SPEC.md` in the
repo, and `~/Documents/DAGS/DAGS-spec-v1.1.pdf`. It describes the code as
built, including the K1–K3 fixes.

### Mac test run (Claude Code, 2026-09-16)
Done on Román's Mac by Claude Code on 2026-09-16 (commit "DAGS: run the
typer/rich/textual tests on the Mac"):

- `./bin/dev-setup.sh` built `.swarm/venv` (macOS, Python 3.11.7) and installed
  typer 0.27.2, rich 15.0.0, textual 8.2.8, pyyaml 6.0.3, pytest 9.1.1.
- **The whole suite now runs on the Mac: 179 pass, 0 skipped, 11m54s.** It is
  slow because the scheduler heartbeat/idle-limit, poller and gitsync tests wait
  on real clocks and real git. The three things that had never run anywhere —
  `tests/test_cli.py`, `tests/test_board.py` and the end-to-end test in
  `tests/test_skill.py` — all pass.
- Two real bugs, both in code written without being able to run it. Fixed in
  the code, not in the tests:
  1. **`dags/cli.py`: typer >= 0.17 no longer depends on click**, it vendors it
     as `typer._click`. `import click` raised `ModuleNotFoundError`, so
     `tests/test_cli.py` could not even be collected and `swarm.py` would not
     start on a fresh venv. The module now takes the vendored click when it is
     there and the real package otherwise, and builds `CONTROL_FLOW` from
     whichever exception classes exist (in current typer, `Exit` and `Abort`
     live only on typer itself).
  2. **`board.py`: `PlanScreen.task` collided with Textual's read-only
     `MessagePump.task` property** — `AttributeError: property 'task' of
     'PlanScreen' object has no setter` as soon as the plan-review modal opened
     (`v` on the Board). Renamed to `task_key`. Same family as the earlier
     `run_action`/`open_url` collisions.
- Smoke tests, all clean:
  - `./bin/swarm.py --help` (bootstraps/uses the venv);
  - `DAGS_NO_VENV=1 .swarm/venv/bin/python bin/swarm.py status` — prints the
    panel; `operator unknown` and `quota-share ?` are expected while
    `humans.yaml` and `.swarm/local.yaml` hold placeholders;
  - `./bin/swarm.py board` driven on a real pty: it renders (quota gauge, live
    claims, awaiting review, feed) and `q` exits 0, restoring the terminal.
- Bot token: `security find-generic-password -s dags-worker-token -w` succeeds.
  Not printed, not committed.
- Nothing touched on GitHub, nothing in the MatchWire repos, nothing pushed.
  `backend.yaml` still has its `OWNER/...` placeholders.
- `ruff` is not installed on the Mac and is not in `bin/requirements-dev.txt`,
  so the lint gate was not re-run here. Cowork's `ruff check` (F, E9, B) still
  covers it; the two fixes above are import-level and a rename.

### Phase details


- **1. Deterministic core.** `bin/resolve.py`, `dags/records.py`, `dags/timeutil.py`.
  - `resolve.lookup()` finds a task by full key, short key or dir name.
  - `resolve.plan_status()` implements the review gate.
- **2. Git sync + gh wrapper.** `dags/gitsync.py`, `dags/gh.py`.
  - Tests: `tests/test_gitsync.py` races four clones of a bare remote (zero
    conflicts, the same `resolve()` result everywhere), covers the add/add
    meta-import race and the single-writer conflict (remote wins).
    `tests/test_gh.py`.
- **3. Issue backend port.**
  - `backends/base.py` (the port, plus `all_tasks` and the shared `ready_from`).
  - `backends/github.py` (GraphQL reads, label writes, `init_commands`).
  - `backends/fake.py`.
  - `dags/plan.py` (plan sync): import, meta revisions, imported-done,
    replanned-after-reject, stale status reset, autonomy downgrade.
  - Tests: `tests/test_backend_contract.py` (fake, github, github-with-issue-types),
    `tests/test_github_backend.py`, `tests/test_plan_sync.py`.
- **4. `swarm.py` bootstrap.**
  - `bin/swarm.py`.
  - `dags/venv.py`: self-install and re-exec, stamped by platform and Python.
    A dev venv also serves normal runs. `DAGS_NO_VENV=1` skips all of it.
  - `dags/prereqs.py`, `dags/repos.py`.
  - `dags/daemon.py`: detached daemon, `.swarm/daemon.pid`,
    `.swarm/daemon.json`, `.swarm/swarm.log`.
  - `dags/panel.py`, `dags/cli.py`.
  - Tests: `tests/test_bootstrap.py`, `tests/test_cli.py`.
- **5. Scheduler, workers, skill.**
  - `dags/scheduler.py`: the Scheduler, plus a Heartbeater with the idle limit.
  - `dags/worktree.py`, `dags/work.py`, `dags/actions.py`, `bin/workers/`,
    `bin/skill/`, `dags/notify.py`.
  - Tests:
    - `tests/test_scheduler.py`:
      - claim, prepare and prompt;
      - every worker launcher;
      - default worker;
      - human-must-scope;
      - macOS-only launchers;
      - race and withdraw;
      - quota share, global N and throttle;
      - pause, resume and stop;
      - soft takeover;
      - dependencies and blocked epic;
      - arbitration;
      - resume on another machine from the pushed branch and checkpoint;
      - heartbeats and idle limit;
      - release.
    - `tests/test_work.py`:
      - plan gate (human review, changes requested, self-approve, unknown human);
      - notes, block and templates;
      - `done` refusals (no approval, no summary, no changes, red tests,
        lost claim before push);
      - the full output contract (template commit by the bot author,
        `.swarm-task` never committed, push, PR opened with the bot token,
        ledger and backend updated);
      - PR update after changes are requested.
    - `tests/test_workers.py`.
    - `tests/test_skill.py`: stdlib-only check; the end to end run needs typer.
- **6. Poller + notifications.** `bin/poll.py`, `dags/feed.py`, `dags/snapshot.py`.
  - Tests (`tests/test_poll.py`):
    - quiet first run, then the feed;
    - contract watcher (strip `.swarm-task/`, then announce);
    - merge → done and dependants unblocked;
    - closed PR → rejected → re-planned;
    - changes requested → reopened exactly once across machines;
    - overlap;
    - stale heartbeat;
    - conflict → needs arbitration → cleared by arbitration;
    - feed wording;
    - notifier (log, desktop, webhook, listeners).
- **7. Swarm Board.**
  - `bin/board.py` (Textual) is a thin layer over `dags/boardview.py`
    (plain data) and `dags/actions.py` / `dags/work.py`.
  - Panels: live claims, awaiting review (live gh status), needs arbitration
    (poller thrash flags plus live conflicts), plans awaiting review, the quota
    gauge, the machine line, and the activity feed (ledger events plus the
    daemon's `.swarm/notifications.log`).
  - Keys:
    - `p` / `r` / `t` / `s`: pause, resume, throttle, stop;
    - `n`: global N;
    - `f`: freeze/unfreeze;
    - `a`: reassign;
    - `e`: take over / release epic;
    - `v`: review plan;
    - `m`: approve & merge;
    - `o` / `O`: open ticket / PR;
    - `w`: choose worker;
    - `q`: quit.
  - The worker prompt pops up by itself (Ch.10.7).
  - With no daemon running, the Board runs its own poller.
  - Tests: `tests/test_boardview.py` (here), `tests/test_board.py`
    (Textual pilot, run on the Mac).
- **Also added:** `CLAUDE.md`, `BATON`, `FutureWork.md`, `bin/dev-setup.sh`,
  `pyproject.toml` (pytest and ruff config).
- **K1–K3 fixes (Cowork, 2026-09-16):**
  - `dags/worktree.cleanup`, used by `actions.merge`, and
    `Poller._sweep_worktrees`;
  - `resolve.over_quota` and `Scheduler.enforce_quota`;
  - `Context(persist_identity=)`, `swarm.py identity show|set`;
  - the skill shows `pause_requested`.
  - Tests: `tests/test_poll.py` (two new), `tests/test_scheduler.py`
    (three new), `tests/test_bootstrap.py` (identity), `tests/test_cli.py`
    (identity commands).

## Phase 9 inputs (Román, 2026-09-16)
- Plan repo: `romanfq/matchwire-spec`, with issues created there.
- `romanfq/matchwire-backend` and `romanfq/matchwire-frontend` are both
  **public**, so branch protection works on GitHub Free.
- The bot account exists; its token is in the Keychain (`dags-worker-token`).
- GitHub login: `romanfq`.

Done by Cowork:
- `poc/matchwire/plan.yaml`, written from `~/Documents/DAGS/matchwire/MatchWire-Spec.md`:
  - 3 epics: BE, FE and E2E.
  - 18 tasks: S1 (backend scaffold), E1, E1b, E2–E6, E4b, S2 (frontend
    scaffold), E7–E13, E14.
  - Dependencies follow the spec's §7 table. S1 and S2 come first.
  - Autonomy follows §7: E7 is `human-must-scope`, E12 is `auto-pr`, the rest
    are `human-must-review`.
  - E14 targets `matchwire-frontend`.
  - Seeding it makes 13 labels, 21 issues, 18 parent links and 29
    dependencies (checked against the fake tracker).
- `backend.yaml` now names the real repos. The frontend test command is
  `CI=true npm test --silent`, so Vitest runs once instead of watching.
- `humans.yaml`: `roman` / `romanfq`.
- The Mac's `.swarm/local.yaml` maps both repos to their checkouts under
  `~/Documents/DAGS/matchwire/`. `bot:` is not filled in yet.

Why this goes to Claude Code: the Cowork cloud sandbox can't reach these
GitHub repos, and the Cowork shell on the Mac has no network.

## Next (holder: claude-code)
**The meta swarm's first task runs in this repo.** Román starts `dags-a` from
`~/Documents/DAGS/swarm/dags-meta` with `--quota-share 1`. It claims
**#18** (the opt-in change: only issues carrying a swarm label belong to the
plan) — the gate was checked and #18 is the only claimable task.

**Why you hold the baton:** the worker *is* a Claude Code session, editing this
repo in a worktree under `~/Documents/DAGS/code/poc-swarm-ensemble` on branch
`swarm/GH-18`. The baton rule says only `claude-code` edits this repo, so it
stays with you until the task is merged. Cowork will not touch this repo
meanwhile; it keeps `dags-meta` and the logs.

**If you are the worker session** (started by the Board in a worktree): follow
`.swarm-task/README.md` exactly — `plan`, wait for Román's approval, then
`implement`, then `done`. Stay inside your worktree, and change only `bin/`,
`tests/` and `docs/`. Never touch `tasks/`, `control/`, `quota/`, `priority/`,
`BATON`, `CLAUDE.md`, `backend.yaml` or `humans.yaml`. Don't run `swarm.py`
commands other than through `.swarm-task/swarm-task`. Nothing will tell you when
the plan is approved (that is issue #2): ask Román, or poll
`.swarm-task/swarm-task status`.

**If you are helping Román outside the worktree:** review the PR before merge —
it must touch only the three allowed paths — and after merge run, in
`dags-meta`, `tools/upgrade-from-source.sh ~/Documents/DAGS/code/poc-swarm-ensemble`,
commit there, then stop and start the machine so the swarm picks up its own fix.
Don't edit files in `dags-meta`; running its scripts is fine.

**Known rough edges, expected on this run:** no notification when a plan is
approved (#2); daemon errors invisible on the Board (#10); links not clickable
(#5); `done` accepting an empty round (#14). Note anything new as a
`next-version` issue and add it to #7.

**Hand back** to cowork when #18 is merged and the machine restarted, with what
happened and anything worth filing.

## Meta swarm plan labelled (Claude Code, 2026-09-19)
**The meta swarm's plan is labelled and ordered. Nothing is running.**
Done on the Mac by Claude Code, 2026-09-19, with Román's yes at each GitHub step.

### Issues filed (all `next-version`, all added to #7)
| # | Item |
|---|---|
| [#17](https://github.com/romanfq/poc-swarm-ensemble/issues/17) | Dogfooding: run a separate "meta" swarm that works on DAGS itself (Item 12) |
| [#18](https://github.com/romanfq/poc-swarm-ensemble/issues/18) | The plan should be opt-in: only issues carrying a swarm label belong to the swarm (Item 13) |
| [#19](https://github.com/romanfq/poc-swarm-ensemble/issues/19) | `swarm.py protect` has no `--yes` (Item 14a) |
| [#20](https://github.com/romanfq/poc-swarm-ensemble/issues/20) | `task list` has no `--json` (Item 14b) |
| [#21](https://github.com/romanfq/poc-swarm-ensemble/issues/21) | The seed CLI test has never run (Item 14c) |

#7 now checklists #1–#6 and #8–#21, and its three "known from the build" lines
for `protect --yes`, `task list --json` and the never-run seed test are ticked
off, each pointing at its new issue.

### Labelling
Labels were **not** re-created: Román had already run `backend init --apply`
from `dags-meta`. `OPTIN=18 EXTRA="19 20 21" tools/label-issues.sh` was shown as
a dry run (41 `gh issue edit` commands), then applied. Nothing in `dags-meta`
was edited.

### The one surprise: #17 was not in the plan
`tools/label-issues.sh` covers 20 of the 21 issues — #17 is in neither `PLAN`
nor `EXTRA`. That does **not** make it invisible today: `plan sync` imports every
open issue, and `ready_from` (`bin/backends/base.py:99`) holds back only epics,
closed issues and ones labelled `blocked`/`done`. An unlabelled issue therefore
arrives as a claimable task with the default autonomy and
`default_repo: romanfq/poc-swarm-ensemble`. Applying as-is would have left **two**
claimable tasks.

Román chose to label it by hand, so outside the script #17 got `type:task`,
`swarm:autonomy:human-must-review`, `swarm:status:blocked`,
`repo:romanfq/poc-swarm-ensemble`, `--parent 7` and `--add-blocked-by 18`.
**Cowork may want to add #17 to `PLAN`** so the whole plan comes from one place;
the labels already on the issue make that a no-op re-run.

It is a neat demonstration of #18: the gap only exists because membership of the
plan is an accident of being open, rather than a label.

### Verification
- `./bin/swarm.py plan sync` from `dags-meta`: **21 imported**, ledger commit
  `9678ec1`, which pushed itself (`plan.sync` defaults to `push=True`). All 21
  live under `tasks/GH-7/`, the epic.
- `./bin/swarm.py backend ready --ledger`: **exactly one task —
  romanfq/poc-swarm-ensemble#18**, as intended.
- `./bin/swarm.py backend ready` (tracker only) lists **nine**: #3, #5, #10, #15,
  #16, #18, #19, #20, #21. Eight of them carry `swarm:status:ready` while being
  blocked by #18, so only the ledger knows they are not claimable. That is #8's
  complaint seen from the other side, and worth remembering when reading the
  tracker during the dogfooding run.

### Not done, deliberately
- **No swarm was started.** The meta swarm's step 5 (`start --quota-share 1`)
  is untouched, and both MatchWire machines are still stopped.
- **Branch protection on `poc-swarm-ensemble` was not enabled** — it would block
  the MatchWire ledger pushes.

## Phase 9 pause and triage (Cowork, 2026-09-18)
**The swarm is stopped and Phase 9 is paused.** Both machines have `stop`
records — `teammate-b-stop-401` (21:46:01) and `macbookpro-68b8-stop-412` — and
no daemon is running on the Mac. Nothing claims, prompts or heartbeats, so the
ledger is quiet and safe to read.

### Where the scenarios stand
- **Done earlier:** pause/resume, throttle, a second machine joining, plan
  review and approval, and a first PR opened by the bot.
- **Scenario 6 (request-changes loop): half passed.** The mechanics work —
  Román's `CHANGES_REQUESTED` review with `fix: rewrite the README description
  from the MatchWire spec` produced a `reopened` completion, the task was
  re-claimed and re-dispatched, and `done` commented on the same PR. But both
  `pr-opened` records name the same commit `c3d850f`, the README was never
  rewritten, and the feed announced "finished" a second time. Filed as **#14**.
- **Not started:** merging PR #1 (scenario 9: `GH-5`/`GH-6` unblock, scenario 2:
  both machines race), arbitration and freeze, `kill -9` plus lease expiry and
  resume, reject-approach, epic takeover.

### Live state to resume from
- `GH-4` (romanfq/matchwire-spec#4): awaiting review,
  romanfq/matchwire-backend#1 open and `CHANGES_REQUESTED`, one commit,
  the requested README rewrite still not done. This clone's `.worktrees/GH-4`
  is intact and must stay while this machine owns the task (#13).
- `GH-13` (romanfq/matchwire-spec#13): claim lapses with the daemon; the plan is
  approved and its checkpoint intact, so a worker can resume it. **Its tracker
  label is still `swarm:status:ready`**, so the thrashing of #12 restarts with
  the swarm unless someone runs
  `swarm.py backend set-status GH-13 blocked` first. Román released the claim
  but had not parked the task when the swarm was stopped.
- Both code worktrees and every checkpoint are untouched.

### Warning for the next run
The Mac sleeps, and the daemon only runs while it is awake — measured at ~27% of
wall time. Every claim churns across a sleep (#12). Run `caffeinate -is`
alongside the swarm for any scenario involving two machines racing, or expect
the noise.

### Issues filed this session
#10 (Board has no log; a dispatch failure was invisible), #11 ("Not now" and
taking a machine out of rotation, from the draft), #12 (a claim nobody answers is
re-claimed forever, with a design), #13 (a task cannot move machines while the
first holds its branch worktree), #14 (`done` re-announces with no new commit),
#15 (`daemon.json`'s `started_utc`), #16 (namespace branches by plan repo).
All labelled `next-version` and checklisted in the pinned #7, which now covers
#1–#6 and #8–#16.

**Nothing in `bin/` was changed.** Every finding is filed, not fixed: both
daemons and every worker run that code, and the checklist forbade touching it
while the swarm was live.

## Next-version issues completed (Claude Code, 2026-09-17)
**Done: the next-version issues are complete and correctly cross-linked**
(Claude Code, 2026-09-17).

1. **Item 9 filed as [#9](https://github.com/romanfq/poc-swarm-ensemble/issues/9)**
   — "Board: spot a plan.md that was written but not submitted, and offer
   'submit and review'", body verbatim from the draft, `next-version` label.
2. **Corrections applied** to #1, #2, #3, #6 and #8, exactly as the Corrections
   section listed and nothing else:
   - #1 `Seen on GH-4 (S1).` → `` Seen on `GH-4` (romanfq/matchwire-spec#4, S1). ``
   - #2 both mentions (the worker quote and "the case that happened on GH-4")
   - #3 `"roman approved plan for GH-4"`
   - #6 `Seen on matchwire-backend#1 (GH-4):` → fully qualified, `GH-4` in backticks
   - #8 `#5 … #20` → `romanfq/matchwire-spec#5` / `romanfq/matchwire-spec#20`
   - #4 and #5 say `GH-N`, a placeholder, so they were left alone.
3. **#9 added to #7's checklist**, after the #8 line. #7 now lists #1–#6, #8, #9
   and keeps the "known from the build" list. It is still pinned.
4. **Verified.** All nine bodies were re-read from GitHub and scanned for
   anything GitHub would autolink into *this* repo (bare `#<n>` or `GH-<n>`
   outside backticks, ignoring `owner/repo#<n>`). The only ones left are
   deliberate: #7's own checklist, and #9's "a feed event once #3 lands", which
   really does mean this repo's #3.

**The pattern, for whoever writes the next one:** a swarm short key is written
`` `GH-4` (romanfq/matchwire-spec#4) `` — backticks stop GitHub turning `GH-4`
into a link to this repo's #4, and the qualified form links where it should.
Bare `#<n>` means an issue in this repo and nothing else.

Nothing else on GitHub was touched, and no `swarm:*` labels were used here.

## Phase 9 status and Item 9 (Cowork, 2026-09-17)
Román handed the baton to Cowork on 2026-09-17 to carry on with **Phase 9**.

**Phase 9 status.** The running log is `claude/DAGS-phase9-results.md` in the
DAGS project.
- Two machines are running:
  - `macbookpro-68b8` (this clone): share throttled to 3 (Board `t` pressed twice);
  - `teammate-b` (`~/Documents/DAGS/swarm/teammate-b`): share 1, default worker `claude`.
- Global N is 3.
- GH-4 (S1) has PR matchwire-backend#1 open. GH-13 (S2) is being worked in VS
  Code, with its plan approved.
- Nothing else is claimable until PR #1 is merged. Merging it unblocks GH-5 and
  GH-6: the dependency-unblocking and claim-race scenarios.
- Done so far: pause/resume, throttle, and a second machine joining.
- Still to run: the normal flow to merge, claim race, arbitration and freeze,
  killing a daemon to test lease expiry and resume, the request-changes loop,
  reject approach, and epic takeover.

**Item 9 is ready to file.** The earlier copy never reached the Mac; it is now
at the end of `~/Documents/DAGS/next-version-issues.md` ("Item 9 (file after
#8)").
- Claude Code may file it without holding the baton, because filing needs no
  repo edits:
  `gh issue create --repo romanfq/poc-swarm-ensemble --label next-version ...`,
  then add its line to #7 after #8.
- Tell Román the number; Cowork records it here.
- Claude Code's code trace of the gap (below) matches the draft.

## Next-version issues (Claude Code, 2026-09-17)
**The next-version issues are filed** in `romanfq/poc-swarm-ensemble`, all
labelled `next-version` (created 2026-09-17). #1–#7 use Cowork's drafts in
`~/Documents/DAGS/next-version-issues.md` verbatim; #8 was written from Román's
one-line note in the chat:

| # | Item |
|---|---|
| [#1](https://github.com/romanfq/poc-swarm-ensemble/issues/1) | Sandboxing: workers and the Board must not change the swarm or its scripts |
| [#2](https://github.com/romanfq/poc-swarm-ensemble/issues/2) | Nannying: workers aren't told when a human or the swarm acts on their task |
| [#3](https://github.com/romanfq/poc-swarm-ensemble/issues/3) | Activity feed: plan approvals (and other worker/human events) don't show |
| [#4](https://github.com/romanfq/poc-swarm-ensemble/issues/4) | Board: let me defer a newly claimed task (snooze, back in an hour, pause) |
| [#5](https://github.com/romanfq/poc-swarm-ensemble/issues/5) | Board: links aren't clickable |
| [#6](https://github.com/romanfq/poc-swarm-ensemble/issues/6) | PR lifecycle on the Board: comments, reviews, send fixes back to the worker |
| [#8](https://github.com/romanfq/poc-swarm-ensemble/issues/8) | Labels: retire the ones that stop applying as a task moves through its lifecycle (added 2026-09-17, after #7) |
| [#9](https://github.com/romanfq/poc-swarm-ensemble/issues/9) | Board: spot a plan.md that was written but not submitted, and offer "submit and review" |
| **[#7](https://github.com/romanfq/poc-swarm-ensemble/issues/7)** | **Next version: things to fix** — the tracking issue, **pinned**, checklist links #1–#6, #8 and #9, and keeps the "known from the build, not yet filed" list |

- The `next-version` label was created first (`1d76db`, "Fix or improve in the
  next DAGS version").
- No `swarm:*` labels were used, as instructed. The swarm's own plan stays in
  `romanfq/matchwire-spec`.
- Issues are authored by `romanfq`, not the bot. The bot is only for PR
  authorship, so approvals work under branch protection.

#8 came from Román after the tracking issue existed, so it was filed the same
way and its checklist line was appended to #7 with `gh issue edit 7 --body-file`.
Its body is grounded in the code: `_swap_label`
(`bin/backends/github.py:215`) only swaps labels *within* a prefix, so nothing is
retired when it merely stops applying. Live examples at the time: #5 and #20 in
`matchwire-spec` both read `swarm:status:ready` while blocked by dependencies,
because `seed`/`plan sync` write `ready` to every task and real readiness is
computed in the ledger (`resolve.deps_done`) and never written back.

**Item 9 was filed later the same day as #9**, once Cowork re-saved its text
to `~/Documents/DAGS/next-version-issues.md`. The first attempt failed because
that file still ended at Item 6 on the Mac; see the Next section above.

**If Román sends more items:** file each one the same way
(`gh issue create --repo romanfq/poc-swarm-ensemble --label next-version
--title ... --body-file ...`) and add a checklist line to #7.

**One local change worth knowing about.** Claude Code's permission classifier
blocked `gh issue create` roughly half the time, so Román added
`.claude/settings.local.json` with allow rules for `gh issue create/edit/list/pin`
and `gh label`. That file is machine-local and is now in `.gitignore`; a fresh
clone will hit the same prompts until it has its own copy.

## Phase 9 setup results (Claude Code, 2026-09-16)
Phase 9 is seeded and GitHub is set up. Done on Román's Mac by Claude Code on
2026-09-16, each GitHub step with his explicit yes in the chat.

**Tracker.** `romanfq/matchwire-spec` (public, issues on) now holds the plan:
- epics: **BE #1**, **FE #2**, **E2E #3**;
- backend tasks: S1 #4, E1 #5, E1b #6, E2 #7, E3 #8, E4 #9, E5 #10, E6 #11,
  E4b #12;
- frontend tasks: S2 #13, E7 #14, E8 #15, E9 #16, E10 #17, E11 #18, E12 #19,
  E13 #20; end-to-end: E14 #21.

`backend seed poc/matchwire/plan.yaml` dry run showed 13 labels, 21 issues,
18 parents and 29 dependencies; `--apply --yes` created all of it and
`seed.verify` came back clean. Then `plan sync` imported 21 (ledger commit
`be0ef6d`; note `plan.sync` defaults to `push=True`, so that one pushed itself —
Román had already approved pushing this repo), and `task list` shows **GH-4 (S1) and GH-13 (S2) as the
only `(ready)` tasks** — the rest are blocked by their dependencies, as planned.

**Bot.** `romanfq-dagsbot`, id `329746898`. It has **write** on both code
repos, so no invitation is needed. `.swarm/local.yaml` (machine-local, never
committed) now has:
`bot: {login: romanfq-dagsbot, email: 329746898+romanfq-dagsbot@users.noreply.github.com}`,
confirmed through `Context().bot_identity()`. The token was never printed.

**gh flags (step 2): fine.** gh 2.101.0 lists `--parent number` and
`--add-blocked-by number` (plus `--remove-parent` / `--remove-blocked-by`), both
taking an issue number or URL — which is how `GitHubBackend.set_parent` and
`add_dependency` call them. No change needed. Note the bare numbers resolve
inside `--repo`, so this holds only while every plan issue lives in one repo.

**Code repos.**
- `matchwire-frontend` was completely empty (no branches at all), so worktrees
  had nothing to fork from. Pushed an initial README commit `c02321e` on `main`
  saying the real scaffold arrives with S2. `matchwire-backend` already had
  `main`.
- Branch protection is **applied** to both: `enforce_admins: true`,
  1 approving review, `dismiss_stale_reviews: true`, no required checks.
  Verified by reading `/branches/main/protection` back. Both repos are public,
  so this works on the Free plan. The bot is the PR author and Román approves,
  so the one-review rule is satisfiable.
- This repo was pushed to `github.com/romanfq/poc-swarm-ensemble` (Román's yes).

**Not done / worth a look:**
- The test suite was **not** re-run this session — Román asked to skip it. The
  last full run is the one recorded above (179 pass). `test_cli.py::
  test_backend_seed_dry_run_then_apply` has therefore never run here, though the
  real dry run and apply both behaved.
- `swarm.py protect` has no `--yes`, unlike `backend seed`, so it can't be
  driven non-interactively; I answered its prompt on stdin. Worth adding for
  parity.
- `task list` has no `--json`, so there's no machine-readable task list.

Then Cowork: set up two "machines" and run the plan §4 Phase 9 scenarios. S1
and S2 are the only ready work, so the first run exercises scaffolding before
anything else can start.
