"""Worker-side operations behind the injected skill (whitepaper Ch.7.3, Ch.8, Ch.9.3).

``.swarm-task/swarm-task`` is stdlib-only so it runs in any worktree; for
anything that touches the ledger, the backend or GitHub it calls
``swarm.py task ...``, which lands here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import resolve
import workers
from backends.base import TaskRef
from dags import gh, repos, worktree
from dags import ledger as L
from dags import testscope, timeutil

log = logging.getLogger("dags.work")


class WorkError(RuntimeError):
    pass


def _ref(task_dir: Path) -> TaskRef:
    return TaskRef(str(resolve.read_meta(task_dir)["key"]))


def _try_backend(ctx, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 - the backend is a mirror; never fail the ledger step
        log.warning("backend update failed: %s", e)


def my_claim(ctx, task_dir: Path) -> str:
    res = resolve.resolve(task_dir, timeutil.now(), ctx.settings.lease_s, ctx.human_names)
    if res.winner is None or res.winner.machine != ctx.identity or res.winner not in res.valid:
        raise L.LostClaim(f"this machine ({ctx.identity}) does not own {resolve.label(task_dir)}")
    return res.winner.id


# ---------------------------------------------------------------------------
# dispatch (Ch.7.3 "Selecting a worker")
# ---------------------------------------------------------------------------

class _Steps:
    """Times the steps of a dispatch (GH-100): each is logged at DEBUG; the total and the slowest
    go into the ``worker-dispatched`` event, so the next report has numbers."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.spans: dict[str, float] = {}

    @contextlib.contextmanager
    def step(self, name: str):
        t = time.monotonic()
        try:
            yield
        finally:
            self.spans[name] = self.spans.get(name, 0.0) + time.monotonic() - t
            log.debug("dispatch step %s: %.2fs", name, self.spans[name])

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def slowest(self) -> tuple[str, float] | None:
        return max(self.spans.items(), key=lambda kv: kv[1], default=None)


class Dispatched(str):
    """The worker's label, plus how long the hand-over took."""
    elapsed_s: float = 0.0
    slowest: str | None = None

    def message(self, short: str) -> str:
        return f"Handed {short} to {self} ({self.elapsed_s:.1f}s)"


# -- background work: whatever talks to GitHub follows the launch -----------------------------

_background: list[threading.Thread] = []


def _spawn(name: str, fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 - retried later (the next push carries the commit)
            log.warning("%s failed: %s", name, e)
    t = threading.Thread(target=run, name=name, daemon=True)
    _background.append(t)
    t.start()


def drain(timeout: float = 10.0) -> None:
    """Wait (bounded) for the background fetch/push; a short-lived CLI calls it before exiting."""
    deadline = time.monotonic() + timeout
    while _background:
        t = _background.pop(0)
        t.join(max(0.0, deadline - time.monotonic()))


# -- the spec, snapshotted at dispatch (GH-100) --------------------------------------------------

def spec_wait_s() -> float:
    """How long dispatch waits for a live issue read when it has this claim's cached spec."""
    try:
        return float(os.environ.get("DAGS_SPEC_WAIT", "3"))
    except ValueError:
        return 3.0


def _spec_cache_path(ctx, task_dir: Path) -> Path:
    return ctx.swarm_dir / "spec-cache" / f"{worktree.task_slug(task_dir)}.json"


def _store_spec(ctx, task_dir: Path, claim_id: str, spec: str) -> None:
    path = _spec_cache_path(ctx, task_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"claim_id": claim_id, "fetched_utc": timeutil.iso(), "spec": spec}),
                   encoding="utf-8")
    tmp.replace(path)


