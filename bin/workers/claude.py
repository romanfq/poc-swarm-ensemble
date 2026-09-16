"""claude — an interactive Claude Code session in a new terminal window,
inside the task's worktree, pointed at the injected skill (Ch.7.3)."""
from __future__ import annotations

import shlex
from pathlib import Path

from workers.base import ClaimedTask, MacWorker, applescript_string

PROMPT = ("You are the worker for DAGS task {short} ({title}). Read .swarm-task/README.md and "
          "follow it exactly: run `.swarm-task/swarm-task plan`, wait for the review gate, then "
          "`.swarm-task/swarm-task implement`, and finish with `.swarm-task/swarm-task done`. "
          "If you need a human decision, run `.swarm-task/swarm-task block \"<question>\"` and stop. "
          "Never run `gh pr merge`.")


class ClaudeWorker(MacWorker):
    name = "claude"
    label = "claude"
    human = False

    def shell_command(self, task: ClaimedTask, worktree: Path) -> str:
        args = [str(self.local.get("claude_bin", "claude")), *map(str, self.local.get("claude_args", []))]
        prompt = PROMPT.format(short=task.short, title=task.title)
        return f"cd {shlex.quote(str(worktree))} && {' '.join(shlex.quote(a) for a in args)} {shlex.quote(prompt)}"

    def commands(self, task: ClaimedTask, worktree: Path) -> list[list[str]]:
        cmd = applescript_string(self.shell_command(task, worktree))
        app = str(self.local.get("terminal_app", "Terminal"))
        if app.lower().startswith("iterm"):
            script = ['tell application "iTerm"',
                      "activate",
                      "set w to (create window with default profile)",
                      f"tell current session of w to write text {cmd}",
                      "end tell"]
        else:
            script = ['tell application "Terminal"', "activate", f"do script {cmd}", "end tell"]
        out = ["osascript"]
        for line in script:
            out += ["-e", line]
        return [out]
