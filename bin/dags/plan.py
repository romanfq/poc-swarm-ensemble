"""Plan sync: bound issue backend -> ledger (plan §2.7, whitepaper Ch.11 step 1).

Humans only ever edit the tracker. Every scheduler cycle mirrors it into
``tasks/<EPIC>/<TASK>/meta.yaml`` in one ledger transaction:

* new tasks and epics get a ``meta.yaml`` (never rewritten afterwards);
* later changes to title / epic / dependencies / autonomy / repo become
  append-only revisions in ``meta/`` (``resolve.read_meta`` merges them);
* a task the backend already shows Done (closed before the swarm saw it, or
  closed by hand) gets an imported ``done`` completion so dependants unblock;
* a task whose PR was rejected becomes claimable again once a human sets it
  back to ``swarm:status:ready`` (a ``replanned`` completion);
* repeated failure downgrades the autonomy tier one step (Ch.8).

Backend status labels are a mirror of the ledger; stale mirrors (a claim that
expired while the label still says in-progress) are corrected afterwards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import resolve
from backends.base import Task, TaskRef, downgrade
from dags import records as R
from dags import timeutil

log = logging.getLogger("dags.plan")

TRACKED = ("title", "epic", "dependencies", "autonomy", "repo", "is_epic", "issue_url")
NO_EPIC = "_no-epic"
EPIC_DIR = "_epic"


@dataclass
class SyncReport:
    imported: list[str] = field(default_factory=list)
    revised: list[str] = field(default_factory=list)
    done_imported: list[str] = field(default_factory=list)
    replanned: list[str] = field(default_factory=list)
    downgraded: list[str] = field(default_factory=list)
    status_fixed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.imported or self.revised or self.done_imported or self.replanned or self.downgraded)

    def summary(self) -> str:
        parts = [f"{len(v)} {k.replace('_', ' ')}" for k, v in self.__dict__.items() if v and k != "errors"]
        return ", ".join(parts) or "no changes"


def short_of(backend, ref: TaskRef) -> str:
    fn = getattr(backend, "short_key", None)
    return fn(ref) if fn else ref.key


def meta_fields(backend, t: Task, default_repo: str | None, epic_repos: dict) -> dict:
    repo = t.repo
    if not repo and t.epic:
        repo = epic_repos.get(t.epic.key) or epic_repos.get(short_of(backend, t.epic))
    return {
        "title": t.title,
        "epic": t.epic.key if t.epic else None,
        "dependencies": [d.key for d in t.dependencies],
        "autonomy": t.autonomy,
        "repo": None if t.is_epic else (repo or default_repo),
        "is_epic": t.is_epic,
        "issue_url": t.url,
    }


def task_dir_for(root: Path, backend, t: Task) -> Path:
    if t.is_epic:
        return root / "tasks" / R.slug(short_of(backend, t.ref)) / EPIC_DIR
    epic = R.slug(short_of(backend, t.epic)) if t.epic else NO_EPIC
    return root / "tasks" / epic / R.slug(short_of(backend, t.ref))


def _stamp(ctx, clock: int, **data) -> dict:
    return {**data, "machine": ctx.identity, "logical_clock": clock, "wall_utc": timeutil.iso()}


def sync(ctx, backend=None, push: bool = True) -> SyncReport:
    backend = backend or ctx.backend
    report = SyncReport()
    tasks = {t.ref.key: t for t in backend.all_tasks()}
    # dependencies that live outside the listed set (other repos, archived)
    for t in list(tasks.values()):
        for d in t.dependencies:
            if d.key not in tasks:
                try:
                    tasks[d.key] = backend.get_task(d)
                except Exception as e:  # noqa: BLE001 - unknown deps simply stay unready
                    report.errors.append(f"{d.key}: {e}")
    default_repo = ctx.default_repo()
    epic_repos = dict(ctx.backend_cfg.get("epic_repos") or {})
    settings = ctx.settings
    downgrades: list[tuple[TaskRef, str, int]] = []

    # autonomy downgrades touch the backend first; the ledger only records success
    now = timeutil.now()
    idx = resolve.index(ctx.root)
    for key, d in idx.items():
        t = tasks.get(key)
        if t is None or t.is_epic or t.done:
            continue
        meta = resolve.read_meta(d)
        done_downgrades = int(meta.get("downgrades") or 0)
        failures = resolve.retry_count(d, now, settings.lease_s)
        if failures >= settings.max_retries * (done_downgrades + 1) and t.autonomy != downgrade(t.autonomy):
            new = downgrade(t.autonomy)
            try:
                backend.set_autonomy(t.ref, new)
                backend.post_comment(t.ref, f"DAGS: {failures} failed attempts; autonomy lowered "
                                            f"from {t.autonomy} to {new} for human triage.")
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{key}: downgrade failed: {e}")
                continue
            t.autonomy = new
            downgrades.append((t.ref, new, done_downgrades + 1))

    fixes: list[tuple[TaskRef, str]] = []

    def build():
        written: list[Path] = []
        clock = resolve.next_clock(ctx.root)
        index = resolve.index(ctx.root)
        down = {ref.key: (tier, n) for ref, tier, n in downgrades}

        for key, t in tasks.items():
            fields = meta_fields(backend, t, default_repo, epic_repos)
            d = index.get(key)
            if d is None:
                if t.closed and not t.is_epic and not any(
                        key in [x.key for x in o.dependencies] for o in tasks.values()):
                    continue            # closed and nobody depends on it: ignore
                d = task_dir_for(ctx.root, backend, t)
                if (d / "meta.yaml").exists():   # same short name, different key
                    d = d.with_name(d.name + "-" + R.slug(key)[-8:])
                written.append(R.write_new(d / "meta.yaml", _stamp(
                    ctx, clock, key=key, short=short_of(backend, t.ref),
                    coordination_ref=backend.coordination_ref(t.ref), **fields)))
                clock += 1
                index[key] = d
                report.imported.append(key)
            else:
                current = resolve.read_meta(d)
                changed = {k: v for k, v in fields.items() if current.get(k) != v}
                if key in down:
                    changed["autonomy"], changed["downgrades"] = down[key]
                    report.downgraded.append(key)
                if changed:
                    written.append(R.write_new(d / "meta" / R.meta_revision_name(ctx.identity, clock),
                                               _stamp(ctx, clock, **changed)))
                    clock += 1
                    report.revised.append(key)

        for key, t in tasks.items():
            d = index.get(key)
            if d is None or t.is_epic:
                continue
            outcome = resolve.read_outcome(d)
            if t.done and outcome.kind != "done":
                written.append(R.write_new(d / "completions" / R.completion_name(ctx.identity, "done", clock),
                                           _stamp(ctx, clock, kind="done", imported=True,
                                                  reason="closed in the issue backend")))
                clock += 1
                report.done_imported.append(key)
                continue
            if outcome.kind == "rejected" and t.status == "ready":
                written.append(R.write_new(d / "completions" / R.completion_name(ctx.identity, "replanned", clock),
                                           _stamp(ctx, clock, kind="replanned")))
                clock += 1
                report.replanned.append(key)
                continue
            if t.closed:
                continue
            state = resolve.task_state(d, now, settings.lease_s, ctx.human_names)
            if state == "open" and t.status in ("claimed", "in-progress", "awaiting-review"):
                fixes.append((t.ref, "ready"))
        return written

    msg = "plan sync"
    ctx.coord.transaction(build, f"[swarm] {ctx.identity}: {msg}", push=push)

    for ref, status in fixes:
        try:
            backend.set_status(ref, status)
            report.status_fixed.append(ref.key)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{ref.key}: status fix failed: {e}")
    if report.changed:
        log.info("plan sync: %s", report.summary())
    for err in report.errors:
        log.warning("plan sync: %s", err)
    return report
