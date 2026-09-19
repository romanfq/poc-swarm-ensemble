# DAGS — implementation handover

Read this together with the whitepaper and the spec v1.1 (`docs/SPEC.md` here,
`claude/DAGS-spec.md` and the PDF in the claude.ai project). Where the
whitepaper's text and figures disagree, the text wins. "Plan §x" below refers
to `claude/DAGS-implementation-plan.md` in the same project.

This file holds **decisions made**, **build status**, and the **Next**
checklist for whoever holds the baton. It is not a changelog — what happened
and when is in `git log`, in `BATON`'s `history:`, and in the issue tracker
(`romanfq/poc-swarm-ensemble`, pinned issue **#7**). Rewritten from scratch on
2026-09-19 because the dated, append-only sections it used to carry had grown
to 725 lines and gone stale in places (one line here once claimed something a
later section already contradicted). Keep it this way: update state in place,
don't append another dated section.

## Two swarms exist, on two different things

This is easy to get backwards, so it goes at the top.

| | **MatchWire swarm** | **Meta (dogfooding) swarm** |
|---|---|---|
| Coordination repo | `~/Documents/DAGS/swarm/poc-swarm-ensemble` (this repo) | `~/Documents/DAGS/swarm/dags-meta` |
| Plan repo | `romanfq/matchwire-spec` | `romanfq/poc-swarm-ensemble` (this repo's own issues) |
| Code repo(s) | `romanfq/matchwire-backend`, `romanfq/matchwire-frontend` | `romanfq/poc-swarm-ensemble`, a **second** checkout at `~/Documents/DAGS/code/poc-swarm-ensemble` (so it never shares worktrees with this clone — the collision `#13` describes) |
| What it works on | the MatchWire app (plan §4 POC) | DAGS' own `next-version` backlog, issues `#1`–`#37`+ in this repo |
| Machines | `macbookpro-68b8` (this clone), `teammate-b` | `dags-a` (`dags-meta`) |
| Right now | both machines **stopped** | **stopped** too, holding 2 unreleased claims (`GH-16` in-progress, `GH-21` claimed, no worker chosen) — check live: `cd ~/Documents/DAGS/swarm/dags-meta && ./bin/swarm.py status` |

The meta swarm exists because Román is dogfooding DAGS on itself: its plan is
this repo's own issue tracker, and its workers open PRs against this repo's
`bin/`, `tests/` and `docs/` — the same code the MatchWire swarm runs.

Other locations: MatchWire backend `~/Documents/DAGS/matchwire/be/matchwire-backend`,
frontend `~/Documents/DAGS/matchwire/fe/matchwire-frontend`.

## Decisions made

**Settled by Román**
- **D1: code location.** The code lives inside the coordination repo, in `bin/` (plan §2.15).
- **D2: bot account for worker PRs** (plan §2.1). Login `romanfq-dagsbot`,
  token in the macOS Keychain (`dags-worker-token`). Alternatives:
  `DAGS_WORKER_GH_TOKEN`, or `worker_token: none` (PRs under your own account).
  - Only the PR step (`gh pr create`/`gh pr comment` in `swarm-task done`) uses
    the bot token; pushes use the human's own git credentials.
  - Worker commits are authored as the bot when `.swarm/local.yaml` has `bot:`.
  - Humans approve and merge with their own `gh auth`.
  - **D2b, added 2026-09-19: this applies to Claude Code's own direct fixes
    too, not just swarm workers.** When Claude Code opens a PR against this
    repo outside a claimed task (a manual fix, like the CI workflow or the
    ruff line), open it with the bot's token so the PR author is
    `romanfq-dagsbot`, not `romanfq` — then Román can review and merge it
    normally. Opening it as `romanfq` makes Román the author, and GitHub
    refuses self-approval outright, forcing an admin bypass every time. See
    "Branch protection and CI" below for why that now matters.
- **D3: macOS only** for worker launchers (plan §2.14): Terminal/iTerm via
  `osascript`, `open -na "IntelliJ IDEA.app"`, `code -n`.
- **D4: Phase 8 (Jira adapter) deferred.** No Jira instance (`FutureWork.md`).
  `backend: jira` fails with a pointer there.
- **D5: two agents, one baton.** Cowork writes code; Claude Code runs what
  needs the real Mac (venv, `gh`, Keychain, `osascript`) and anything touching
  GitHub. `BATON` names the only agent allowed to edit this repo; hand-offs
  are local commits, never pushed by the hand-off itself. `CLAUDE.md` holds
  Claude Code's rules. Why: Cowork's Mac shell is an isolated, network-less
  Linux VM, and its cloud sandbox can't reach these GitHub repos either — only
  Claude Code on the Mac can run `gh`, so GitHub work funnels there.
- **D26, added 2026-09-19: it's a POC — self-approval is acceptable.** Román
  asked, explicitly, for the option to merge his own PRs without a second
  reviewer. Kept anyway: D2b's bot-authorship pattern, because it turned out
  to solve the friction *without* removing the review gate — so it's used in
  practice, but nothing stops falling back to an admin bypass if D2b is ever
  inconvenient. See "Branch protection and CI".

**Plan defaults adopted** (no objection raised)
- **D6: leases (§2.3).** Wall-clock, corrected for skew against GitHub's
  `Date` header. Lease 15 min, heartbeat every 3 min (`backend.yaml` → `swarm:`).
- **D7: single-writer files (§2.4).** `heartbeats/<machine>.yaml` and
  `checkpoint.yaml` are written only by the current winner. Retry count is
  computed, never stored. `meta.yaml` never changes; tracker changes land as
  `meta/` revisions.
- **D8: daemon and Board (§2.2).** `start` spawns a detached daemon
  (scheduler, heartbeat, poller), `.swarm/daemon.pid`/`.json`/`swarm.log`. The
  Board (`swarm.py board` or `start --attach`) runs its own poller when no
  daemon is up. `stop` writes a shared stop record, then sends SIGTERM.
- **D9: global quota (§2.5).** `quota/<human>-<clock>.yaml`, latest clock
  wins, only `humans.yaml` names count. Default is `swarm.default_quota`.
- **D10: target repo (§2.6).** `repo:OWNER/NAME` label → `epic_repos:` →
  `default_repo:`. Epics have no repo; a task without one is never claimed.
- **D11: plan sync and Done (§2.7).** Every cycle mirrors the tracker into
  `tasks/`. Done = the PR was merged (poller or Board's Approve & merge), or
  the tracker issue was closed by hand.
- **D12: autonomy tiers (§2.8).** `auto-pr` self-approves its plan;
  `human-must-review` needs a human; `human-must-scope` never goes to an AI
  worker. After `max_retries` failed claims the tier drops one step — except
  it can't drop past `human-must-scope`, which is a live gap (`#12`).
- **D13: idle limit (§2.9).** After `human_idle_hours` with no progress, "still
  working?"; one more lease with no answer and heartbeats stop. Applies to
  every worker, human or AI.
- **D14: tests instead of CI, extended 2026-09-19.** `done` still runs the
  repo's `test_command` and refuses to open a PR if it fails; that part is
  unchanged. New: a GitHub Actions build also gates the merge itself — see
  below.
- **D15: command corrections (§2.11).** `gh issue edit --add-blocked-by`.
  `swarm.py protect` prints the full JSON body, dry run unless `--apply`.
  `board --web` installs textual-dev and serves on port 4590.
- **D16: arbitration trust (§2.12), partly implemented.** Records from names
  not in `humans.yaml` are ignored. The commit-author-email check is **not
  built** (`K4`, still open).
- **D17: locations (§2.13).** `.worktrees/<TASK>` on branch `swarm/<TASK>`,
  git-ignored. Code repos: `.swarm/local.yaml` → `repos:`, else
  `.swarm/repos/OWNER/NAME`. **Caution, found 2026-09-17:** two machines
  mapping the *same* code checkout collide — git allows a branch in exactly
  one worktree (`#13`). The meta swarm avoids this with a dedicated second
  checkout; the MatchWire machines currently don't need to, since only one is
  ever running at a time.
- **D18: GitHub reads** via one paginated `gh api graphql` query; writes via
  `gh issue edit/close/comment`.
- **D19: task keys.** `OWNER/REPO#N`, short name `GH-N`. Ledger dirs
  `tasks/<EPIC-short>/<TASK-short>`. Every command accepts full key, short
  key or dir name. **Caution:** `GH-N` is only unique within one plan repo —
  it collides across swarms or plan repos (`#16`, unfixed).
- **D20: plan review gate.** `plan_md`/`plan_sha` in `checkpoint.yaml`;
  approvals are append-only `plan-reviews/` records.
- **D21: the skill stays stdlib-only.** `.swarm-task/swarm-task` delegates to
  `swarm.py task …` via the venv Python in `.swarm-task/context.json`.
- **D22: outcome records** in `completions/`: `pr-opened`, `done`, `reopened`,
  `rejected`, `replanned`. New `CHANGES_REQUESTED` → `reopened`, task resumes
  from checkpoint on any machine. PR closed unmerged → `rejected`, cleared by
  a human setting `swarm:status:ready` (records `replanned`).
- **D23: venv.** `.swarm/venv` created and re-exec'd into on first run,
  stamped by platform/Python, rebuilt if foreign. `typer>=0.16`; typer ≥0.17
  vendors its own click, `dags/cli.py` takes whichever is present.
- **D24: notifications** always go to `.swarm/notifications.log`; `notify:`
  in `.swarm/local.yaml` adds desktop notifications and a webhook.
- **D25: plan seeding.** `swarm.py backend seed FILE [--apply] [--yes]`
  (`bin/dags/seed.py`). Idempotent via a hidden `<!-- dags-seed: ID -->`
  marker; never rewrites an existing issue.

**Known gaps**
- **K1–K3: fixed** (worktree cleanup after merge/reject; quota-lowered tasks
  pause then release; `--identity` persistence).
- **K4: open.** D16's commit-author-email check isn't built.
- The **`next-version` backlog is the live list of everything else** —
  37+ issues found running both swarms, from missing Board affordances to
  real correctness bugs, all labelled `next-version` and checklisted on the
  pinned **`romanfq/poc-swarm-ensemble#7`**. That issue is the source of
  truth; this file doesn't duplicate its contents because it will only go stale.

## Branch protection and CI (added 2026-09-19)

- **`.github/workflows/tests.yml`** runs the suite on every `pull_request`
  (against the merge result, `refs/pull/N/merge`) and on push to `main`.
  `ubuntu-latest`, installs `bin/requirements-dev.txt`, sets a throwaway git
  identity (a fresh runner has none, unlike a dev Mac), runs
  `python -m pytest -q tests`. **259 passed, 1 skipped, ~100s.** No `ruff`
  step yet — the one-item backlog (`B905` in `bin/poll.py`) was fixed
  separately (PR `#37`) but the step was never re-added to the workflow.
- **Why it exists:** `9c69647` ("Merge branch 'main' into swarm/GH-10")
  dropped 11 lines of `tests/test_board.py` in a conflict resolution and left
  `main` uncollectable for a day. Nothing had run the *merge result* before.
- **Enforcement is a GitHub *ruleset*** named "main" (id `23706858`), **not**
  classic branch protection (that was deleted and replaced by this). Current
  rules: no deletion, no force-push, `tests` must pass, 1 approving review,
  `bypass_actors: []` — nobody, including the repo owner, can bypass it right
  now. Self-approval is impossible by GitHub's own rule regardless of this
  config, which is what made D2b/D26 the actual answer, not a config toggle.
- **What this means for landing anything on `main`:** open the PR as the bot
  (D2b above), then Román reviews and merges as himself. Worked out live on
  `#32` (classic protection, `enforce_admins` toggled twice — noisier, and
  each toggle briefly disabled *every* rule, not just review) and `#37` (the
  ruleset; closed and reopened authored by the bot instead of touching the
  ruleset at all — cleaner, keep using this one).
- **Still unresolved, not yet acted on:** the MatchWire daemon's `gitsync.push()`
  (`bin/dags/gitsync.py:136`) pushes ledger commits **directly** to `main`,
  no PR. That will fail the moment either MatchWire machine restarts, because
  this ruleset now covers `main` and direct pushes aren't exempted. Both
  machines are stopped, so nothing is broken *yet*. Fixing it means either
  moving the MatchWire ledger to an unprotected branch/repo, or excluding
  direct pushes for the ledger somehow — not designed yet.

## Build status

| Phase | Status |
|---|---|
| 0–7 (core, Board) | done, tested |
| 8 Jira adapter | deferred (`FutureWork.md`) |
| 9 POC on MatchWire | in progress — see below |
| — Meta/dogfooding swarm | in progress — see below, not in the original phase numbering |

Tests: 259 passed, 1 skipped (Linux, CI, ~100s) — the number that gates
merges now. Locally on the Mac: same pass/skip count, 1049s serial,
226.91s with `pytest-xdist -n auto` (measured, not yet adopted anywhere).
Per-module test coverage isn't reproduced here; it's stable and discoverable
from `tests/` itself.

### MatchWire POC
Plan repo `romanfq/matchwire-spec`: 3 epics (BE, FE, E2E), 18 tasks from
`MatchWire-Spec.md` §7, seeded and dependency-ordered. `backend.yaml` names
the real repos; both are public branch-protected (1 review), so worker PRs
need a human approval same as here.
- **`GH-4` (S1, backend scaffold): merged.**
- **`GH-13` (S2, frontend scaffold): stuck, now parked.** It thrashed —
  claimed and re-claimed with no worker chosen, ~9 times, while the Mac slept
  between checks (`#12` documents why and proposes a fix). Set to
  `swarm:status:blocked` on 2026-09-19 so it won't be re-claimed; the scaffold
  itself still isn't done. Set back to `ready` when someone's actually going
  to work it.
- Both machines stopped; see the branch-protection note above before
  restarting either.

### Meta (dogfooding) swarm
`dags-meta`, plan = this repo's own issues, labelled and ordered via
`tools/label-issues.sh` (`OPTIN=18`, three small `EXTRA=` fixes). The opt-in
task (`#18`, "only issues carrying a swarm label belong to the swarm") is
**merged**, via a worker session in `~/Documents/DAGS/code/poc-swarm-ensemble`
and PR `#22`. Now stopped, holding two unreleased claims (`GH-16`
in-progress, worker vscode; `GH-21` claimed, no worker chosen) — they'll
lapse on their own or need `task release`. Check `./bin/swarm.py status`
from `dags-meta` before restarting.

## Next (holder: claude-code)

1. **Both swarms are stopped; nothing is claiming anything right now.**
   `dags-meta` still holds two claims from before it stopped (`GH-16`,
   `GH-21`) — decide whether to release them or let them lapse before
   restarting it.
2. **`GH-13` is parked** (`swarm:status:blocked`, done 2026-09-19) — the
   scaffold still needs doing, just not by accident on restart.
3. **The MatchWire-ledger-vs-ruleset conflict** (Branch protection section
   above) needs a real decision before either MatchWire machine restarts.
   Not designed yet — surface it to Román rather than guessing.
4. **If restoring the ruff step to CI**, pin the version
   (`pipx run ruff==<version> check bin tests`) so a future ruff release can't
   turn `main` red on its own.
5. **Filing more `next-version` issues:** same recipe as `#1`–`#37` —
   `gh issue create --repo romanfq/poc-swarm-ensemble --label next-version
   --title ... --body-file ...`, add a checklist line to `#7`. Check for
   autolink collisions (bare `#<n>` links into *this* repo; backtick a swarm
   short key like `` `GH-4` `` when it could be misread that way) before
   filing.
6. **Hand back:** update this section and `BATON`'s `history:`, commit
   locally, push (both daemons are stopped, so nothing pushes this for you).