def _load_spec(ctx, task_dir: Path, claim_id: str) -> str | None:
    """The cached spec, only if it was fetched for this claim: never older than the claim."""
    try:
        data = json.loads(_spec_cache_path(ctx, task_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("spec") if isinstance(data, dict) and data.get("claim_id") == claim_id else None


def _fetch_spec(ctx, task_dir: Path) -> str:
    get = getattr(ctx.backend, "get_task_fresh", None) or ctx.backend.get_task
    return get(_ref(task_dir)).spec_markdown()


class _SpecFetch:
    """A live read of the issue body, started early so it overlaps the local work. ``result``
    returns it; if GitHub is slow or down and this claim's cached spec exists, that is used
    instead (and a slow read still lands in ``on_late`` if it differs)."""

    def __init__(self, ctx, task_dir: Path, claim_id: str):
        self.ctx, self.task_dir, self.claim_id = ctx, task_dir, claim_id
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._box: dict = {}
        self._late = None
        self.cached = _load_spec(ctx, task_dir, claim_id)
        threading.Thread(target=self._run, name=f"spec {task_dir.name}", daemon=True).start()

    def _run(self) -> None:
        try:
            spec = _fetch_spec(self.ctx, self.task_dir)
            _store_spec(self.ctx, self.task_dir, self.claim_id, spec)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._box["err"] = e
            self._done.set()
            return
        with self._lock:
            self._box["spec"] = spec
            late = self._late
        self._done.set()
        if late is not None and spec != self.cached:
            late(spec)

    def result(self, on_late=None) -> str:
        self._done.wait(None if self.cached is None else spec_wait_s())
        with self._lock:
            if "spec" in self._box:
                return self._box["spec"]
            if self.cached is None:
                raise self._box["err"]
            if "err" in self._box:
                log.warning("%s: issue read failed (%s); using the spec fetched at claim time",
                            self.task_dir.name, self._box["err"])
            else:
                self._late = on_late
            return self.cached


def fetch_ahead(ctx, task_dir: Path) -> None:
    """When the claim is taken a human is about to spend seconds choosing a worker: fetch the code
    repo meanwhile, so the worktree is cut from a fresh base. (``prepare`` at that moment also
    leaves the issue body in the spec cache.) Best effort, in the background."""
    repo = resolve.read_meta(task_dir).get("repo")
    if repo:
        _spawn(f"fetch {task_dir.name}", lambda: (ctx.repo_path(repo) / ".git").exists()
               and repos.fetch_origin(repo, ctx.repo_path(repo)))


def prepare(ctx, task_dir: Path, claim_id: str, worker: str | None = None, steps: _Steps | None = None) -> Path:
    steps = steps or _Steps()
    meta = resolve.read_meta(task_dir)
    repo = meta.get("repo")
    if not repo:
        raise WorkError(f"{resolve.label(task_dir)} has no target repo (add a repo: label)")
    spec = _SpecFetch(ctx, task_dir, claim_id)
    base = ctx.repo_config(repo).get("base", "main")
    with steps.step("repo"):
        repo_path = repos.ensure(ctx, repo, fetch=False)     # fetching follows the launch
        if not worktree.worktree_path(ctx, task_dir).joinpath(".git").exists():
            # Nothing to branch from yet, or a resumed task whose branch another machine pushed.
            if not worktree._has_ref(repo_path, f"refs/remotes/origin/{base}") \
                    or L.read_checkpoint(task_dir).get("branch"):
                repos.fetch_origin(repo, repo_path)
    with steps.step("worktree"):
        wt = worktree.ensure(ctx, task_dir, repo_path, base)
    with steps.step("spec"):
        text = spec.result(on_late=lambda late: worktree.write_spec(wt, late))
    with steps.step("inject"):
        worktree.inject(ctx, wt, text, worktree.context_for(ctx, task_dir, claim_id, worker))
    return wt


def choose_worker(ctx, task_dir: Path, choice: str, launch=None, platform: str | None = None) -> str:
    """Record the worker in checkpoint.yaml, inject the skill and hand over.

    The launch comes as soon as the local work is done (GH-100): no pull, no fetch, no push in
    front of it, and the repo lock is taken only for the checkpoint commit, if it is free."""
    steps = _Steps()
    name = workers.resolve_name(choice)
    meta = resolve.read_meta(task_dir)
    autonomy = str(meta.get("autonomy") or "")
    if name not in workers.allowed_for(autonomy):
        raise WorkError(f"{resolve.label(task_dir)} is {autonomy}: only a human worker may take it")
    claim_id = my_claim(ctx, task_dir)
    w = workers.get(name, ctx.local, launch=launch, platform=platform)
    L.require_mine(ctx, task_dir, claim_id, pull=False)
    wt = prepare(ctx, task_dir, claim_id, name, steps=steps)
    task = workers.ClaimedTask(key=str(meta["key"]), short=resolve.label(task_dir),
                               title=str(meta.get("title") or ""), claim_id=claim_id,
                               autonomy=autonomy, repo=meta.get("repo"),
                               branch=worktree.branch_name(task_dir))

    def record():
        slowest = steps.slowest()
        event = {"kind": "worker-dispatched", "worker": w.label, "human": ctx.operator,
                 "elapsed_s": round(steps.elapsed(), 2)}
        if slowest:
            event.update(slowest=slowest[0], slowest_s=round(slowest[1], 2))
        L.update_checkpoint(ctx, task_dir, claim_id, pull=False, push=False,
                            worker=name, worker_label=w.label, branch=worktree.branch_name(task_dir),
                            dispatched_utc=timeutil.iso(), needs_human=None, dispatch_failed=None, event=event)

    recorded = False
    if ctx.coord.lock.try_enter():
        try:
            with steps.step("checkpoint"):
                record()
            recorded = True
        finally:
            ctx.coord.lock.__exit__(None, None, None)
    else:
        log.info("%s: the repo lock is busy; launching first, recording after", task.short)
    elapsed, slowest = steps.elapsed(), steps.slowest()
    w.dispatch(task, wt)
    if not recorded:
        try:
            record()
        except Exception as e:  # noqa: BLE001 - the worker is running; its own commands show a lost claim
            log.warning("%s: recording the hand-over failed: %s", task.short, e)
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "in-progress")
    _spawn(f"push {task.short}", ctx.coord.push)
    if meta.get("repo"):
        _spawn(f"fetch {task.short}", repos.fetch_origin, meta["repo"], ctx.repo_path(meta["repo"]))
    out = Dispatched(w.label)
    out.elapsed_s, out.slowest = elapsed, slowest[0] if slowest else None
    return out


# ---------------------------------------------------------------------------
# checkpoint notes, plan, feedback (Ch.8, Ch.9.3)
# ---------------------------------------------------------------------------

def note(ctx, task_dir: Path, *, summary=None, tried=(), remaining=None, questions=(), risks=()) -> dict:
    claim_id = my_claim(ctx, task_dir)
    return _write_note(ctx, task_dir, claim_id, _note_args(summary, tried, remaining, questions, risks))


def _note_args(summary, tried, remaining, questions, risks) -> dict:
    return {"summary": summary, "tried": list(tried or ()), "remaining": remaining,
            "questions": list(questions or ()), "risks": list(risks or ())}


def _write_note(ctx, task_dir: Path, claim_id: str, a: dict, **kw) -> dict:
    fields = {}
    if a.get("summary") is not None:
        fields["summary"] = a["summary"]
    if a.get("remaining") is not None:
        fields["remaining"] = list(a["remaining"])
    append = {k: list(v) for k, v in (("tried", a.get("tried")), ("open_questions", a.get("questions")),
                                      ("risks", a.get("risks"))) if v}
    return L.update_checkpoint(ctx, task_dir, claim_id, append=append, **fields, **kw)


# -- note without waiting (GH-113): one process, no pull, a local commit, never blocked on the lock ----

def note_lock_wait_s() -> float:
    """How long `note` waits for a busy repo lock before it queues the write."""
    try:
        return float(os.environ.get("DAGS_NOTE_LOCK_WAIT", "2"))
    except ValueError:
        return 2.0


def _note_queue(ctx) -> Path:
    return ctx.swarm_dir / "note-queue"


def _enter_lock(ctx, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while not ctx.coord.lock.try_enter():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _local_note(ctx, task_dir: Path, claim_id: str, a: dict) -> None:
    """Commit locally. Caller holds the repo lock; the next pushing command carries the commit."""
    _write_note(ctx, task_dir, claim_id, a, pull=False, push=False)


def flush_note_queue(ctx) -> int:
    """Replay notes that were queued while the lock was busy. Notes of a claim that is no longer
    ours are dropped. Returns how many were written."""
    qdir = _note_queue(ctx)
    files = sorted(qdir.glob("*.json")) if qdir.is_dir() else []
    if not files:
        return 0
    if not _enter_lock(ctx, note_lock_wait_s()):
        return 0
    n = 0
    try:
        for f in files:
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
                task_dir = Path(item["task_dir"])
                claim_id = my_claim(ctx, task_dir)
                _local_note(ctx, task_dir, claim_id, item["args"])
                n += 1
            except (L.LostClaim, OSError, ValueError, KeyError) as e:
                log.warning("dropped queued note %s: %s", f.name, e)
            f.unlink(missing_ok=True)
    finally:
        ctx.coord.lock.__exit__(None, None, None)
    return n


def note_with_events(ctx, task_dir: Path, claim_id: str, *, summary=None, tried=(), remaining=None,
                     questions=(), risks=()) -> dict:
    """What `swarm-task note` runs (GH-113): the news and the write in one process. Reads the
    ledger as last synced (the daemon pulls), commits locally without a push, and queues the
    note when the repo lock stays busy. A lost claim writes nothing."""
    steps = _Steps()
    with steps.step("events"):
        info = worker_events(ctx, task_dir, claim_id, pull=False)
    out = {"events": info["events"], "written": False, "queued": False}
    if any(e["kind"] == "claim-lost" for e in info["events"]):
        return out
    a = _note_args(summary, tried, remaining, questions, risks)
    with steps.step("lock"):
        got = _enter_lock(ctx, note_lock_wait_s())
    if got:
        try:
            with steps.step("commit"):
                flush_note_queue(ctx)  # older notes first
                _local_note(ctx, task_dir, claim_id, a)
        finally:
            ctx.coord.lock.__exit__(None, None, None)
        out["written"] = True
    else:
        qdir = _note_queue(ctx)
        qdir.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}.json"
        tmp = qdir / (name + ".tmp")
        tmp.write_text(json.dumps({"task_dir": str(task_dir), "args": a}), encoding="utf-8")
        tmp.rename(qdir / name)
        out["queued"] = True
    if os.environ.get("DAGS_TRACE"):
        print("note timings: " + ", ".join(f"{k}={v:.3f}s" for k, v in steps.spans.items())
              + f", total={steps.elapsed():.3f}s", file=sys.stderr)
    return out


def _post(ctx, task_dir: Path, text: str) -> str | None:
    """Post on the task's issue; the comment's URL, or None when the backend is down or has no URL."""
    try:
        return ctx.backend.post_comment(_ref(task_dir), text)
    except Exception as e:  # noqa: BLE001 - the backend is a mirror; never fail the ledger step
        log.warning("backend update failed: %s", e)
        return None


# What marks the comment a reviewer's replies are counted after. The legacy texts keep tasks
# that were started before the markers existed working.
PLAN_MARKER = "<!-- dags-plan: {sha} -->"
BLOCK_MARKER = "<!-- dags-block: {claim} -->"
_MARKER_RE = re.compile(r"<!-- dags-(?:plan|block): [^>]*-->|^DAGS: (?:plan for review|worker needs a human decision)",
                        re.M)


def replies(ctx, task_dir: Path) -> list[dict]:
    """What the listed humans said on the issue since the latest plan or question comment (GH-33).
    Free text; nothing parses it. Raises whatever the backend raises."""
    comments = ctx.backend.list_comments(_ref(task_dir))
    last = max((i for i, c in enumerate(comments) if _MARKER_RE.search(c.body or "")), default=-1)
    out = []
    for c in comments[last + 1:]:
        human = ctx.human_by_github(c.author)
        if human:
            out.append({"human": human, "author": c.author, "body": c.body, "created_at": c.created_at,
                        "url": c.url})
    return out


def plan_sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]


