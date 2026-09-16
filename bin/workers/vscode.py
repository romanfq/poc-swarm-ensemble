"""VSCode + Human — ``code <worktree> <README>`` in a new window (Ch.7.3)."""
from __future__ import annotations

import shutil
from pathlib import Path

from workers.base import ClaimedTask, MacWorker


class VSCodeWorker(MacWorker):
    name = "vscode"
    label = "VSCode + Human"
    human = True

    def commands(self, task: ClaimedTask, worktree: Path) -> list[list[str]]:
        code = self.local.get("code_bin") or shutil.which("code")
        if code:
            return [[str(code), "-n", str(worktree), str(self.readme(worktree))]]
        return [["open", "-na", "Visual Studio Code.app", "--args", "-n", str(worktree), str(self.readme(worktree))]]
