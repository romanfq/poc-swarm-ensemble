"""The poller's single pluggable notify step (whitepaper Ch.9.4 step 4).

Always appends to ``.swarm/notifications.log`` (a human can ``tail -f`` it);
optionally a macOS desktop notification and/or an outbound webhook, both
configured in ``.swarm/local.yaml``:

    notify:
      desktop: true
      webhook: https://hooks.slack.com/services/...
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from dags import timeutil

log = logging.getLogger("dags.notify")

Listener = Callable[[str, str], None]
_listeners: list[Listener] = []
_lock = threading.Lock()


def subscribe(fn: Listener) -> None:
    """In-process listeners (the Board uses this when it shares a process)."""
    with _lock:
        _listeners.append(fn)


def unsubscribe(fn: Listener) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


class Notifier:
    def __init__(self, swarm_dir: Path, local: dict | None = None, opener=None, run=None,
                 platform: str | None = None):
        self.log_file = Path(swarm_dir) / "notifications.log"
        cfg = (local or {}).get("notify") or {}
        self.desktop = bool(cfg.get("desktop", False))
        self.webhook = cfg.get("webhook")
        self.opener = opener or urllib.request.urlopen
        self.run = run or subprocess.run
        self.platform = platform or sys.platform

    def __call__(self, text: str, kind: str = "info") -> None:
        line = f"{timeutil.iso()} [{kind}] {text}"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.info("%s", line)
        with _lock:
            listeners = list(_listeners)
        for fn in listeners:
            try:
                fn(text, kind)
            except Exception:  # noqa: BLE001
                log.exception("notify listener failed")
        if self.desktop and self.platform == "darwin":
            script = f'display notification {json.dumps(text)} with title "DAGS swarm" subtitle {json.dumps(kind)}'
            try:
                self.run(["osascript", "-e", script], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                log.warning("desktop notification failed")
        if self.webhook:
            body = json.dumps({"text": f"[DAGS] {text}", "kind": kind}).encode()
            req = urllib.request.Request(self.webhook, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with self.opener(req, timeout=10):
                    pass
            except Exception as e:  # noqa: BLE001
                log.warning("webhook notification failed: %s", e)