def submit_plan(ctx, task_dir: Path, plan_md: str) -> str:
    if not plan_md.strip():
        raise WorkError("plan.md is empty")
    claim_id = my_claim(ctx, task_dir)
    meta = resolve.read_meta(task_dir)
    sha = plan_sha(plan_md)
    fields = {"plan_md": plan_md, "plan_sha": sha, "needs_human": None}
    self_approved = meta.get("autonomy") == "auto-pr"
    if self_approved:
        fields["plan_self_approved"] = sha            # self-review allowed (plan §2.8)
    cp = L.read_checkpoint(task_dir)
    event = {"kind": "plan-submitted", "plan_sha": sha, "self_approved": self_approved,
             "worker": cp.get("worker_label") or cp.get("worker")}
    L.update_checkpoint(ctx, task_dir, claim_id, event=event, **fields)
    status = resolve.plan_status(task_dir, ctx.human_names)
    if status != "approved":
        url = _post(ctx, task_dir, f"{PLAN_MARKER.format(sha=sha)}\n"
                    f"DAGS: plan for review. Answer the questions below in a comment here; approve on the "
                    f"Swarm Board or with `swarm.py task approve-plan {resolve.label(task_dir)}`.\n\n{plan_md}")
        if url:
            L.update_checkpoint(ctx, task_dir, claim_id, plan_comment_url=url)
    return status


