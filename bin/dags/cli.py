"""The ``swarm.py`` command surface (whitepaper Ch.5, plan Phase 4), built with typer.

Every command is a thin shell over dags.* functions, so the logic is tested
without typer and this module only has to parse arguments and print.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

import resolve
from backends.base import SWARM_STATUSES, TaskRef
from dags import actions, daemon, gh, panel, plan, prereqs, repos, snapshot, timeutil, work
from dags import ledger as L
from dags.config import ConfigError, Context
from dags.gitsync import GitError

console = Console()
err = Console(stderr=True)

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="DAGS — Distributed AGent Swarm. One command to join the swarm (Ch.5).")
task_app = typer.Typer(no_args_is_help=True, help="Inspect and act on single tasks.")
backend_app = typer.Typer(no_args_is_help=True, help="Talk to the bound issue backend (Ch.3).")
plan_app = typer.Typer(no_args_is_help=True, help="Mirror the tracker into the ledger.")
quota_app = typer.Typer(no_args_is_help=True, help="The global quota N (Ch.7.1).")
epic_app = typer.Typer(no_args_is_help=True, help="Epic takeover (Ch.10.4).")
app.add_typer(task_app, name="task")
app.add_typer(backend_app, name="backend")
app.add_typer(plan_app, name="plan")
app.add_typer(quota_app, name="quota")
app.add_typer(epic_app, name="epic")

SCRIPT = Path(__file__).resolve().parent.parent / "swarm.py"
KNOWN_ERRORS = (ConfigError, work.WorkError, actions.ActionError, L.LostClaim, gh.GhError, GitError,
                repos.RepoError, ValueError, KeyError, RuntimeError)

_state: dict = {}


def ctx() -> Context:
    if "ctx" not in _state:
        _state["ctx"] = Context(_state.get("root"), identity=_state.get("identity"))
    return _state["ctx"]


def fail(message: str, code: int = 1):
    err.print("[bold red]error:[/]", escape(str(message)))
    raise typer.Exit(code)


def guarded(fn):
    """Turn expected failures into one red line instead of a traceback."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (typer.Exit, click.exceptions.Exit, click.exceptions.Abort, click.ClickException):
            raise
        except KNOWN_ERRORS as e:
            if os.environ.get("DAGS_DEBUG"):
                raise
            fail(str(e))
    return wrapper


def task_dir(key: str) -> Path:
    return ctx().task_dir_for(key)


def calibrate_clock() -> str:
    date = gh.server_date()
    if not date:
        return "clock skew: not measured (gh api unavailable)"
    timeutil.set_offset(timeutil.offset_from_http_date(date))
    return f"clock skew: {timeutil.offset():+.1f}s vs GitHub"


