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

- **self-approve** tasks: you may approve your own plan, so you can go straight on.
- **human-must-review** tasks: wait until a human approves the plan on the
  Swarm Board (or with `swarm.py task approve-plan`). Then run
  `.swarm-task/swarm-task wait --for approved`. It blocks cheaply and returns when
  the plan is approved, sent back (with the reviewer's note), or the task is paused
  or taken from you. Don't build a watcher of your own. If it times out (exit 3),
  run it again.

Your plan is posted as a comment on the task's issue. The reviewer answers your
**Risks / open questions** there, in that thread. `swarm-task status` and
`swarm-task implement` print what listed humans said after your latest plan or
`block` comment, under "Reviewer says". A comment never approves the plan: that
stays a Board / `approve-plan` action.

Nothing reaches the reviewer until `.swarm-task/swarm-task plan --submit` runs.
If the Board notices a draft plan in the worktree, it can submit and review it
for you.

Don't write any code before the plan is approved.

## 2. Implement: `.swarm-task/swarm-task implement`

This prints the approved plan, the checkpoint (what's been tried and what's
left) and any reviewer feedback. Feedback is tagged `fix:`, `explain:` or
`reject-approach:`. Work against the plan, not the raw ticket. Record progress
as you go, so anyone can resume if you stop:

    .swarm-task/swarm-task note --tried "cached the feed client" --remaining "parse scores" "tests"
    .swarm-task/swarm-task note --summary "Adds a 60s poller for the sports feed" --risk "rate limits"

If you need a decision from a human, ask and then **wait** for the answer. Don't guess:

    .swarm-task/swarm-task block "Should cancelled matches be stored?"
    .swarm-task/swarm-task wait --for answer

The question is posted on the issue too. If the human replies there, the reply
shows up under "Reviewer says" in `swarm-task status`.

Every `swarm-task` command starts by printing news about your task (`[swarm] ...`):
a human's answer, a plan sent back, a pause request, or a lost claim. If it says the
task is no longer yours, or asks you to pause, record your progress with `note` and stop.

## 3. Finish: `.swarm-task/swarm-task done`

Needs a `--summary` note first. This commits using the house template, brings the
branch up to date with the base (a rebase before the first push, a merge once a PR
is open; never a force-push), runs the repo's tests on the updated tree, pushes the
branch and opens the pull request. It never merges; a human does that.

If `done` fails (for example, tests are red), fix the problem and run it again.

If updating the branch conflicts, `done` leaves the merge in progress and blocks with
the files and the steps. Don't edit the tree: `.swarm-task/swarm-task wait --for answer`,
then run `done` again. It accepts the human's resolution only if the merge is committed,
no paths are unmerged and no conflict markers remain.
