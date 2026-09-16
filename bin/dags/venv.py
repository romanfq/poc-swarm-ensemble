"""Self-installing virtual environment (whitepaper Ch.5.2, Ch.5.3 step 1).

stdlib only: this runs before any third-party package is importable.

``ensure()`` makes sure ``.swarm/venv`` exists, matches this machine and the
current ``bin/requirements.txt``, and then re-executes the calling script with
the venv's interpreter. A stamp file records what the venv was built for, so a
venv created on another OS or Python (for example inside a Linux VM that
mounts this folder) is rebuilt instead of half-working.

Set ``DAGS_NO_VENV=1`` to skip all of this (tests, or a hand-managed env).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
STAMP = ".dags-stamp.json"


class VenvError(RuntimeError):
    pass


def check_python(version=None) -> None:
    version = version or sys.version_info
    if tuple(version[:2]) < MIN_PYTHON:
        raise VenvError(f"DAGS needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
                        f"(this is {version[0]}.{version[1]}). On macOS: `brew install python@3.12`.")


def venv_dir(root: Path) -> Path:
    return Path(root) / ".swarm" / "venv"


def venv_python(root: Path) -> Path:
    return venv_dir(root) / "bin" / "python"


def requirement_files(root: Path, dev: bool) -> list[Path]:
    files = [Path(root) / "bin" / "requirements.txt"]
    if dev:
        files.append(Path(root) / "bin" / "requirements-dev.txt")
    return files


def wanted_stamp(root: Path, dev: bool) -> dict:
    h = hashlib.sha256()
    for f in requirement_files(root, dev):
        h.update(f.read_bytes())
    return {
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "requirements": h.hexdigest(),
        "dev": dev,
    }


def read_stamp(root: Path) -> dict:
    try:
        return json.loads((venv_dir(root) / STAMP).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def in_venv(root: Path) -> bool:
    try:
        return Path(sys.prefix).resolve() == venv_dir(root).resolve()
    except OSError:
        return False


def plan_action(root: Path, dev: bool) -> str:
    """'ok' | 'install' (same interpreter, requirements changed) | 'rebuild'."""
    have = read_stamp(root)
    # a dev venv (bin/dev-setup.sh) also satisfies a normal run: compare like with like
    want = wanted_stamp(root, dev or bool(have.get("dev")))
    if not venv_python(root).exists() or not have:
        return "rebuild"
    for key in ("platform", "machine", "python"):
        if have.get(key) != want[key]:
            return "rebuild"
    if have.get("requirements") != want["requirements"] or (dev and not have.get("dev")):
        return "install"
    return "ok"


def _run(cmd, quiet: bool) -> None:
    r = subprocess.run(cmd, capture_output=quiet, text=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[-2000:] if quiet else ""
        raise VenvError(f"command failed: {' '.join(map(str, cmd))}\n{detail}\n"
                        "Manual fallback (Ch.5.2):\n"
                        "  python3 -m venv .swarm/venv\n"
                        "  .swarm/venv/bin/pip install -r bin/requirements.txt")


def build(root: Path, dev: bool = False, python: str | None = None, quiet: bool = True,
          action: str | None = None) -> None:
    root = Path(root)
    action = action or plan_action(root, dev)
    vdir = venv_dir(root)
    if action == "install" and read_stamp(root).get("dev"):
        dev = True                      # keep the dev extras of a bin/dev-setup.sh venv
    if action == "ok":
        return
    if action == "rebuild":
        if vdir.exists():
            shutil.rmtree(vdir)
        vdir.parent.mkdir(parents=True, exist_ok=True)
        print("[swarm] creating .swarm/venv (first run) ...", file=sys.stderr)
        _run([python or sys.executable, "-m", "venv", str(vdir)], quiet)
        _run([str(venv_python(root)), "-m", "pip", "install", "-q", "--upgrade", "pip"], quiet)
    print("[swarm] installing bin/requirements ...", file=sys.stderr)
    for f in requirement_files(root, dev):
        _run([str(venv_python(root)), "-m", "pip", "install", "-q", "-r", str(f)], quiet)
    # written by the venv interpreter so platform/python describe the venv itself
    stamp = subprocess.run([str(venv_python(root)), "-c",
                            "import json,sys,platform;print(json.dumps([sys.platform, platform.machine(), "
                            "'%d.%d' % sys.version_info[:2]]))"],
                           capture_output=True, text=True, check=True)
    plat, mach, pyver = json.loads(stamp.stdout)
    want = wanted_stamp(root, dev)
    want.update(platform=plat, machine=mach, python=pyver)
    (vdir / STAMP).write_text(json.dumps(want, indent=2))


def ensure(root: Path, argv: list[str], script: Path, dev: bool = False) -> None:
    """Build/refresh the venv if needed, then re-exec inside it. Returns only
    when already running inside an up-to-date venv (or when disabled)."""
    if os.environ.get("DAGS_NO_VENV"):
        return
    try:
        check_python()
        root = Path(root)
        dev = dev or bool(os.environ.get("DAGS_DEV"))
        if in_venv(root):
            action = plan_action(root, dev)
            if action == "install":
                build(root, dev, action="install")
            elif action == "rebuild":
                raise VenvError(".swarm/venv looks broken; delete it and run again")
            return
        base_python = sys.executable
        action = plan_action(root, dev)
        if action != "ok":
            build(root, dev, python=base_python, action=action)
    except VenvError as e:
        print(f"[swarm] {e}", file=sys.stderr)
        raise SystemExit(2) from None
    py = str(venv_python(root))
    os.execv(py, [py, str(script), *argv[1:]])