@app.callback()
def main_options(
    root: Optional[Path] = typer.Option(None, "--root", help="Coordination repo clone (default: this repo)."),
    identity: Optional[str] = typer.Option(None, "--identity",
                                           help="Override the machine identity (several 'machines' on one computer)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    _state.clear()
    _state["root"] = root or os.environ.get("DAGS_ROOT")
    _state["identity"] = identity or os.environ.get("DAGS_IDENTITY")
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# lifecycle (Ch.5.3)
# ---------------------------------------------------------------------------

@app.command()
@guarded
def start(
    quota_share: int = typer.Option(1, "--quota-share", help="Max tasks this machine may hold at once."),
    poll_interval: str = typer.Option("60s", "--poll-interval"),
    cycle_interval: str = typer.Option("30s", "--cycle-interval"),
    default_worker: Optional[str] = typer.Option(None, "--default-worker",
                                                 help="claude | intellij | vscode; omit to choose per task on the Board."),
    attach: bool = typer.Option(False, "--attach", help="Stay attached showing the Swarm Board."),
    skip_prereqs: bool = typer.Option(False, "--skip-prereqs", hidden=True),
    no_poller: bool = typer.Option(False, "--no-poller"),
):
    """Join the swarm: prerequisites, sync, repos, identity, then start the daemon."""
    c = ctx()
    if not skip_prereqs:
        checks = prereqs.run()
        for chk in checks:
            if not chk.ok:
                (err if chk.fatal else console).print(
                    f"[{'red' if chk.fatal else 'yellow'}]{'✗' if chk.fatal else '!'} {chk.name}[/]:",
                    escape(chk.detail))
        bad = prereqs.failures(checks)
        if bad:
            fail("prerequisites missing: " + ", ".join(b.name for b in bad), 2)
    if default_worker:
        import workers
        default_worker = workers.resolve_name(default_worker)
    identity_new = c.identity_is_new() and not _state.get("identity")
    _ = c.identity
    c.require_human()
    skew = calibrate_clock()
    c.coord.pull()
    synced = True
    try:
        report = plan.sync(c)
        if report.errors:
            console.print("[yellow]plan sync warnings:[/]", escape("; ".join(report.errors[:3])))
    except Exception as e:  # noqa: BLE001
        synced = False
        console.print("[yellow]plan sync failed:[/]", escape(str(e)))
    for repo, result in repos.ensure_all(c).items():
        if isinstance(result, Exception):
            console.print(f"[yellow]{escape(repo)}:[/]", escape(str(result)))
    L.control(c, "start", quota_share=quota_share, default_worker=default_worker)
    opts = daemon.Options(quota_share=quota_share, poll_interval=daemon.parse_interval(poll_interval),
                          cycle_interval=daemon.parse_interval(cycle_interval),
                          default_worker=default_worker, identity=_state.get("identity"), no_poller=no_poller)
    pid = daemon.running_pid(c)
    if pid:
        console.print(f"[yellow]daemon already running (pid {pid}); settings recorded, not restarted[/]")
    else:
        daemon.spawn(c, opts, SCRIPT)
    console.print(panel.render(panel.status_rows(c, identity_new=identity_new, synced=synced,
                                                 share=quota_share, poll_interval=opts.poll_interval)))
    console.print(f"[dim]{skew}[/]")
    if attach:
        board(web=False, port=4590)


@app.command("_daemon", hidden=True)
def run_daemon(
    quota_share: int = typer.Option(1, "--quota-share"),
    poll_interval: float = typer.Option(60, "--poll-interval"),
    cycle_interval: float = typer.Option(30, "--cycle-interval"),
    default_worker: Optional[str] = typer.Option(None, "--default-worker"),
    identity: Optional[str] = typer.Option(None, "--identity"),
    no_poller: bool = typer.Option(False, "--no-poller"),
):
    c = Context(_state.get("root"), identity=identity or _state.get("identity"))
    daemon.setup_logging(c)
    try:
        calibrate_clock()
    except Exception:  # noqa: BLE001
        pass
    opts = daemon.Options(quota_share, poll_interval, cycle_interval, default_worker, identity, no_poller)
    raise typer.Exit(daemon.Daemon(c, opts).run())


@app.command()
@guarded
def stop(wait: float = typer.Option(120, help="Seconds to wait for the loops to finish their cycle.")):
    """Stop this machine's swarm; leases lapse through the normal heartbeat timeout."""
    c = ctx()
    L.control(c, "stop")
    if not daemon.running_pid(c):
        console.print("no daemon running; stop recorded")
        return
    ok = daemon.terminate(c, wait)
    console.print("[green]stopped[/]" if ok else "[yellow]daemon still finishing; check .swarm/swarm.log[/]")


@app.command()
@guarded
def pause(machine: Optional[str] = typer.Option(None, help="Another machine (defaults to this one).")):
    """Stop claiming new tasks; in-flight work carries on (Ch.10.2)."""
    actions.pause(ctx(), machine)
    console.print(f"paused {machine or ctx().identity}")


@app.command()
@guarded
def resume(machine: Optional[str] = typer.Option(None)):
    """Resume claiming."""
    actions.resume(ctx(), machine)
    console.print(f"resumed {machine or ctx().identity}")


@app.command()
@guarded
def throttle(share: int, machine: Optional[str] = typer.Option(None)):
    """Change this machine's quota share live, without a restart."""
    actions.throttle(ctx(), share, machine)
    console.print(f"quota share for {machine or ctx().identity} is now {share}")


@app.command()
@guarded
def status(as_json: bool = typer.Option(False, "--json")):
    """Print the status panel (Ch.5.4)."""
    c = ctx()
    try:
        c.coord.pull()
        synced = True
    except GitError:
        synced = False
    rows = panel.status_rows(c, synced=synced)
    if as_json:
        typer.echo(json.dumps([{"label": a, "value": b, "style": s} for a, b, s in rows], indent=2))
    else:
        console.print(panel.render(rows))


@app.command()
@guarded
def board(web: bool = typer.Option(False, "--web", help="Serve the Board at http://localhost:4590 (textual-dev)."),
          port: int = typer.Option(4590)):
    """Open the Swarm Board (Ch.10)."""
    c = ctx()
    board_py = Path(__file__).resolve().parent.parent / "board.py"
    env_args = []
    if _state.get("identity"):
        env_args = ["--identity", _state["identity"]]
    if web:
        import shutil
        import subprocess
        textual = shutil.which("textual", path=str(Path(sys.executable).parent)) or shutil.which("textual")
        if not textual:
            console.print("installing textual-dev for `textual serve` (plan §2.11) ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "textual-dev"], check=True)
            textual = str(Path(sys.executable).parent / "textual")
        import shlex
        cmd = shlex.join([sys.executable, str(board_py), "--root", str(c.root), *env_args])
        os.execv(textual, [textual, "serve", "--port", str(port), cmd])
    import board as board_mod
    board_mod.run(c)


@app.command()
@guarded
def protect(repo: str, branch: str = typer.Option("main"), approvals: int = typer.Option(1),
            apply: bool = typer.Option(False, "--apply", help="Actually call the GitHub API (humans only).")):
    """Branch protection for a code repo (Ch.5.5, plan §2.11). Prints the call unless --apply."""
    body = json.dumps(actions.protection_body(approvals), indent=2)
    cmd = actions.protection_command(repo, branch)
    console.print("[bold]gh " + " ".join(cmd) + "[/] <<'EOF'")
    console.print(body, markup=False, highlight=False)
    console.print("EOF")
    console.print("[yellow]On GitHub Free this only applies to public repositories (Ch.5.5).[/]")
    if not apply:
        console.print("[dim]dry run — re-run with --apply to set it[/]")
        return
    ctx().require_human()
    if not typer.confirm(f"Set branch protection on {repo}:{branch}?"):
        raise typer.Exit(1)
    gh.gh(cmd, input=body)
    console.print("[green]branch protection set[/]")


# ---------------------------------------------------------------------------
# plan / backend (Ch.3, plan §2.7)
# ---------------------------------------------------------------------------

@plan_app.command("sync")
@guarded
def plan_sync():
    """Mirror the tracker into tasks/*/meta.yaml."""
    rep = plan.sync(ctx())
    console.print(rep.summary())
    for e in rep.errors:
        console.print(escape(str(e)), style="yellow")


def _ref_for(key: str) -> TaskRef:
    d = resolve.lookup(ctx().root, key)
    if d is not None:
        return TaskRef(str(resolve.read_meta(d)["key"]))
    b = ctx().backend
    parse = getattr(b, "parse_ref", None)
    return parse(key) if parse else TaskRef(key)


@backend_app.command("get-task")
@guarded
def backend_get_task(key: str):
    """Print a task's spec as markdown (becomes .swarm-task/spec.md)."""
    typer.echo(ctx().backend.get_task(_ref_for(key)).spec_markdown())


@backend_app.command("ready")
@guarded
def backend_ready(ledger_only: bool = typer.Option(False, "--ledger", help="Also apply ledger readiness.")):
    """Tasks the backend holds ready (optionally ∩ ledger readiness)."""
    c = ctx()
    refs = c.backend.ready_tasks()
    if ledger_only:
        snap = snapshot.take(c)
        ready = {t.key for t in snap.ready}
        refs = [r for r in refs if r.key in ready]
    for r in refs:
        typer.echo(r.key)


@backend_app.command("set-status")
@guarded
def backend_set_status(key: str, status: str):
    """Set the swarm:status label."""
    if status not in SWARM_STATUSES:
        fail(f"status must be one of {', '.join(SWARM_STATUSES)}")
    ctx().backend.set_status(_ref_for(key), status)


@backend_app.command("init")
@guarded
def backend_init(apply: bool = typer.Option(False, "--apply", help="Create the labels on GitHub (humans only).")):
    """Create the swarm labels in the plan repo (prints the commands unless --apply)."""
    c = ctx()
    b = c.backend
    if not hasattr(b, "init_commands"):
        fail(f"the {b.name} backend needs no initialisation")
    code_repos = sorted((c.backend_cfg.get("repos") or {}).keys())
    cmds = b.init_commands(code_repos)
    for cmd in cmds:
        console.print("gh " + " ".join(f"'{a}'" if " " in a else a for a in cmd), markup=False)
    if not apply:
        console.print("[dim]dry run — re-run with --apply to create them[/]")
        return
    c.require_human()
    if not typer.confirm(f"Create/update {len(cmds)} labels in {getattr(b, 'repo', '?')}?"):
        raise typer.Exit(1)
    for cmd in cmds:
        gh.gh(cmd)
    console.print("[green]labels ready[/]")


# ---------------------------------------------------------------------------
# human levers
# ---------------------------------------------------------------------------

@quota_app.command("set")
@guarded
def quota_set(n: int, reason: str = typer.Option("", "--reason")):
    """Set the global concurrency cap N."""
    actions.set_quota(ctx(), n, reason)
    console.print(f"global quota is now {n}")


@quota_app.command("show")
@guarded
def quota_show():
    s = snapshot.take(ctx())
    console.print(f"{s.quota_used}/{s.quota_n} in use; this machine {s.mine_used}")


@epic_app.command("takeover")
@guarded
def epic_takeover(epic: str):
    actions.takeover(ctx(), _ref_for(epic).key)
    console.print(f"{ctx().identity} took over {epic}")


@epic_app.command("release")
@guarded
def epic_release(epic: str):
    actions.release_epic(ctx(), _ref_for(epic).key)
    console.print(f"{ctx().identity} released {epic}")


@task_app.command("list")
@guarded
def task_list(all_states: bool = typer.Option(False, "--all", help="Include done tasks.")):
    from rich.table import Table
    c = ctx()
    c.coord.pull()
    snap = snapshot.take(c)
    t = Table("task", "title", "state", "owner", "worker", "repo", "PR")
    for v in snap.work:
        if v.state == "done" and not all_states:
            continue
        state = v.state + (" (ready)" if v.ready else "")
        t.add_row(*(Text(x) for x in (v.short, v.title, state, v.owner_machine or "", v.worker or "",
                                     v.repo or "", v.pr_url or "")))
    console.print(t)


@task_app.command("show")
@guarded
def task_show(key: str):
    c = ctx()
    snap = snapshot.take(c)
    v = snap.by_key(key)
    if v is None:
        fail(f"no task {key}")
    data = {
        "key": v.key, "short": v.short, "title": v.title, "state": v.state, "ready": v.ready,
        "epic": v.epic, "repo": v.repo, "autonomy": v.autonomy, "retries": v.retries,
        "winner": v.winner.id if v.winner else None, "resolution": v.res.reason,
        "claims": [c_.id for c_ in v.res.claims], "live": [c_.id for c_ in v.res.valid],
        "arbitration": v.res.arbitration, "plan_status": v.plan_status, "pr": v.pr_url,
        "checkpoint": {k: val for k, val in v.checkpoint.items() if k != "plan_md"},
        "dir": str(v.dir.relative_to(c.root)),
    }
    typer.echo(json.dumps(data, default=str, indent=2))


@task_app.command("worker")
@guarded
def task_worker(key: str, choice: str = typer.Argument(..., help="a/claude, b/intellij, c/vscode")):
    """Choose the worker for a task this machine claimed (Ch.10.7)."""
    label = work.choose_worker(ctx(), task_dir(key), choice)
    console.print(f"[swarm-board] Ok, you have selected {label}. Handing over {key} to it — "
                  f"when done, it will announce with the PR link here.", markup=False)


@task_app.command("freeze")
@guarded
def task_freeze(key: str, reason: str = typer.Option(..., "--reason")):
    actions.freeze(ctx(), task_dir(key), reason)
    console.print(f"{key} frozen")


@task_app.command("unfreeze")
@guarded
def task_unfreeze(key: str, reason: str = typer.Option("unfrozen", "--reason")):
    actions.unfreeze(ctx(), task_dir(key), reason)
    console.print(f"{key} unfrozen")


@task_app.command("reassign")
@guarded
def task_reassign(key: str, winner: str, reason: str = typer.Option(..., "--reason")):
    """Award a task to one of its claimants (claim id or machine)."""
    actions.reassign(ctx(), task_dir(key), winner, reason)
    console.print(f"{key} awarded to {winner}")


@task_app.command("approve-plan")
@guarded
def task_approve_plan(key: str, reject: bool = typer.Option(False, "--reject"),
                      note: str = typer.Option("", "--note")):
    """Review gate for human-must-review tasks (plan §2.8)."""
    work.approve_plan(ctx(), task_dir(key), "changes-requested" if reject else "approved", note)
    console.print(f"plan for {key} {'sent back' if reject else 'approved'}")


@task_app.command("merge")
@guarded
def task_merge(key: str, force: bool = typer.Option(False, "--force", help="Merge even with failing checks.")):
    """Approve & merge: gh pr review --approve, then gh pr merge --squash (Ch.9.1)."""
    url = actions.merge(ctx(), task_dir(key), allow_failing_checks=force)
    console.print(f"[green]merged[/] {url}")


@task_app.command("open")
@guarded
def task_open(key: str, pr: bool = typer.Option(False, "--pr")):
    d = task_dir(key)
    url = actions.pr_url(d) if pr else actions.ticket_url(ctx(), d)
    if not url:
        fail("no link available")
    typer.echo(url)
    actions.open_url(url)


@task_app.command("release")
@guarded
def task_release(key: str):
    """Give a task back (this machine's claim)."""
    work.release(ctx(), task_dir(key))
    console.print(f"released {key}")


@task_app.command("still-working")
@guarded
def task_still_working(key: str):
    """Answer the idle prompt (plan §2.9)."""
    work.still_working(ctx(), task_dir(key))


# -- used by the injected skill -----------------------------------------------------

@task_app.command("context")
@guarded
def task_context(key: str):
    """Checkpoint, plan status and PR feedback as JSON (swarm-task implement)."""
    typer.echo(json.dumps(work.implement_gate(ctx(), task_dir(key)), default=str, indent=2))


@task_app.command("note")
@guarded
def task_note(key: str,
              summary: Optional[str] = typer.Option(None, "--summary"),
              tried: List[str] = typer.Option([], "--tried"),
              remaining: Optional[List[str]] = typer.Option(None, "--remaining"),
              question: List[str] = typer.Option([], "--question"),
              risk: List[str] = typer.Option([], "--risk")):
    """Record progress in checkpoint.yaml (Ch.8)."""
    work.note(ctx(), task_dir(key), summary=summary, tried=tried, remaining=remaining or None,
              questions=question, risks=risk)


@task_app.command("submit-plan")
@guarded
def task_submit_plan(key: str, file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    status = work.submit_plan(ctx(), task_dir(key), file.read_text(encoding="utf-8"))
    typer.echo(status)


@task_app.command("block")
@guarded
def task_block(key: str, question: str):
    work.block(ctx(), task_dir(key), question)


@task_app.command("done")
@guarded
def task_done(key: str, worktree_path: Path = typer.Option(..., "--worktree", exists=True, file_okay=False),
              skip_tests: bool = typer.Option(False, "--skip-tests", hidden=True)):
    """The output contract: test, commit, push, open the PR (Ch.7.3)."""
    url = work.finish(ctx(), task_dir(key), worktree_path, skip_tests=skip_tests)
    typer.echo(url)


def main() -> None:
    app(prog_name="swarm.py")
