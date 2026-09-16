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

Tests: **179 pass, nothing skipped**, on Román's Mac (2026-09-16, Claude Code),
in 11m54s with `./bin/dev-setup.sh`. That includes the three things that had
never run anywhere before — `tests/test_cli.py`, `tests/test_board.py` and the
end-to-end test in `tests/test_skill.py`. The suite is slow by design: the
scheduler heartbeat/idle-limit, poller and gitsync tests wait on real clocks
and real git.

In the Cowork cloud sandbox the same suite is 164 pass, 3 modules skipped
(no typer/rich/textual there).

`ruff check` (F, E9, B) is clean as of the last Cowork run; ruff isn't
installed on the Mac and isn't in `bin/requirements-dev.txt`, so Claude Code
did not re-run it.

| Phase | Status |
|---|---|
| 0 Scaffolding | done |
| 1 Deterministic core | done, tested |
| 2 Git sync + gh wrapper | done, tested |
| 3 Issue backend port | done, tested (Jira deferred) |
| 4 `swarm.py` bootstrap | done, tested (CLI tests run on the Mac) |
| 5 Scheduler, workers, skill | done, tested (skill end-to-end run on the Mac) |
| 6 Poller + notifications | done, tested |
| 7 Swarm Board | done, tested (Textual pilot run on the Mac) |
| 8 Jira adapter | deferred (`FutureWork.md`) |
| 9 POC on MatchWire | not started |

The typer/rich/textual code was written without being able to run it, then
reviewed by a second agent against the libraries' APIs. Fixes from that review:
- `BoardApp.run_action` renamed to `run_job`, because the old name hid
  Textual's own dispatcher;
- `open_url` renamed so it no longer hides Textual's own method;
- a lock stops Board refreshes from overlapping;
- markup escaping in labels, table cells, notifications and CLI errors;
- `guarded` no longer swallows click's Abort/Exit;
- `typer>=0.16` (older typer breaks with click 8.2+);
- worker letters on the Board are the fixed a/b/c from Ch.10.7;
- `--json` output goes through `typer.echo`.

Running it on the Mac caught two more that no review had:
- **typer >= 0.17 doesn't depend on click**, it vendors it as `typer._click`.
  `dags/cli.py` now takes the vendored module when it's there and the real
  package otherwise, and builds `CONTROL_FLOW` from whichever exception classes
  exist — in current typer, `Exit` and `Abort` live only on `typer` itself.
  Before the fix `swarm.py` wouldn't start on a fresh venv at all.
- **`PlanScreen.task` hid Textual's read-only `MessagePump.task`**, so the
  plan-review modal (`v`) died with `AttributeError: property 'task' ... has no
  setter`. Renamed to `task_key`. Third name collision of the same kind: when
  adding an attribute to a Textual class, check it isn't already a property.

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

## Next (holder: cowork)
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

Then, for Cowork: the Phase 9 POC. It needs Román's go-ahead and details for:
- the plan repo name, and replacing `OWNER/…` in `backend.yaml`;
- `humans.yaml` (his GitHub login);
- labels (`swarm.py backend init --apply`);
- branch protection (`swarm.py protect <repo> --apply`); on the Free plan this
  depends on whether the MatchWire repos are public;
- the bot account's collaborator access and `.swarm/local.yaml`
  (repo paths, `bot:` login and email).
