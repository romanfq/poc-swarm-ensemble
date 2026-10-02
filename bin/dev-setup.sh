#!/usr/bin/env bash
# Create .swarm/venv with bin/requirements-dev.txt and run the full test suite
# (including the typer/rich/textual tests that are skipped without those packages).
#
#   ./bin/dev-setup.sh            # set up (first time) and run all tests
#   ./bin/dev-setup.sh -k board   # extra args go to pytest
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
# stdout carries only the interpreter to test with; messages go to stderr.
VENV_PY="$("$PY" - <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "bin")
from dags import venv
venv.check_python()
root = Path(".").resolve()
reuse = venv.find_reusable(root, dev=True)
if reuse:
    print(f"[dev-setup] reusing {reuse}", file=sys.stderr)
    print(reuse)
else:
    action = venv.plan_action(root, dev=True)
    if action != "ok":
        venv.build(root, dev=True, quiet=False, action=action)
    print(f"[dev-setup] .swarm/venv ready ({action})", file=sys.stderr)
    print(venv.venv_python(root))
PYEOF
)"

# -n auto: the tests are independent (each builds its own repos under tmp_path). --durations feeds
# the time estimate that "swarm-task done" records in the checkpoint.
exec "$VENV_PY" -m pytest -q -n auto --durations=20 tests "$@"
