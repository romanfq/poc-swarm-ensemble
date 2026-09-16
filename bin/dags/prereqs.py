"""Local prerequisite checks (whitepaper Ch.5.3 step 1): fail fast, no silent
partial setup."""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from dags import gh
from dags.venv import MIN_PYTHON


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


def run(need_gh_auth: bool = True, which=shutil.which, platform: str | None = None) -> list[Check]:
    platform = platform or sys.platform
    out = [Check("python", sys.version_info[:2] >= MIN_PYTHON,
                 f"{sys.version_info[0]}.{sys.version_info[1]} (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)")]
    out.append(Check("git", which("git") is not None, "install with `xcode-select --install` or Homebrew"))
    has_gh = which("gh") is not None
    out.append(Check("gh", has_gh, "install with `brew install gh`"))
    if has_gh:
        if need_gh_auth:
            out.append(Check("gh auth", gh.auth_ok(), "run `gh auth login`"))
        missing = gh.missing_features()
        out.append(Check("gh features", not missing,
                         ("upgrade gh (`brew upgrade gh`); missing: " + ", ".join(missing)) if missing else
                         f"gh {gh.version() or '?'}"))
    out.append(Check("macOS", platform == "darwin",
                     "worker launchers are macOS-only (plan §2.14); scheduling still works", fatal=False))
    return out


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.fatal]
