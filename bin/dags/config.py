"""Configuration and per-machine context.

Committed (shared):  backend.yaml, humans.yaml, CONVENTIONS.md, templates/
Machine-local:       .swarm/identity, .swarm/local.yaml, .swarm/venv, .swarm/repos/

.swarm/local.yaml (never committed) example:

    human: roman                      # optional; otherwise matched via `gh api user`
    repos:                            # existing checkouts to use instead of cloning
      OWNER/matchwire-backend: ~/Documents/DAGS/matchwire/be/matchwire-backend
    worker_token:
      keychain_service: dags-worker-token   # or: env: DAGS_WORKER_GH_TOKEN, or: none
    bot:
      login: matchwire-bot
      email: 12345+matchwire-bot@users.noreply.github.com
    terminal_app: Terminal            # or iTerm
    intellij_app: IntelliJ IDEA       # or "IntelliJ IDEA CE"
    notify:
      desktop: true
      webhook: https://hooks.example.com/...
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from dags import records as R

ROOT = Path(__file__).resolve().parent.parent.parent   # coordination repo root


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    default_quota: int = 3
    lease_s: float = 15 * 60
    heartbeat_s: float = 3 * 60
    max_retries: int = 3
    human_idle_s: float = 8 * 3600
    thrash_threshold: int = 2

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        d = d or {}
        return cls(
            default_quota=int(d.get("default_quota", 3)),
            lease_s=float(d.get("lease_minutes", 15)) * 60,
            heartbeat_s=float(d.get("heartbeat_minutes", 3)) * 60,
            max_retries=int(d.get("max_retries", 3)),
            human_idle_s=float(d.get("human_idle_hours", 8)) * 3600,
            thrash_threshold=int(d.get("thrash_threshold", 2)),
        )


class Context:
    """Everything a machine needs to act on the swarm."""

    def __init__(self, root: Path | str | None = None, identity: str | None = None,
                 persist_identity: bool = False):
        self.root = Path(root or ROOT).resolve()
        self.swarm_dir = self.root / ".swarm"
        self.worktrees_dir = self.root / ".worktrees"
        self._identity_override = R.slug(identity) if identity else None
        self._backend = None
        #: set when --identity differs from the name this clone already has
        self.identity_mismatch: str | None = None
        if self._identity_override and persist_identity:
            stored = self.stored_identity()
            if stored is None:
                self.create_identity(self._identity_override)     # remembered from now on (K3)
            elif stored != self._identity_override:
                self.identity_mismatch = stored

    # -- files ----------------------------------------------------------------
    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def conventions(self) -> Path:
        return self.root / "CONVENTIONS.md"

    @cached_property
    def backend_cfg(self) -> dict:
        cfg = R.load_yaml(self.root / "backend.yaml")
        if not cfg:
            raise ConfigError(f"missing or empty {self.root / 'backend.yaml'}")
        return cfg

    @cached_property
    def settings(self) -> Settings:
        return Settings.from_dict(self.backend_cfg.get("swarm", {}))

    @cached_property
    def humans(self) -> list[dict]:
        return [h for h in (R.load_yaml(self.root / "humans.yaml").get("humans") or []) if h.get("name")]

    @property
    def human_names(self) -> set[str]:
        return {str(h["name"]) for h in self.humans}

    def human_by_github(self, login: str | None) -> str | None:
        for h in self.humans:
            if login and str(h.get("github", "")).lower() == login.lower():
                return str(h["name"])
        return None

    def human_by_email(self, email: str | None) -> str | None:
        for h in self.humans:
            if email and email.lower() in [str(e).lower() for e in h.get("emails") or []]:
                return str(h["name"])
        return None

    @property
    def local(self) -> dict:
        return R.load_yaml(self.swarm_dir / "local.yaml")

    # -- identity (Ch.5.3 step 3) ----------------------------------------------
    @property
    def identity(self) -> str:
        if self._identity_override:
            return self._identity_override
        path = self.swarm_dir / "identity"
        if path.exists():
            value = path.read_text().strip()
            if value:
                return value
        return self.create_identity()

    def stored_identity(self) -> str | None:
        path = self.swarm_dir / "identity"
        value = path.read_text().strip() if path.exists() else ""
        return value or None

    def set_identity(self, name: str) -> str:
        """Rename this clone's machine identity (``swarm.py identity set``)."""
        value = R.slug(name)
        self.swarm_dir.mkdir(parents=True, exist_ok=True)
        (self.swarm_dir / "identity").write_text(value + "\n")
        self._identity_override = None
        return value

    def identity_is_new(self) -> bool:
        return not (self.swarm_dir / "identity").exists()

    def create_identity(self, name: str | None = None) -> str:
        path = self.swarm_dir / "identity"
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        host = socket.gethostname().split(".")[0].lower()
        value = R.slug(name) if name else f"{R.slug(host)}-{secrets.token_hex(2)}"
        self.swarm_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")
        return value

    @property
    def operator(self) -> str:
        """The human attending this machine, as named in humans.yaml."""
        explicit = self.local.get("human") or os.environ.get("DAGS_HUMAN")
        if explicit:
            return str(explicit)
        cache = self.swarm_dir / "operator"
        if cache.exists() and cache.read_text().strip():
            return cache.read_text().strip()
        from dags import gh
        name = self.human_by_github(gh.login())
        if name:
            self.swarm_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(name + "\n")
            return name
        return "unknown"

    def require_human(self) -> str:
        who = self.operator
        if who not in self.human_names:
            raise ConfigError(
                f"'{who}' is not listed in humans.yaml; add yourself there (and set "
                f"`human:` in .swarm/local.yaml if your GitHub login isn't listed)")
        return who

    # -- repos ------------------------------------------------------------------
    def repo_config(self, repo: str) -> dict:
        return dict((self.backend_cfg.get("repos") or {}).get(repo) or {})

    def default_repo(self) -> str | None:
        return self.backend_cfg.get("default_repo")

    def repo_path(self, repo: str) -> Path:
        mapping = self.local.get("repos") or {}
        if repo in mapping:
            return Path(os.path.expanduser(str(mapping[repo]))).resolve()
        owner, _, name = repo.partition("/")
        return self.swarm_dir / "repos" / owner / name

    # -- worker token (plan §2.1) ---------------------------------------------------
    def worker_token(self) -> str | None:
        """Token used by `swarm-task done` to open PRs as the bot account.
        Returns None only when local.yaml explicitly opts out (`none`)."""
        spec = self.local.get("worker_token") or {"keychain_service": "dags-worker-token"}
        if spec == "none" or (isinstance(spec, dict) and spec.get("mode") == "none"):
            return None
        env_name = spec.get("env", "DAGS_WORKER_GH_TOKEN") if isinstance(spec, dict) else "DAGS_WORKER_GH_TOKEN"
        if os.environ.get(env_name):
            return os.environ[env_name]
        service = spec.get("keychain_service") if isinstance(spec, dict) else None
        if service and sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        raise ConfigError(
            "no worker (bot) GitHub token found. Store it with:\n"
            "  security add-generic-password -a dags-bot -s dags-worker-token -w\n"
            f"or export {env_name}, or set `worker_token: none` in .swarm/local.yaml "
            "to open PRs with your own account (admin-bypass mode).")

    def bot_identity(self) -> tuple[str, str] | None:
        bot = self.local.get("bot") or {}
        if bot.get("login") and bot.get("email"):
            return str(bot["login"]), str(bot["email"])
        return None

    # -- collaborators ---------------------------------------------------------------
    @cached_property
    def coord(self):
        from dags.gitsync import Coord
        return Coord(self.root, self.swarm_dir / "git.lock")

    @property
    def backend(self):
        if self._backend is None:
            from backends import load_backend
            self._backend = load_backend(self)
        return self._backend

    def set_backend(self, backend) -> None:
        self._backend = backend

    def task_dir_for(self, key: str) -> Path:
        import resolve
        d = resolve.lookup(self.root, key)
        if d is None:
            raise ConfigError(f"task {key} is not in the ledger (run `swarm.py plan sync`)")
        return d
