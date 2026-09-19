#!/usr/bin/env python3
"""The local poller (whitepaper Ch.9.4) — notification without a hosted service.

Each cycle:
  1. sync the coordination repo;
  2. diff against the last-seen state (.swarm/poller-state.json): newly
     awaiting review, stale heartbeats, more than one live claim;
  3. for tasks with an open PR, read live review/CI state with gh; merged ->
     done, closed -> rejected, new changes-requested review -> reopened;
     overlapping files between open swarm PRs -> merge-order decision;
  4. notify (log file, desktop, webhook);
  5. escalate a repeatedly racing task as "needs arbitration".
It also watches the output contract (Ch.7.3): once this machine's worker has
opened its PR, ``.swarm-task/`` is removed from the worktree *before* the
completion is announced, and the worktree itself goes once the PR is merged
or closed.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import resolve  # noqa: E402
from dags import actions, feed, gh, snapshot, worktree  # noqa: E402
from dags import ledger as L  # noqa: E402
from dags import records as R  # noqa: E402

log = logging.getLogger("dags.poll")

STATE_FILE = "poller-state.json"
# feed events this machine's scheduler already sends to notify when it writes them
SCHEDULER_ANNOUNCES = {"event:worker-dispatched", "event:awaiting-worker",
                       "event:pause-requested", "event:pause-lifted"}


@dataclass
class PollReport:
    events: list[tuple[str, str]] = field(default_factory=list)      # (kind, text)
    stamps: list[str | None] = field(default_factory=list)            # per event: the record's wall_utc
    needs_arbitration: list[str] = field(default_factory=list)
    overlaps: list[tuple[str, str, list[str]]] = field(default_factory=list)
    pr_status: dict[str, dict] = field(default_factory=dict)          # task key -> {state, review, checks, url}

    def add(self, kind: str, text: str, at: str | None = None) -> None:
        self.events.append((kind, text))
        self.stamps.append(at)


class Poller:
    def __init__(self, ctx, notify=None, use_gh: bool = True):
        self.ctx = ctx
        self.notify = notify or (lambda text, kind="info", at=None: log.info(text))
        self.use_gh = use_gh
        self.state_path = ctx.swarm_dir / STATE_FILE
        self.state = self._load()

    # -- persisted state ----------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault("seen_records", [])
        data.setdefault("states", {})
        data.setdefault("stale", [])
        data.setdefault("conflicts", {})          # key -> {signature: cycles}
        data.setdefault("needs_arbitration", [])
        data.setdefault("reviews_seen", {})       # key -> [review ids acted on]
        data.setdefault("overlaps", [])
        data.setdefault("announced", [])
        data.setdefault("first_run", True)
        return data

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.state_path)

    # -- cycle ------------------------------------------------------------------------
    def cycle(self) -> PollReport:
        ctx = self.ctx
        rep = PollReport()
        ctx.coord.pull()
        snap = snapshot.take(ctx)
        first = self.state["first_run"]

        self._feed(rep, quiet=first)
        self._diff_states(snap, rep, quiet=first)
        self._conflicts(snap, rep)
        if self.use_gh:
            self._prs(snap, rep)
        self._contract(snap, rep)
        self._sweep_worktrees(snap)

        self.state["first_run"] = False
        self.save()
        for (kind, text), at in zip(rep.events, rep.stamps):
            if at:
                self.notify(text, kind, at=at)      # stamped when it happened, not when seen
            else:
                self.notify(text, kind)
        return rep

    def _feed(self, rep: PollReport, quiet: bool) -> None:
        seen = set(self.state["seen_records"])
        fresh = feed.new_events(self.ctx.root, seen)
        self.state["seen_records"] = sorted(seen)
        if quiet:
            return
        for e in fresh:
            if e.kind == "completion:pr-opened":
                continue            # announced by the contract watcher after cleanup
            if e.kind in SCHEDULER_ANNOUNCES and e.machine == self.ctx.identity:
                continue            # this machine's scheduler already notified it
            rep.add("feed", e.text, e.wall)

    def _diff_states(self, snap: snapshot.Snapshot, rep: PollReport, quiet: bool) -> None:
        old = self.state["states"]
        new = {t.key: t.state for t in snap.work}
        stale_now = []
        for t in snap.work:
            before = old.get(t.key)
            if not quiet and t.state == "awaiting-review" and before != "awaiting-review":
                rep.add("review", f"{t.short} is awaiting review: {t.pr_url}")
            # a claim that expired without a withdrawal: its machine went quiet (Ch.6.5, Ch.8)
            expired = [c for c in t.res.claims
                       if c not in t.res.valid and c.id not in resolve.read_withdrawals(t.dir)
                       and c.id not in t.res.outcome.completed_claims]
            if expired and t.state in ("open",) and not t.res.outcome.kind:
                latest = max(expired, key=lambda c: c.sort_key)
                stale_now.append(latest.id)
                if latest.id not in self.state["stale"] and not quiet:
                    rep.add("stale", f"{t.short}: heartbeat from {latest.machine} went stale; "
                                     f"the task is ready to be resumed from its checkpoint")
        self.state["states"] = new
        self.state["stale"] = sorted(set(self.state["stale"]) | set(stale_now))

    def _conflicts(self, snap: snapshot.Snapshot, rep: PollReport) -> None:
        threshold = self.ctx.settings.thrash_threshold
        conflicts = self.state["conflicts"]
        flagged = set(self.state["needs_arbitration"])
        for t in snap.work:
            sig = resolve.conflict_signature(t.res)
            history = resolve.race_history(t.dir)
            count = max(history.values(), default=0)
            if sig:
                per = conflicts.setdefault(t.key, {})
                per[sig] = per.get(sig, 0) + 1
                if per[sig] == 1:
                    rep.add("conflict", f"{t.short}: {sig.replace('|', ' and ')} both hold a live claim")
                count = max(count, per[sig])
            resolved = t.res.arbitration is not None or t.state in ("done", "awaiting-review")
            if count >= threshold and not resolved:
                if t.key not in flagged:
                    who = sig or max(history, key=history.get)
                    rep.add("needs-arbitration",
                            f"{t.short} needs arbitration: {who.replace('|', ' and ')} keep racing for it "
                            f"(Ch.6.6 — reassign or freeze it on the Board)")
                flagged.add(t.key)
            elif resolved:
                flagged.discard(t.key)
        self.state["needs_arbitration"] = sorted(flagged)
        rep.needs_arbitration = sorted(flagged)

    def _prs(self, snap: snapshot.Snapshot, rep: PollReport) -> None:
        open_prs: list[tuple[snapshot.TaskView, str, str]] = []
        for t in snap.work:
            if t.state not in ("awaiting-review",) or not t.pr_url:
                continue
            found = gh.repo_from_pr_url(t.pr_url)
            if not found:
                continue
            repo, number = found
            try:
                pr = gh.pr_view(repo, number)
            except (gh.GhError, FileNotFoundError) as e:
                log.warning("gh pr view %s failed: %s", t.pr_url, e)
                continue
            rep.pr_status[t.key] = {"state": pr.get("state"), "review": pr.get("reviewDecision"),
                                    "checks": gh.checks_summary(pr), "url": t.pr_url}
            state = pr.get("state")
            if state == "MERGED":
                actions.record_merged(self.ctx, t.dir, t.pr_url, merged_by=None)
                rep.add("merged", f"{t.short} was merged — done; dependants can start")
                self._cleanup_worktree(t)
                continue
            if state == "CLOSED":
                L.complete(self.ctx, t.dir, "rejected", pr_url=t.pr_url,
                           reason="PR closed without merge (reject-approach, Ch.9.2)")
                self._backend(t, "blocked",
                              f"DAGS: PR {t.pr_url} was closed — approach rejected. Re-plan this task, "
                              f"add the lesson to CONVENTIONS.md, then set swarm:status:ready.")
                rep.add("rejected", f"{t.short}: approach rejected (PR closed); waiting for re-planning")
                self._cleanup_worktree(t)
                continue
            open_prs.append((t, repo, number))
            seen = set(self.state["reviews_seen"].get(t.key, []))
            seen |= {str(d.get("review_id")) for _, d in R.read_dir(t.dir / "completions")
                     if d.get("kind") == "reopened" and d.get("review_id")}
            for review in pr.get("reviews") or []:
                rid = str(review.get("id") or f"{review.get('submittedAt')}-{(review.get('author') or {}).get('login')}")
                if review.get("state") != "CHANGES_REQUESTED" or rid in seen:
                    continue
                seen.add(rid)
                L.complete(self.ctx, t.dir, "reopened", pr_url=t.pr_url, review_id=rid,
                           reviewer=(review.get("author") or {}).get("login"))
                self._backend(t, "ready", None)
                rep.add("changes-requested",
                        f"Changes requested on {t.short} by {(review.get('author') or {}).get('login')}; "
                        f"it goes back to the queue and resumes from its checkpoint")
                break
            self.state["reviews_seen"][t.key] = sorted(seen)
        self._overlaps(open_prs, rep)

    def _overlaps(self, open_prs, rep: PollReport) -> None:
        files: dict[str, tuple[str, set[str], str]] = {}
        for t, repo, number in open_prs:
            try:
                files[t.key] = (repo, set(gh.pr_files(repo, number)), t.short)
            except (gh.GhError, FileNotFoundError):
                continue
        known = {tuple(x) for x in self.state["overlaps"]}
        keys = sorted(files)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                ra, fa, sa = files[a]
                rb, fb, sb = files[b]
                shared = sorted(fa & fb) if ra == rb else []
                if not shared:
                    continue
                rep.overlaps.append((sa, sb, shared))
                if (a, b) not in known:
                    known.add((a, b))
                    rep.add("overlap", f"{sa} and {sb} both change {', '.join(shared[:5])} in {ra}; "
                                       f"decide which merges first")
        self.state["overlaps"] = sorted(list(k) for k in known)

    def _contract(self, snap: snapshot.Snapshot, rep: PollReport) -> None:
        """Output contract watcher (Ch.7.3, Ch.10.7)."""
        announced = set(self.state["announced"])
        for t in snap.work:
            out = t.res.outcome
            if out.kind not in ("pr-opened", "done") or not out.record:
                continue
            owner = (out.record or {}).get("machine")
            wt = worktree.worktree_path(self.ctx, t.dir)
            if owner == self.ctx.identity and out.kind == "pr-opened":
                cp = t.checkpoint
                if cp.get("pr_url") != out.pr_url:
                    continue            # checkpoint not updated yet: contract not fulfilled
                worktree.strip_skill(wt)
            marker = f"{t.key}@{out.record.get('logical_clock')}"
            if out.kind == "pr-opened" and marker not in announced:
                announced.add(marker)
                worker = out.record.get("worker") or "the worker"
                rep.add("finished", f"{worker} has finished {t.short}. The PR can be found at {out.pr_url}")
        self.state["announced"] = sorted(announced)

    def _cleanup_worktree(self, t) -> None:
        try:
            worktree.cleanup(self.ctx, t.dir)
        except Exception as e:  # noqa: BLE001
            log.warning("could not remove the worktree of %s: %s", t.short, e)

    def _sweep_worktrees(self, snap: snapshot.Snapshot) -> None:
        """Remove this machine's worktrees for tasks that are finished, however
        they got there: merged on the Board, in the web UI, or closed (K1)."""
        for t in snap.work:
            if t.state in ("done", "rejected") and worktree.worktree_path(self.ctx, t.dir).exists():
                self._cleanup_worktree(t)

    def _backend(self, t, status: str, comment: str | None) -> None:
        from backends.base import TaskRef
        ref = TaskRef(t.key)
        try:
            self.ctx.backend.set_status(ref, status)
            if comment:
                self.ctx.backend.post_comment(ref, comment)
        except Exception as e:  # noqa: BLE001
            log.warning("backend update for %s failed: %s", t.key, e)


def main(argv: list[str] | None = None) -> int:
    """`poll.py [--once]` — standalone poller for a human who only wants alerts."""
    import argparse
    import time

    from dags.config import Context
    from dags.notify import Notifier

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--identity")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ctx = Context(identity=args.identity)
    poller = Poller(ctx, Notifier(ctx.swarm_dir, ctx.local))
    while True:
        poller.cycle()
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
