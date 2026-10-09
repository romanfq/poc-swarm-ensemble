"""Markdown <-> ADF converters (GH-123)."""
import re

from dags.adf import to_adf, to_markdown

PLAN_RE = re.compile(r"<!-- dags-(?:plan|block): [^>]*-->")


def test_empty_input_is_a_document():
    assert to_adf("")["type"] == "doc" and to_markdown(to_adf("")) == ""


def test_swarm_shapes_round_trip():
    md = ("# Plan\n\n## Approach\n\nChange `foo` and **bar** per [the spec](https://x.test/a).\n\n"
          "- one\n- two\n\n1. first\n2. second\n\n```python\nprint(1)\nprint(2)\n```")
    assert to_markdown(to_adf(md)) == md


def test_markers_survive_a_round_trip():
    md = "<!-- dags-plan: abc123def456 -->\n## Plan\nbody"
    out = to_markdown(to_adf(md))
    assert PLAN_RE.search(out) and "abc123def456" in out


def test_seed_marker_survives():
    out = to_markdown(to_adf("text\n\n<!-- dags-seed: T1 -->"))
    assert re.search(r"<!-- dags-seed: T1 -->", out)


def test_line_breaks_inside_a_paragraph_become_hard_breaks():
    doc = to_adf("a\nb")
    assert {"type": "hardBreak"} in doc["content"][0]["content"]


def test_unknown_nodes_degrade_to_text_and_never_raise():
    doc = {"type": "doc", "content": [
        {"type": "panel", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "careful"}]}]},
        {"type": "somethingNew", "content": [{"type": "text", "text": "kept"}]},
        {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@roman"}},
                                          {"type": "weird", "text": "!"}]},
        {"type": "mediaSingle"}]}
    out = to_markdown(doc)
    assert "careful" in out and "kept" in out and "@roman" in out
    assert to_markdown({"nonsense": 1}) == "" and to_markdown(None) == "" and to_markdown("plain") == "plain"


def test_tables_and_quotes_read_as_text():
    doc = {"type": "doc", "content": [
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]},
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "b"}]}]}]}]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "q"}]}]}]}
    assert to_markdown(doc) == "a | b\n\n> q"
