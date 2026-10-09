"""Markdown subset <-> Atlassian Document Format (GH-123), stdlib only.

Jira Cloud REST v3 reads and writes descriptions and comments as ADF, a JSON tree. The swarm works in
markdown text. ``to_adf`` covers what the swarm emits: paragraphs, ``#`` headings, bullet and numbered
lists, fenced code, inline code, links and bold. ``to_markdown`` reads everything else Jira may send;
an unknown node degrades to its text and never raises.

Markers such as ``<!-- dags-plan: sha -->`` are HTML comments, which ADF cannot carry. They are written
as inline code (``<!-- ... -->`` inside backticks), so the regexes in work.py and seed.py still find them
after a round trip.
"""
from __future__ import annotations

import re

_INLINE = re.compile(r"`(?P<code>[^`\n]+)`|\*\*(?P<bold>[^*\n]+)\*\*|\[(?P<text>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
                     r"|(?P<marker><!--.*?-->)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^```\s*([\w+-]*)\s*$")


def _text(s: str, marks: list[dict] | None = None) -> dict:
    node = {"type": "text", "text": s}
    if marks:
        node["marks"] = marks
    return node


def _inline(s: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            out.append(_text(s[pos:m.start()]))
        if m.group("code") is not None:
            out.append(_text(m.group("code"), [{"type": "code"}]))
        elif m.group("bold") is not None:
            out.append(_text(m.group("bold"), [{"type": "strong"}]))
        elif m.group("marker") is not None:
            out.append(_text(m.group("marker"), [{"type": "code"}]))
        else:
            out.append(_text(m.group("text"), [{"type": "link", "attrs": {"href": m.group("href")}}]))
        pos = m.end()
    if pos < len(s):
        out.append(_text(s[pos:]))
    return out


def _lines_inline(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for i, line in enumerate(lines):
        if i:
            out.append({"type": "hardBreak"})
        out += _inline(line)
    return out


def _list(kind: str, items: list[str]) -> dict:
    return {"type": kind, "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(i) or [_text(" ")]}]}
        for i in items]}


def to_adf(markdown: str) -> dict:
    """A document node for ``markdown`` (the subset above). Empty input gives a document with one empty paragraph."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            blocks.append({"type": "paragraph", "content": _lines_inline(para)})
            para.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            lang, body = fence.group(1), []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]) and lines[i].strip() != "```":
                body.append(lines[i])
                i += 1
            node: dict = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            if body:
                node["content"] = [_text("\n".join(body))]
            blocks.append(node)
            i += 1
            continue
        h = _HEADING.match(line)
        if h:
            flush()
            blocks.append({"type": "heading", "attrs": {"level": len(h.group(1))}, "content": _inline(h.group(2))})
            i += 1
            continue
        for pattern, kind in ((_BULLET, "bulletList"), (_NUMBER, "orderedList")):
            if pattern.match(line):
                flush()
                items = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append(pattern.match(lines[i]).group(1))
                    i += 1
                blocks.append(_list(kind, items))
                break
        else:
            if not line.strip():
                flush()
            else:
                para.append(line)
            i += 1
    flush()
    return {"version": 1, "type": "doc", "content": blocks or [{"type": "paragraph", "content": []}]}


# -- ADF -> markdown -----------------------------------------------------------------------------------
def _marked(node: dict) -> str:
    text = node.get("text") or ""
    for mark in node.get("marks") or []:
        kind = mark.get("type")
        if kind == "code":
            text = f"`{text}`"
        elif kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "link":
            text = f"[{text}]({(mark.get('attrs') or {}).get('href', '')})"
    return text


def _inline_md(nodes: list[dict]) -> str:
    out = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            out.append(_marked(n))
        elif t == "hardBreak":
            out.append("\n")
        elif t == "mention":
            out.append((n.get("attrs") or {}).get("text") or "@someone")
        elif t == "emoji":
            out.append((n.get("attrs") or {}).get("text") or (n.get("attrs") or {}).get("shortName") or "")
        elif t in ("inlineCard", "blockCard", "embedCard"):
            out.append((n.get("attrs") or {}).get("url") or "")
        elif n.get("content"):
            out.append(_inline_md(n["content"]))
        elif isinstance(n.get("text"), str):
            out.append(n["text"])
    return "".join(out)


def _block_md(node: dict, depth: int = 0) -> str:
    t = node.get("type")
    kids = node.get("content") or []
    if t == "paragraph":
        return _inline_md(kids)
    if t == "heading":
        return "#" * int((node.get("attrs") or {}).get("level", 1)) + " " + _inline_md(kids)
    if t == "codeBlock":
        lang = (node.get("attrs") or {}).get("language") or ""
        return f"```{lang}\n{_inline_md(kids)}\n```"
    if t in ("bulletList", "orderedList"):
        rows = []
        for n, item in enumerate(kids, 1):
            marker = "-" if t == "bulletList" else f"{n}."
            body = _join(item.get("content") or [], depth + 1).replace("\n", "\n" + "  " * (depth + 1))
            rows.append(f"{marker} {body}")
        return "\n".join(rows)
    if t == "listItem":
        return _join(kids, depth)
    if t == "blockquote":
        return "\n".join("> " + line for line in _join(kids, depth).split("\n"))
    if t == "rule":
        return "---"
    if t == "table":
        rows = [" | ".join(_join(c.get("content") or [], depth).replace("\n", " ") for c in r.get("content") or [])
                for r in kids]
        return "\n".join(rows)
    if t in ("mediaSingle", "mediaGroup", "media"):
        return ""
    if kids:
        return _join(kids, depth)              # panel, expand, unknown containers: their text
    return _inline_md([node]) if t == "text" or "text" in node else ""


def _join(nodes: list[dict], depth: int = 0) -> str:
    parts = [_block_md(n, depth) for n in nodes or []]
    return "\n\n".join(p for p in parts if p != "")


def to_markdown(adf) -> str:
    """Markdown for an ADF document. A plain string (older payloads) is returned as is."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    try:
        return _join(adf.get("content") or [] if isinstance(adf, dict) else [])
    except (AttributeError, TypeError, ValueError):
        return ""