def approve_plan(ctx, task_dir: Path, decision: str = "approved", note_text: str = "") -> None:
    cp = L.read_checkpoint(task_dir)
    if not cp.get("plan_sha"):
        raise WorkError(f"{resolve.label(task_dir)} has no plan to review yet")
    L.review_plan(ctx, task_dir, cp["plan_sha"], decision, note_text)


def pr_for(ctx, task_dir: Path) -> tuple[str, str] | None:
    """(repo, number) of this task's PR, from the ledger or the branch."""
    outcome = resolve.read_outcome(task_dir)
    if outcome.pr_url:
        return gh.repo_from_pr_url(outcome.pr_url)
    repo = resolve.read_meta(task_dir).get("repo")
    if not repo:
        return None
    pr = gh.pr_for_branch(repo, worktree.branch_name(task_dir))
    return (repo, str(pr["number"])) if pr else None


def feedback(ctx, task_dir: Path) -> list[dict]:
    found = pr_for(ctx, task_dir)
    if not found:
        return []
    return gh.parse_feedback(gh.pr_view(*found))


def implement_gate(ctx, task_dir: Path) -> dict:
    """What `swarm-task implement` needs: may we proceed, and the context."""
    my_claim(ctx, task_dir)
    status = resolve.plan_status(task_dir, ctx.human_names)
    cp = L.read_checkpoint(task_dir)
    try:
        fb = feedback(ctx, task_dir)
    except (gh.GhError, FileNotFoundError) as e:
        fb = [{"tag": "error", "text": f"could not read PR feedback: {e}"}]
    try:
        said, said_error = replies(ctx, task_dir), None
    except Exception as e:  # noqa: BLE001 - one API read; the worker can still go on without it
        said, said_error = [], f"could not read the issue's comments: {e}"
    return {"allowed": status == "approved", "plan_status": status, "checkpoint": cp, "feedback": fb,
            "replies": said, "replies_error": said_error}


