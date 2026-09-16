# DAGS coordination repo — notes for Claude Code

This repo is the **coordination repository** of DAGS (Distributed AGent Swarm):
- the ledger (`tasks/`, `control/`, `priority/`, `quota/`);
- the committed protocol scripts (`bin/`).

Design sources, in order of authority:
1. The whitepaper, *DAGS — Distributed AGent Swarm*. Where its text and figures
   disagree, the text wins. Chapter numbers (Ch.x) in the code refer to it.
2. `docs/HANDOVER.md`: decisions made, build status, and the **Next** checklist.
3. The implementation plan (§x.y references in the code). Its key decisions are
   summarised in `docs/HANDOVER.md`.

## Rules

1. **Baton.** Read `BATON` before editing anything. If `holder` isn't
   `claude-code`, don't change files. Say so and stop. Two agents work on this
   repo (Claude in the Cowork app, and you) but never at the same time.
   - To hand back: update the **Next** section of `docs/HANDOVER.md`, set
     `holder: cowork` (or `roman`), and commit.
   - Commit locally at every hand-off. **Never push** unless Román asks.
2. **Don't touch the MatchWire repos** until the Phase 9 POC:
   `~/Documents/DAGS/matchwire/be/matchwire-backend` and
   `~/Documents/DAGS/matchwire/fe/matchwire-frontend`.
3. **Ask Román before changing anything on GitHub.** That includes branch
   protection, labels (`swarm.py backend init --apply`), repos, tokens and
   pushes. `swarm.py protect` and `backend init` are dry runs unless given `--apply`.
4. **macOS only** for worker launchers (plan §2.14).
5. **Worker PRs are opened by the bot account.** Its token is in the macOS
   Keychain, service `dags-worker-token` (plan §2.1). Never print or commit the token.
6. **Dependencies:** `bin/requirements.txt` stays at typer, rich, textual and
   pyyaml. Everything else is stdlib. `bin/skill/swarm-task` must stay
   **stdlib-only**.

## Running things

```bash
./bin/dev-setup.sh            # creates .swarm/venv (dev deps) and runs the whole suite
./bin/dev-setup.sh -k cli     # pytest args pass through
.swarm/venv/bin/python -m pytest -q tests
DAGS_NO_VENV=1 python3 bin/swarm.py --help    # skip the venv bootstrap
```

- Tests put `bin/` on `sys.path` (see `tests/conftest.py`).
- The `swarm` fixture builds a bare "GitHub" remote with machine clones.
- `fake_gh` swaps the gh runner.
- `tests/fakes.py` has a stateful fake GitHub.

## Layout

| Path | Contents |
|---|---|
| `bin/resolve.py` | Pure claim resolution, readiness and quota |
| `bin/dags/` | records, gitsync, gh, config, ledger (writes), plan (tracker → ledger), snapshot, feed, boardview (what the Board shows), scheduler, work (worker side), actions (human levers), repos, worktree, notify, daemon, panel, cli (typer), venv, prereqs |
| `bin/backends/` | Issue-backend port: `fake`, `github`. Jira is future work, see `FutureWork.md` |
| `bin/workers/` | Worker port: `claude`, `intellij`, `vscode` (macOS launchers) |
| `bin/skill/` | Injected into every worktree as `.swarm-task/` |
| `bin/poll.py` | Poller |
| `bin/board.py` | Swarm Board (Textual) |
| `bin/swarm.py` | Entry point |
