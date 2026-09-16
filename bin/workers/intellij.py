"""IntelliJ + Human — IntelliJ opens on the worktree with .swarm-task/README.md
as the first tab, then gets out of the way (Ch.7.3)."""
from __future__ import annotations

from pathlib import Path

from workers.base import ClaimedTask, MacWorker


class IntelliJWorker(MacWorker):
    name = "intellij"
    label = "IntelliJ + Human"
    human = True

    def commands(self, task: ClaimedTask, worktree: Path) -> list[list[str]]:
        app = str(self.local.get("intellij_app", "IntelliJ IDEA"))
        return [["open", "-na", f"{app}.app", "--args", str(worktree), str(self.readme(worktree))]]
