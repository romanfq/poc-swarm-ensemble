"""Backend selection (whitepaper Ch.3.4): backend.yaml names the adapter."""
from __future__ import annotations

import importlib

from backends.base import IssueBackend, Task, TaskRef  # noqa: F401

ADAPTERS = {
    "github": ("backends.github", "GitHubBackend"),
    "fake": ("backends.fake", "FakeBackend"),
}


# Planned adapters that are not built yet.
NOT_YET = {"jira": "see FutureWork.md"}


def load_backend(ctx) -> IssueBackend:
    name = str(ctx.backend_cfg.get("backend", "")).strip()
    if name not in ADAPTERS:
        raise ValueError(f"backend.yaml: unknown backend '{name}' (choose one of {', '.join([*ADAPTERS, *NOT_YET])})")
    if name in NOT_YET:
        raise ValueError(f"backend.yaml: the {name} adapter is not implemented yet ({NOT_YET[name]})")
    module_name, cls_name = ADAPTERS[name]
    cls = getattr(importlib.import_module(module_name), cls_name)
    return cls.from_config(ctx.backend_cfg.get(name) or {}, ctx)
