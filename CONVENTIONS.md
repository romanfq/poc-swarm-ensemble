# Swarm conventions

Required reading for every worker, human or AI, before starting a task
(whitepaper Ch.9.3). Add a line here whenever a reviewer rejects an approach,
so the same mistake doesn't come back on a different task.

## Process

1. `swarm-task plan` — write `.swarm-task/plan.md`. Don't write code yet.
2. Get the plan reviewed (by yourself for `auto-pr` tasks, by a human otherwise).
3. `swarm-task implement` — work against the plan and record progress with `swarm-task note`.
4. `swarm-task done` — commits, pushes and opens the PR. Never run `gh pr merge`.

If you're blocked or need a human decision, run `swarm-task block "<question>"`
and stop. Don't guess.

## Code

- Keep each PR to the task's scope. Don't reformat unrelated files.
- Tests for new behaviour go in the same PR.
- Never commit `.swarm-task/` (it is git-excluded already).

## Rejected approaches

<!-- - YYYY-MM-DD TASK-KEY: what was rejected and why -->