def block(ctx, task_dir: Path, question: str) -> None:
    flush_note_queue(ctx)
    claim_id = my_claim(ctx, task_dir)
    meta = resolve.read_meta(task_dir)
    if meta.get("autonomy") == "auto-pr":
        _try_backend(ctx, ctx.backend.set_autonomy, _ref(task_dir), "human-must-review")
        L.set_autonomy(ctx, task_dir, "human-must-review", f"plan question: {question}", human=str(ctx.operator))
    url = _post(ctx, task_dir, f"{BLOCK_MARKER.format(claim=claim_id)}\n"
                f"DAGS: worker needs a human decision. Answer in a comment here:\n\n{question}")
    fields = {"question_comment_url": url} if url else {}
    L.update_checkpoint(ctx, task_dir, claim_id, needs_human=question, plan_self_approved=None,
                        append={"open_questions": [question]},
                        event={"kind": "needs-human", "question": question}, **fields)


def answer_question(ctx, task_dir: Path, answer: str) -> None:
    """A human answers the worker's `block` question (Board or `swarm.py task answer`). An
    append-only event, so any machine may write it; the checkpoint stays the worker's."""
    human = ctx.require_human()
    if not answer.strip():
        raise WorkError("an answer can't be empty")
    ctx.coord.pull()
    question = resolve.open_question(task_dir)
    if not question:
        raise WorkError(f"{resolve.label(task_dir)} has no open question")
    cp = L.read_checkpoint(task_dir)
    L.record_event(ctx, task_dir, "human-answered", claim_id=cp.get("claim_id"), human=human,
                   question=question, answer=answer.strip())
    _try_backend(ctx, ctx.backend.post_comment, _ref(task_dir), f"DAGS: {human} answered: {answer.strip()}")


def worker_events(ctx, task_dir: Path, claim_id: str, pull: bool = True) -> dict:
    """What `swarm-task` prints first, and what `swarm-task wait` blocks on (GH-2). Reads the
    ledger after a pull (``pull=False``: as last synced, for `note`, GH-113); never raises on a
    lost claim, that is one of the events."""
    if pull:
        ctx.coord.pull()
    events = resolve.worker_events(
        task_dir, claim_id, ctx.identity, timeutil.now(), ctx.settings.lease_s, ctx.human_names,
        control=L.machine_control(ctx.root, ctx.identity), idle_limit_s=ctx.settings.human_idle_s)
    return {"events": events, "plan_status": resolve.plan_status(task_dir, ctx.human_names),
            "open_question": resolve.open_question(task_dir),
            "open_tests": (resolve.test_scope_status(task_dir, ctx.human_names) or {}).get("status") == "pending"}


def still_working(ctx, task_dir: Path) -> None:
    claim_id = my_claim(ctx, task_dir)
    L.update_checkpoint(ctx, task_dir, claim_id, human_confirmed_utc=timeutil.iso(),
                        event={"kind": "still-working", "human": ctx.operator})


def release(ctx, task_dir: Path, reason: str = "released") -> None:
    claim_id = my_claim(ctx, task_dir)
    L.withdraw(ctx, task_dir, claim_id, reason)
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "ready")


def park(ctx, task_dir: Path, reason: str, human: str) -> None:
    """Give this machine's claim back and park the task in one ledger step. Unlike
    ``release`` it never sets the backend status to ``ready``: the task stays out of the
    queue until a human unparks it (GH-73)."""
    claim_id = my_claim(ctx, task_dir)
    L.withdraw(ctx, task_dir, claim_id, "parked-by-human", human=human, note=reason)


# ---------------------------------------------------------------------------
# test scope (GH-50): ask the human before the worker runs tests
# ---------------------------------------------------------------------------

def _base(ctx, task_dir: Path) -> str:
    return ctx.repo_config(str(resolve.read_meta(task_dir)["repo"])).get("base", "main")


