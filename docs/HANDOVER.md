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
- Code lives inside the coordination repo (plan §2.15).
- Workers open PRs with a separate GitHub bot (machine) account (plan §2.1).
  - The token is in the macOS Keychain, service `dags-worker-token`.
  - Pushes use the human's normal git credentials. Only the PR step
    (`gh pr create`/`gh pr comment` inside `swarm-task done`) uses the bot token,
    because the PR author is what matters for approvals.
- macOS only for worker launchers (plan §2.14).
- Every other open question uses the plan's proposed default:
  - §2.3 skew-corrected wall-clock leases;
  - §2.5 `quota/` records;
  - §2.6 `repo:` label;
  - §2.13 `.worktrees/` plus `.swarm/local.yaml` repo path mapping.
- **Phase 8 (Jira adapter) is deferred.** Román has no Jira instance. See
  `FutureWork.md`; `backend: jira` fails with a pointer there.
- **Two agents, one baton.**
  - Claude in Cowork writes code and the tests that run in its cloud sandbox.
  - Claude Code on the Mac runs anything that needs the real Mac: the venv,
    typer/rich/textual, `gh`, Keychain, `osascript`, and the Phase 9 POC.
  - The `BATON` file names the only agent allowed to edit. Hand-offs are local
    commits, never pushed. `CLAUDE.md` holds the rules for Claude Code.
- GitHub reads use one paginated `gh api graphql` query (parent, subIssues,
  blockedBy, issueType). This avoids depending on which `--json` fields a given
  gh release supports. Writes use `gh issue edit/close/comment`.
- **Plan review gate (§2.8).**
  - The plan is stored in `checkpoint.yaml` (`plan_md`, `plan_sha`).
  - Human approvals are append-only records in `tasks/…/plan-reviews/`, so any
    human on any machine can approve without writing the single-writer checkpoint.
  - `auto-pr` tasks self-approve.
- **The skill stays stdlib-only.** `.swarm-task/swarm-task` calls
  `swarm.py task …` (context, note, submit-plan, block, done) using the venv
  Python recorded in `.swarm-task/context.json`.
- Task keys:
  - GitHub tasks are keyed `OWNER/REPO#N`, with the short name `GH-N`.
  - Ledger dirs are `tasks/<EPIC-short>/<TASK-short>`; epics use `tasks/<EPIC>/_epic`.
  - Tasks without an epic go under `tasks/_no-epic/`.
- Still unknown: whether the MatchWire repos are public or private (affects
  branch protection on a free plan).

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

Tests: **109 pass, 1 module skipped** (`tests/test_cli.py`, which needs typer/rich)
in the cloud sandbox. Run the full suite with `./bin/dev-setup.sh` from the repo root.

| Phase | Status |
|---|---|
| 0 Scaffolding | done |
| 1 Deterministic core | done and tested |
| 2 Git sync + gh wrapper | done and tested |
| 3 Issue backend port | done and tested (Jira deferred) |
| 4 `swarm.py` bootstrap | written; stdlib parts tested; typer CLI tests written but not yet run |
| 5 Scheduler, workers, skill | written, **not tested yet** |
| 6 Poller + notifications | written, **not tested yet** |
| 7 Swarm Board | **not started** |
| 8 Jira adapter | deferred (`FutureWork.md`) |
| 9 POC on MatchWire | not started |

### Phase details

- **1. Deterministic core.** `bin/resolve.py`, `dags/records.py`, `dags/timeutil.py`.
  - `resolve.lookup()` finds a task by full key, short key or dir name.
  - `resolve.plan_status()` implements the review gate.
- **2. Git sync + gh wrapper.** `dags/gitsync.py`, `dags/gh.py`.
  - Tests: `tests/test_gitsync.py` races four clones of a bare remote (zero
    conflicts, identical `resolve()` everywhere), covers the add/add meta-import
    race and the single-writer heartbeat conflict (remote wins), plus the
    control/quota records. `tests/test_gh.py`.
- **3. Issue backend port.**
  - `backends/base.py` (the port, plus `all_tasks` and the shared `ready_from` rule).
  - `backends/github.py` (GraphQL reads, label writes, `init_commands`).
  - `backends/fake.py`.
  - `dags/plan.py` (plan sync): import, meta revisions, imported-done,
    replanned-after-reject, stale status reset, autonomy downgrade after
    `max_retries` failures.
  - Tests: `tests/test_backend_contract.py` runs one suite against fake, github
    and github-with-issue-types. `tests/test_github_backend.py`, `tests/test_plan_sync.py`.
