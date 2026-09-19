"""The status panel (whitepaper Ch.5.4).

``status_rows`` is plain data (tested without rich); ``render`` turns it into
the rich Panel a human scans in one glance.
"""
from __future__ import annotations

from dags import boardview, daemon, snapshot


def status_rows(ctx, identity_new: bool = False, synced: bool = True, share: int | None = None,
                poll_interval: float | None = None, snap=None) -> list[tuple[str, str, str]]:
    """[(label, value, style)] — style is one of ok / warn / off / plain."""
    snap = snap or snapshot.take(ctx, share=share)
    pid = daemon.running_pid(ctx)
    inf = daemon.info(ctx) if pid else {}
    opts = inf.get("options") or {}
    threads = inf.get("threads") or {}
    machine = snap.machines.get(ctx.identity, {})
    share = snap.share if snap.share is not None else opts.get("quota_share", share)
    poll_interval = opts.get("poll_interval", poll_interval)

    rows = [("identity", f"{ctx.identity}{' (new)' if identity_new else ''}  ·  operator {ctx.operator}", "plain")]
    ready = len(snap.ready)
    rows.append(("coordination", f"{'synced' if synced else 'NOT synced'} -- {ready} task{'s' if ready != 1 else ''} ready",
                 "ok" if synced else "warn"))

    def thread_row(name: str, extra: str) -> tuple[str, str, str]:
        t = threads.get(name)
        if not pid:
            return (name, f"○ stopped   {extra}", "off")
        if t and not t.get("alive"):
            return (name, f"✗ dead      {extra}", "warn")
        err = f"  (last error: {t['error']})" if t and t.get("error") else ""
        paused = name == "scheduler" and machine.get("paused")
        return (name, f"● {'paused ' if paused else 'running'}   {extra}{err}", "warn" if paused or err else "ok")

    rows.append(thread_row("scheduler", f"quota-share {share if share is not None else '?'}"
                                        f"  ·  default worker {opts.get('default_worker') or 'ask on Board'}"))
    rows.append(thread_row("poller", f"interval {int(poll_interval or 0)}s" if poll_interval else ""))
    rows.append(("board", "● available  textual, `swarm.py board` or start --attach to view", "ok"))
    rows.append(("quota", f"{snap.quota_used}/{snap.quota_n} in use swarm-wide  ·  this machine {snap.mine_used}"
                          f"/{share if share is not None else '?'}", "plain"))
    failed = [t for t in snap.live_claims if t.dispatch_failed]
    for t in failed:
        rows.append(("not started", f"{t.short} on {t.owner_machine}: {boardview.dispatch_failed_text(t)}", "warn"))
    waiting = [t for t in snap.awaiting_worker() if not t.dispatch_failed]
    if waiting:
        rows.append(("needs you", "worker choice for " + ", ".join(t.short for t in waiting), "warn"))
    pending = snap.plans_pending()
    if pending:
        rows.append(("plans", "awaiting review: " + ", ".join(t.short for t in pending), "warn"))
    if snap.awaiting_review:
        rows.append(("review", ", ".join(f"{t.short} {t.pr_url}" for t in snap.awaiting_review), "warn"))
    if pid:
        rows.append(("daemon", f"pid {pid}  ·  log .swarm/swarm.log", "plain"))
    return rows


STYLES = {"ok": "green", "warn": "yellow", "off": "dim", "plain": ""}


def render(rows: list[tuple[str, str, str]]):
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    for label, value, style in rows:
        table.add_row(label, Text(value, style=STYLES[style]))
    return Panel(table, title="swarm", box=box.ROUNDED, expand=False)
