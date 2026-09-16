# You are the worker for this task

This folder (`.swarm-task/`) was put here by the swarm. Git ignores it, and it is
removed automatically once your PR is open. Everything you need is here:

| File | What it is |
|---|---|
| `spec.md` | The ticket, downloaded when the task was handed to you |
| `conventions.md` | House rules. Read them before you start |
| `context.json` | Task, claim, repo and branch details (for the tool below) |
| `swarm-task` | The three commands below |

You are on branch **`swarm/<task>`** in a worktree of its own. Run the commands
from the worktree root.

## 1. Plan: `.swarm-task/swarm-task plan`

This creates `.swarm-task/plan.md` from the spec and, if the task was started
before, from what was already tried. Edit `plan.md` until it says what you'll
change and how you'll test it. Then submit it:

    .swarm-task/swarm-task plan --submit

- **auto-pr** tasks: you may approve your own plan, so you can go straight on.
- **human-must-review** tasks: wait until a human approves the plan on the
  Swarm Board (or with `swarm.py task approve-plan`).
  `.swarm-task/swarm-task status` tells you when it's approved.

Don't write any code before the plan is approved.

## 2. Implement: `.swarm-task/swarm-task implement`

This prints the approved plan, the checkpoint (what's been tried and what's
left) and any reviewer feedback. Feedback is tagged `fix:`, `explain:` or
`reject-approach:`. Work against the plan, not the raw ticket. Record progress
as you go, so anyone can resume if you stop:

    .swarm-task/swarm-task note --tried "cached the feed client" --remaining "parse scores" "tests"
    .swarm-task/swarm-task note --summary "Adds a 60s poller for the sports feed" --risk "rate limits"

If you need a decision from a human, ask and then **stop**. Don't guess:

    .swarm-task/swarm-task block "Should cancelled matches be stored?"

## 3. Finish: `.swarm-task/swarm-task done`

Needs a `--summary` note first. This runs the repo's tests, commits using the
house template, pushes the branch and opens the pull request. It never merges;
a human does that.

If `done` fails (for example, tests are red), fix the problem and run it again.
