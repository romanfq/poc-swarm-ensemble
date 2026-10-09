# Future work

## Jira adapter: built (GH-123)

`backend: jira` ships (`bin/backends/jira.py`, client in `bin/dags/jira_client.py`, ADF in `bin/dags/adf.py`);
see SPEC §3.5 and D4. What is left is manual: the live smoke test (`tests/live_jira_smoke.py`) and the
mirror acceptance run, plus Jira Data Center, webhooks and OAuth 3LO, which are out of scope.

## Nudging a worker session (GH-2)

**Status:** not built. `swarm-task wait` and the `[swarm]` news printed by every
`swarm-task` command (GH-2) are the supported way for a worker to learn what
happened to its task.

Not done, on purpose:

- **Typing into the worker's terminal with `osascript`.** It can land in the middle
  of a prompt or a running tool call, and needs Accessibility permission.
- **Relaunching the worker when a waiting task is approved.** It would start a second
  session beside the first if the original is still open.

Revisit if workers keep ignoring `wait`.
