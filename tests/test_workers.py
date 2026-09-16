from pathlib import Path

import pytest

import workers
from workers.base import ClaimedTask, applescript_string

TASK = ClaimedTask(key="org/plan#3", short="GH-3", title='Say "hi"', claim_id="mac-a-9",
                   autonomy="auto-pr", repo="OWNER/app", branch="swarm/GH-3")
WT = Path("/Users/roman/DAGS/.worktrees/GH 3")


def test_registry_and_prompt():
    assert list(workers.WORKERS) == ["claude", "intellij", "vscode"]
    assert workers.resolve_name("B") == "intellij"
    assert workers.resolve_name("VSCode + Human") == "vscode"
    with pytest.raises(workers.WorkerError):
        workers.resolve_name("emacs")
    assert workers.allowed_for("human-must-scope") == ["intellij", "vscode"]
    assert workers.allowed_for("auto-pr") == ["claude", "intellij", "vscode"]
    assert isinstance(workers.get("a"), workers.Worker)


def test_applescript_escaping():
    assert applescript_string('a "b" \\c') == '"a \\"b\\" \\\\c"'


def test_claude_terminal_and_iterm():
    launched = []
    w = workers.get("claude", {"claude_args": ["--permission-mode", "acceptEdits"]}, launched.append, "darwin")
    w.dispatch(TASK, WT)
    cmd = launched[0]
    assert cmd[0] == "osascript"
    body = "\n".join(cmd[2::2])
    assert body.startswith('tell application "Terminal"')
    shell = w.shell_command(TASK, WT)
    assert shell.startswith("cd '/Users/roman/DAGS/.worktrees/GH 3' && claude --permission-mode acceptEdits ")
    assert "GH-3" in shell and ".swarm-task/README.md" in shell
    iterm = workers.get("claude", {"terminal_app": "iTerm2"}, launched.append, "darwin")
    iterm.dispatch(TASK, WT)
    assert 'tell application "iTerm"' in launched[1]


def test_ide_launchers(monkeypatch):
    launched = []
    workers.get("intellij", {"intellij_app": "IntelliJ IDEA CE"}, launched.append, "darwin").dispatch(TASK, WT)
    assert launched[0] == ["open", "-na", "IntelliJ IDEA CE.app", "--args", str(WT),
                           str(WT / ".swarm-task" / "README.md")]
    monkeypatch.setattr("shutil.which", lambda name: None)
    workers.get("vscode", {}, launched.append, "darwin").dispatch(TASK, WT)
    assert launched[1][:3] == ["open", "-na", "Visual Studio Code.app"]


def test_non_macos_refuses():
    with pytest.raises(workers.WorkerError, match="macOS-only"):
        workers.get("vscode", {}, lambda c: None, "linux").dispatch(TASK, WT)