def propose_tests(ctx, task_dir: Path, wt: Path) -> dict:
    """Map the worker's diff to tests and record the question. Asking again for the same
    set of changed files returns the open question instead of a new one."""
    claim_id = my_claim(ctx, task_dir)
    cp = L.read_checkpoint(task_dir)
    try:
        changed = testscope.changed_files(Path(wt), _base(ctx, task_dir))
    except RuntimeError as e:
        raise WorkError(f"can't read the diff against the base branch: {e}") from e
    status = resolve.test_scope_status(task_dir, ctx.human_names)
    if status and status["proposal"].get("diff_sha") == testscope.diff_sha(changed):
        return status
    proposal = testscope.propose(changed, Path(wt), cp.get("test_durations"))
    L.propose_tests(ctx, task_dir, claim_id, proposal)
    return resolve.test_scope_status(task_dir, ctx.human_names)


def answer_tests(ctx, task_dir: Path, scope: str, targeted_enough: bool = False, note_text: str = "") -> None:
    """A human answers the open question (Board or `swarm task answer-tests`)."""
    if scope not in testscope.SCOPES:
        raise WorkError(f"scope must be one of {', '.join(testscope.SCOPES)}")
    if targeted_enough and scope == "full":
        raise WorkError("'targeted is enough' only makes sense for a scope smaller than full")
    status = resolve.test_scope_status(task_dir, ctx.human_names)
    if not status:
        raise WorkError(f"{resolve.label(task_dir)} has no test question yet")
    L.answer_tests(ctx, task_dir, status["proposal_id"], scope, targeted_enough, note_text)


def accept_tests(ctx, task_dir: Path) -> str:
    """auto-pr only: the worker takes its own recommendation, as it may with a plan. Never
    lets ``done`` skip the full suite; only a human answer does."""
    my_claim(ctx, task_dir)
    if resolve.read_meta(task_dir).get("autonomy") != "auto-pr":
        raise WorkError(f"{resolve.label(task_dir)} is not auto-pr: a human has to answer the test question")
    status = resolve.test_scope_status(task_dir, ctx.human_names)
    if not status:
        raise WorkError("ask first: swarm-task test --propose")
    scope = status["proposal"]["recommendation"]
    L.answer_tests(ctx, task_dir, status["proposal_id"], scope, self_accepted=True)
    return scope


def run_scoped_tests(ctx, task_dir: Path, wt: Path, runner=subprocess.run) -> tuple[str, str]:
    """`swarm-task test`: run only the answered scope. Refuses before an answer. Returns
    (scope, output)."""
    my_claim(ctx, task_dir)
    status = resolve.test_scope_status(task_dir, ctx.human_names)
    if not status:
        raise WorkError("ask the human first: swarm-task test --propose")
    if status["status"] != "answered":
        raise WorkError("the test question hasn't been answered yet "
                        "(answer on the Swarm Board or with `swarm.py task answer-tests`)")
    scope = status["answer"]["scope"]
    files = testscope.scope_files(status["proposal"], scope)
    rcfg = ctx.repo_config(str(resolve.read_meta(task_dir)["repo"]))
    command = testscope.command_for(rcfg.get("test_command"), files, rcfg.get("test_scope_command"))
    if command is None:
        return scope, ""
    return scope, run_tests(command, Path(wt), runner)


def _done_scope(ctx, task_dir: Path, wt: Path) -> tuple[str | None, dict | None]:
    """The scope ``done`` may run instead of the full suite, with the answer behind it. Only a
    human's recorded "targeted is enough" counts, and only while the changed files are still the
    ones the answer was about."""
    status = resolve.test_scope_status(task_dir, ctx.human_names)
    answer = status and status["answer"]
    if not answer or not answer.get("targeted_enough") or answer.get("self_accepted"):
        return None, None
    try:
        current = testscope.diff_sha(testscope.changed_files(Path(wt), _base(ctx, task_dir)))
    except RuntimeError:
        return None, None
    if current != status["proposal"].get("diff_sha") or answer.get("scope") == "full":
        return None, None
    return answer["scope"], answer


# ---------------------------------------------------------------------------
# done — the output contract (Ch.7.3, Ch.4.1, Ch.9.1)
# ---------------------------------------------------------------------------

