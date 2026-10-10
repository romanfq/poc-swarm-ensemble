"""How the worker responds to a PR (GH-6): what makes the swarm act, and what the worker reads.

Everything here is a pure function of PR data, the recognised humans, the time of the worker's
last push and the ids already handled, so it needs no ``gh``. A PR is treated like any PR:

* a recognised human *submitting* a review that requests changes, or a review or comment with
  words addressed to the worker, reopens the task. An approval never does; neither does an
  inline comment on its own (a review with an empty body), or anyone else's text, or the bot's;
* the worker is given that text as written, then the open inline conversations. No tags.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from dags import timeutil

Login = Callable[[str | None], object]          # truthy for a recognised human's GitHub login


@dataclass(frozen=True)
class Item:
    id: str
    kind: str                # review | comment
    author: str | None
    state: str               # the review state, or COMMENT
    body: str
    at: str | None


def _login(raw: dict) -> str | None:
    return (raw.get("author") or {}).get("login")


def review_id(raw: dict) -> str:
    return str(raw.get("id") or f"{raw.get('submittedAt')}-{_login(raw)}")


def comment_id(raw: dict) -> str:
    return "C-" + str(raw.get("id") or f"{raw.get('createdAt')}-{_login(raw)}")


def _items(pr: dict) -> list[Item]:
    out = [Item(review_id(r), "review", _login(r), str(r.get("state") or ""), (r.get("body") or "").strip(),
                r.get("submittedAt")) for r in pr.get("reviews") or []]
    out += [Item(comment_id(c), "comment", _login(c), "COMMENT", (c.get("body") or "").strip(),
                 c.get("createdAt")) for c in pr.get("comments") or []]
    return out


def is_bot(login: str | None, bot: str | None) -> bool:
    return bool(login and bot and login.lower() == bot.lower())


def _after(at: str | None, since) -> bool:
    """True unless both times are readable and ``at`` is not later than ``since``."""
    then, floor = timeutil.parse(at), timeutil.parse(since)
    return then is None or floor is None or then > floor


def addressed_to_worker(item: Item) -> bool:
    if item.kind == "comment":
        return bool(item.body)
    if item.state == "CHANGES_REQUESTED":
        return True                                       # whatever its text
    return item.state == "COMMENTED" and bool(item.body)  # approvals and inline-only reviews: no


def triggers(pr: dict, is_human: Login, bot: str | None = None, since=None,
             handled: set[str] | frozenset[str] = frozenset()) -> list[Item]:
    """The items that should reopen the task, oldest first. Only a recognised human can cause work."""
    found = [i for i in _items(pr)
             if i.id not in handled and addressed_to_worker(i) and not is_bot(i.author, bot)
             and is_human(i.author) and _after(i.at, since)]
    return sorted(found, key=lambda i: (timeutil.parse(i.at) is None, str(timeutil.parse(i.at) or ""), i.id))


def items_by_id(pr: dict, ids: list[str]) -> list[Item]:
    """The triggering items, as they stand on the PR now (an id that has gone is left out)."""
    wanted = {str(i) for i in ids}
    return [i for i in _items(pr) if i.id in wanted]


def _who(login: str | None) -> str:
    return f"@{login}" if login else "(unknown)"


def conversation(items: list[Item], threads: list[dict], is_human: Login, bot: str | None = None) -> str:
    """Plain text for the worker: what triggered this round, then the open inline threads.
    Threads are context, not orders; only recognised humans' and the bot's own comments are shown."""
    lines: list[str] = []
    if items:
        lines.append("What triggered this round (your instructions come from this text):\n")
        for i in items:
            label = "comment" if i.kind == "comment" else f"review, {i.state.replace('_', ' ').lower()}"
            lines.append(f"{_who(i.author)} ({label}, {i.at or 'time unknown'}):")
            lines.append(i.body or "(no text)")
            lines.append("")
    open_threads = [t for t in threads if not t.get("resolved")]
    shown = []
    for t in open_threads:
        comments = [c for c in t.get("comments") or [] if is_human(c.get("author")) or is_bot(c.get("author"), bot)]
        if comments:
            shown.append((t, comments))
    if shown:
        lines.append("Open conversations on the code (context: act on one only if the text above points to it;\n"
                     "answer in a thread with `swarm-task reply THREAD_ID \"text\"`):\n")
        for t, comments in shown:
            where = f"{t.get('path') or '?'}" + (f":{t['line']}" if t.get("line") else "")
            lines.append(f"Thread {t.get('id')}  {where}" + ("  (outdated: the code has changed)" if t.get("outdated") else ""))
            if t.get("hunk"):
                lines.append(t["hunk"].rstrip())
            for c in comments:
                lines.append(f"{_who(c.get('author'))}: {(c.get('body') or '').strip()}")
            lines.append("")
    return "\n".join(lines).rstrip()


def counts(pr: dict, threads: list[dict] | None) -> dict:
    """What the Board shows for a PR: comments on it, and the inline threads still open."""
    conversation_comments = len(pr.get("comments") or [])
    if threads is None:
        return {"comments": conversation_comments, "threads": None}
    inline = sum(len(t.get("comments") or []) for t in threads)
    return {"comments": conversation_comments + inline,
            "threads": sum(1 for t in threads if not t.get("resolved"))}


def live_counts(repo: str, number: str | int, pr: dict) -> dict:
    """``counts`` with the threads read from GitHub; the thread count is None when that read fails."""
    from dags import gh
    try:
        threads = gh.review_threads(repo, number)
    except (gh.GhError, FileNotFoundError, ValueError):
        threads = None
    return counts(pr, threads)
