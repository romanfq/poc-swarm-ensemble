#!/usr/bin/env python3
"""bin/swarm.py — one command to join the swarm (whitepaper Ch.5).

    ./bin/swarm.py start --quota-share 3 --poll-interval 60s
    ./bin/swarm.py board
    ./bin/swarm.py stop

On first run this creates .swarm/venv, installs bin/requirements.txt and
re-executes itself inside that venv (Ch.5.2). Set DAGS_NO_VENV=1 to use the
current interpreter as-is.
"""
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
ROOT = BIN.parent
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def main() -> None:
    from dags import venv
    venv.ensure(ROOT, sys.argv, Path(__file__).resolve())
    from dags.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