def _bullets(items) -> str:
    items = [str(i) for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else "None."


def render(ctx, task_dir: Path, cp: dict) -> tuple[str, str]:
    meta = resolve.read_meta(task_dir)
    values = {
        "task_ref": resolve.label(task_dir),
        "summary": (cp.get("summary") or "").strip(),
        "risks": _bullets(cp.get("risks")),
        "open_questions": _bullets(cp.get("open_questions")),
        "issue_ref": meta.get("issue_url") or meta.get("key"),
        "coordination_ref": task_dir.relative_to(ctx.root).as_posix(),
    }
    commit = (ctx.templates_dir / "commit-message.txt").read_text(encoding="utf-8").format(**values)
    body = (ctx.templates_dir / "pr-description.md").read_text(encoding="utf-8").format(**values)
    return commit, body


_DURATION_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s+(?:call|setup|teardown)\s+(\S+)", re.M)


def parse_durations(output: str, limit: int = 10) -> list[dict]:
    """The slowest tests from pytest's ``--durations`` report, slowest first (setup, call and
    teardown of one test are added up)."""
    totals: dict[str, float] = {}
    for secs, test in _DURATION_LINE.findall(output or ""):
        totals[test] = totals.get(test, 0.0) + float(secs)
    slowest = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    return [{"test": t, "seconds": round(s, 1)} for t, s in slowest]


def run_tests(command: str | None, wt: Path, runner=subprocess.run) -> str:
    """Run the repo's test command; returns its output (the durations come from it)."""
    if not command:
        return ""
    r = runner(command, shell=True, cwd=str(wt), capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise WorkError(f"tests failed (`{command}`), not opening a PR (plan §2.10):\n{out[-3000:]}")
    return out


def _await_checks(ctx, task_dir, claim_id, rcfg, url, token, say, sleep, clock) -> None:
    """Wait briefly for the PR's checks (GH-29). Red: keep the task, record why, raise.
    Pending after the timeout, or no checks at all: carry on and say so."""
    timeout = float(rcfg.get("checks_timeout", 300))
    found = gh.repo_from_pr_url(url)
    if timeout <= 0 or not found:
        return
    say(f"waiting up to {int(timeout)}s for the checks on {url} ...")
    try:
        summary, view = gh.wait_for_checks(*found, timeout, token=token, sleep=sleep, clock=clock)
    except (gh.GhError, FileNotFoundError, ValueError) as e:
        say(f"could not read the checks ({e}); finishing without them")
        return
    if summary == "failing":
        names = ", ".join(gh.failing_checks(view)) or "unknown"
        msg = f"checks failed on {url}: {names}"
        L.update_checkpoint(ctx, task_dir, claim_id, ci_failure=msg, append={"open_questions": [msg]})
        raise WorkError(f"{msg}. The task stays in progress: fix it, then run `done` again.")
    say({"passing": "checks passed",
         "pending": "checks are still running after the wait; finishing without a verdict",
         "no checks": "no checks configured on this PR; finishing as usual"}[summary])


CONFLICT_QUESTION = "Updating {branch} with origin/{base} conflicts in:"


def _block_conflict(ctx, task_dir: Path, wt: Path, text: str) -> None:
    block(ctx, task_dir, text)
    raise WorkError(f"{text}\nThe merge is left in progress; touch nothing and "
                    "`swarm-task wait --for answer`, then run `swarm-task done` again.")


def _check_resolution(ctx, task_dir: Path, wt: Path, base: str, branch: str, cp: dict, label: str) -> None:
    """Before anything is staged: a half-finished merge must not be committed with its markers."""
    prefix = CONFLICT_QUESTION.format(branch=branch, base=base)
    resuming = str(cp.get("needs_human") or "").startswith(prefix)
    problems = []
    if worktree.merge_in_progress(wt):
        problems.append("a merge is still in progress (git commit it)")
    unmerged = worktree.unmerged_paths(wt)
    if unmerged:
        problems.append("unmerged paths: " + ", ".join(unmerged))
    if resuming:
        marked = worktree.conflict_markers(wt, base)
        if marked:
            problems.append("conflict markers committed in: " + ", ".join(marked))
    if problems:
        _block_conflict(ctx, task_dir, wt, f"{prefix} the resolution isn't finished: "
                        + "; ".join(problems) + f".\nIn {wt}: resolve, git add, git commit; "
                        f'then: swarm.py task answer {label} "resolved"')


def _bring_up_to_date(ctx, task_dir: Path, wt: Path, repo: str, base: str, branch: str, label: str) -> None:
    """Fetch the base and bring the branch up to date with it before the tests (GH-91).
    First push: rebase. PR already open: merge, never a force-push. A conflict is left as a
    merge in progress and handed to a human with `block`; the re-run checks the resolution."""
    prefix = CONFLICT_QUESTION.format(branch=branch, base=base)
    worktree.fetch_base(wt, base)
    if worktree.behind(wt, base) == 0:
        return
    try:
        published = (resolve.last_submitted_commit(task_dir) is not None
                     or worktree.remote_has_branch(wt, branch)
                     or gh.pr_for_branch(repo, branch) is not None)
    except Exception:  # noqa: BLE001 - can't tell: assume published, merge is the safe direction
        published = True
    try:
        files = worktree.update_to_base(wt, base, rebase=not published)
    except RuntimeError as e:
        raise WorkError(f"{label}: {e}") from e
    if files:
        _block_conflict(ctx, task_dir, wt, f"{prefix} " + ", ".join(files)
                        + f".\nIn {wt}: resolve the files, git add, git commit; "
                        f'then: swarm.py task answer {label} "resolved" (or y on the Board)')


def finish(ctx, task_dir: Path, wt: Path, *, skip_tests: bool = False, test_runner=subprocess.run,
           say=lambda text: None, wait_sleep=None, wait_clock=None) -> str:
    from dags.gitsync import git
    wt = Path(wt)
    flush_note_queue(ctx)
    ctx.coord.pull()
    claim_id = my_claim(ctx, task_dir)
    meta = resolve.read_meta(task_dir)
    label = resolve.label(task_dir)
    if meta.get("autonomy") == "human-must-scope":
        cp0 = L.read_checkpoint(task_dir)
        if not workers.WORKERS.get(cp0.get("worker") or "", workers.WORKERS["claude"]).human:
            raise WorkError(f"{label} is human-must-scope; an AI worker may not open its PR")
    if resolve.plan_status(task_dir, ctx.human_names) != "approved":
        raise WorkError(f"{label}: the plan isn't approved yet (run `swarm-task plan` and get it reviewed)")
    cp = L.read_checkpoint(task_dir)
    if not (cp.get("summary") or "").strip():
        raise WorkError('record a summary first: swarm-task note --summary "..."')
    repo = str(meta["repo"])
    rcfg = ctx.repo_config(repo)
    base = rcfg.get("base", "main")
    branch = worktree.branch_name(task_dir)
    durations: list[dict] = []
    scoped_note = ""
    commit_msg, body = render(ctx, task_dir, cp)
    _check_resolution(ctx, task_dir, wt, base, branch, cp, label)
    git(["add", "-A"], wt)
    if git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
        cmd = ["commit", "-q", "-F", "-"]
        bot = ctx.bot_identity()
        if bot:
            cmd.insert(1, f"--author={bot[0]} <{bot[1]}>")
        git(cmd, wt, input=commit_msg)
    _bring_up_to_date(ctx, task_dir, wt, repo, base, branch, label)
    if worktree.commits_ahead(wt, base) == 0:
        raise WorkError(f"{label}: no changes to submit on {branch}")
    if not skip_tests:
        scope, answer = _done_scope(ctx, task_dir, wt)
        if scope:
            files = testscope.scope_files(resolve.test_scope_status(task_dir, ctx.human_names)["proposal"], scope)
            command = testscope.command_for(rcfg.get("test_command"), files, rcfg.get("test_scope_command"))
            if command:
                run_tests(command, wt, test_runner)             # partial run: no durations to record
            ran = f"{len(files)} test file(s)" if files else "no tests"
            scoped_note = (f"\n## Tests\nThe full suite did not run: {answer.get('human')} approved the "
                           f"'{scope}' scope ({ran}) as enough.\n")
        else:
            durations = parse_durations(run_tests(rcfg.get("test_command"), wt, test_runner))
    body += scoped_note
    submitted = resolve.last_submitted_commit(task_dir)
    if submitted and worktree.head(wt) == submitted:
        raise WorkError(f"{label}: nothing new since the PR was opened ({submitted[:7]}); "
                        "address the feedback first")

    L.require_mine(ctx, task_dir, claim_id)              # last check before the irreversible steps
    git(["push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}"], wt)
    token = ctx.worker_token()
    existing = gh.pr_for_branch(repo, branch)
    if existing and existing.get("state") == "OPEN":
        url = existing["url"]
        gh.gh(["pr", "comment", str(existing["number"]), "--repo", repo, "--body-file", "-"],
              token=token, input="Updated by the swarm worker.\n\n" + body)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body)
            body_file = f.name
        try:
            out = gh.gh(["pr", "create", "--repo", repo, "--base", base, "--head", branch,
                         "--title", f"[{label}] {meta.get('title') or label}", "--body-file", body_file],
                        token=token)
        finally:
            Path(body_file).unlink(missing_ok=True)
        url = next((ln.strip() for ln in out.splitlines() if "/pull/" in ln), out.strip())

    _await_checks(ctx, task_dir, claim_id, rcfg, url, token, say, wait_sleep, wait_clock)
    worker_label = cp.get("worker_label") or cp.get("worker") or "worker"
    L.update_checkpoint(ctx, task_dir, claim_id, pr_url=url, ci_failure=None, finished_utc=timeutil.iso(), needs_human=None,
                       **({"test_durations": durations} if durations else {}))
    L.complete(ctx, task_dir, "pr-opened", claim_id=claim_id, pr_url=url, worker=worker_label,
               commit=worktree.head(wt))
    _try_backend(ctx, ctx.backend.set_status, _ref(task_dir), "awaiting-review")
    _try_backend(ctx, ctx.backend.post_comment, _ref(task_dir), f"{body}\nPR: {url}\n")
    return url

