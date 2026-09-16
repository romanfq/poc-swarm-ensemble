#!/usr/bin/env bash
# Create .swarm/venv with bin/requirements-dev.txt and run the full test suite
# (including the typer/rich/textual tests that are skipped without those packages).
#
#   ./bin/dev-setup.sh            # set up (first time) and run all tests
#   ./bin/dev-setup.sh -k board   # extra args go to pytest
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
"$PY" - <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "bin")
from dags import venv
venv.check_python()
root = Path(".").resolve()
action = venv.plan_action(root, dev=True)
if action != "ok":
    venv.build(root, dev=True, quiet=False, action=action)
print(f"[dev-setup] .swarm/venv ready ({action})")
PYEOF

exec .swarm/venv/bin/python -m pytest -q tests "$@"