- **4. `swarm.py` bootstrap.**
  - `bin/swarm.py`.
  - `dags/venv.py`: self-install and re-exec; set `DAGS_NO_VENV=1` to skip.
  - `dags/prereqs.py`, `dags/repos.py` (clone or map, `commit.template`, `info/exclude`).
  - `dags/daemon.py`: detached daemon, pidfile, `.swarm/swarm.log`,
    `.swarm/daemon.json`; stop means a control record plus SIGTERM.
  - `dags/panel.py`: rich status panel.
  - `dags/cli.py`: typer app. Commands: `start`, `stop`, `pause`, `resume`,
    `throttle`, `status`, `board`, `protect`, `plan sync`,
    `backend get-task|ready|set-status|init`, `quota set|show`,
    `epic takeover|release`, and `task list|show|worker|freeze|unfreeze|reassign|approve-plan|merge|open|release|still-working`,
    plus the skill-facing `task context|note|submit-plan|block|done`.
  - Tests: `tests/test_bootstrap.py` (stdlib parts), `tests/test_cli.py`
    (typer/rich, skipped in the cloud).
- **5. Scheduler, workers, skill.**
  - `dags/scheduler.py`:
    - Scheduler cycle: plan sync, control records, withdraw lost claims,
      resume won ones, claim within quota, re-resolve, prepare, then dispatch
      or announce the worker prompt.
    - Heartbeater: batched, ownership re-checked after pull, idle limit per
      §2.9, applied to every worker type.
    - Loop thread.
  - `dags/worktree.py`: `.worktrees/<TASK>` on `swarm/<TASK>`, reusing a local
    or remote branch; `.swarm-task/` injection with `context.json`; strip and remove.
  - `dags/work.py`: `choose_worker`, `note`, `submit_plan`/`approve_plan`,
    `feedback`, `block`, `still_working`, `release`, and `finish`:
    test command, template render, commit (bot author if set), push, PR
    create/comment with the bot token, completion, backend status and comment.
  - `dags/actions.py`: pause/resume/throttle/quota, freeze/unfreeze/reassign,
    takeover, merge (gh approve + squash, then a done record), links, and the
    protection body.
  - `bin/workers/`: `claude` (osascript → Terminal/iTerm), `intellij` (`open -na`),
    `vscode` (`code -n`). Registry order a/b/c matches Ch.10.7.
  - `bin/skill/README.md`, `bin/skill/swarm-task`: plan [--submit] / status /
    implement / note / block / done.
  - `dags/notify.py`: log file, macOS notification, webhook, and in-process listeners.
- **6. Poller + notifications.** `bin/poll.py`:
  - feed events (quiet on first run);
  - awaiting-review and stale-heartbeat diffs;
  - live conflicts and thrash escalation;
  - PR tracking: merged → done, closed → rejected (backend set to blocked),
    new CHANGES_REQUESTED review → reopened (backend set to ready);
  - overlap detection;
  - output-contract watcher: strips `.swarm-task/` before announcing
    "<worker> has finished X. The PR can be found at …", and removes the
    worktree after merge or close.
  - `dags/feed.py` gives the plain-English feed (Ch.10.3); `dags/snapshot.py`
    gives the shared read model.
- **Also added:** `CLAUDE.md`, `BATON`, `FutureWork.md`, `bin/dev-setup.sh`,
  `pyproject.toml` (pytest/ruff config).

## Next (holder: cowork)
1. Tests for Phase 5, all in the cloud:
   - scheduler with the fake backend and fake launcher on bare-repo clones;
   - worktree creation/reuse against a local "code" remote;
   - the skill run end to end with a fake gh;
   - `work.finish`;
   - idle limit.
2. Tests for Phase 6: the poller against a fake gh (merged, closed, changes
   requested, overlap, contract cleanup, thrash).
3. Phase 7: `bin/board.py` (Textual). Put the logic in `dags/snapshot.py` and
   `dags/actions.py`; write pilot tests with `App.run_test()` inside
   `asyncio.run()`, skipped without textual.
4. Hand the baton to Claude Code with this checklist:
   - run `./bin/dev-setup.sh`;
   - fix failures in `tests/test_cli.py` and the Board tests;
   - smoke-test `swarm.py --help`, `status` and `board` on the Mac;
   - check `security find-generic-password -s dags-worker-token` works (without printing it);
   - touch nothing on GitHub.
5. Phase 9 POC. Needs Román's go-ahead for:
   - the plan repo;
   - labels (`backend init --apply`);
   - branch protection (`protect --apply`);
   - the bot's collaborator access.
