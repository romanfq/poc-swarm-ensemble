"""Filing an issue from a draft (GH-83): the format, validation, and the write path."""
import pytest

from backends.github import GitHubBackend
from conftest import FakeGh
from dags import draft, gh
from fakes import FakeGitHub

TEXT = """---
labels: [next-version]
autonomy: human-must-review
repo: OWNER/app
epic: 1
depends_on: [4]
status: blocked          # optional
---
# The quota line runs into the bar

Body as usual.

## Detail

- one
"""


@pytest.fixture
def tracker():
    fake = FakeGitHub(repo="acme/plan")
    fake.seed()
    fake.labels.update({"next-version", "swarm:autonomy:human-must-review", "swarm:status:blocked",
                        "repo:OWNER/app"})
    gh.set_runner(FakeGh(fake))
    yield GitHubBackend("acme/plan", cache_seconds=0), fake
    gh.set_runner(None)


def test_parse_takes_title_from_h1_and_keeps_body_verbatim():
    d = draft.parse(TEXT)
    assert d.title == "The quota line runs into the bar"
    assert d.body == "Body as usual.\n\n## Detail\n\n- one\n"
    assert (d.labels, d.autonomy, d.repo, d.epic, d.depends_on, d.status) == (
        ["next-version"], "human-must-review", "OWNER/app", "1", ["4"], "blocked")


def test_render_round_trips():
    d = draft.parse(TEXT)
    assert draft.parse(draft.render(d)) == d
    stamped = draft.parse(draft.render(draft.Draft(**{**d.__dict__, "url": "https://x/issues/9"})))
    assert stamped.url == "https://x/issues/9" and stamped.body == d.body


def test_draft_without_frontmatter_or_h1():
    assert draft.parse("# Just a title\n\nbody").epic is None
    with pytest.raises(draft.DraftError, match="H1"):
        draft.parse("---\nepic: 1\n---\nno title here\n")
    with pytest.raises(draft.DraftError, match="unknown frontmatter"):
        draft.parse("---\nready: true\n---\n# T\n")


def test_looks_like_draft():
    assert draft.looks_like_draft(TEXT) and draft.looks_like_draft("# T\nbody")
    assert not draft.looks_like_draft("just some text") and not draft.looks_like_draft("---\n[\n---\n# T")


def test_resolved_labels_maps_autonomy_repo_status(tracker):
    b, _ = tracker
    d = draft.parse(TEXT)
    assert draft.resolved_labels(b, d) == [
        "next-version", "swarm:status:blocked", "swarm:autonomy:human-must-review", "repo:OWNER/app", "type:task"]
    d.status = None
    d.autonomy = None
    assert "swarm:autonomy:human-must-review" in draft.resolved_labels(b, d)


def test_valid_draft_has_no_problems(tracker):
    b, _ = tracker
    assert draft.validate(draft.parse(TEXT), b, ["OWNER/app"]) == []


def test_every_problem_is_listed_against_its_field(tracker):
    b, fake = tracker
    d = draft.parse("---\nlabels: [nope, swarm:status:ready]\nautonomy: yolo\nrepo: OWNER/other\n"
                    "epic: 4\ndepends_on: [1, 99]\nstatus: ready\n---\n# T\n")
    fields = [p.field for p in draft.validate(d, b, ["OWNER/app"])]
    assert sorted(set(fields)) == ["autonomy", "depends_on", "epic", "labels", "repo", "status"]
    assert fields.count("labels") == 2 and fields.count("depends_on") == 2
    assert fake.issues[4]["labels"] and len(fake.issues) == 8          # nothing written


def test_epic_is_required_and_status_ready_is_refused(tracker):
    b, _ = tracker
    d = draft.parse(TEXT)
    d.epic = None
    d.status = "ready"
    got = {p.field: p.message for p in draft.validate(d, b, ["OWNER/app"])}
    assert "missing" in got["epic"] and "never sets an issue ready" in got["status"]


def test_missing_swarm_label_points_at_backend_init(tracker):
    b, fake = tracker
    fake.labels.discard("repo:OWNER/app")
    probs = draft.validate(draft.parse(TEXT), b, ["OWNER/app"])
    assert [p.field for p in probs] == ["labels"] and "backend init" in probs[0].message


def test_apply_creates_links_and_never_sets_ready(tracker):
    b, fake = tracker
    d = draft.parse(TEXT)
    f = draft.plan_filing(d, b)
    assert f.lines(b.short_key) == [
        "create  task: The quota line runs into the bar  "
        "[next-version, swarm:status:blocked, swarm:autonomy:human-must-review, repo:OWNER/app, type:task]",
        "parent  (new) -> epic GH-1", "depends (new) blocked by GH-4"]
    ref = draft.apply(f, b, echo=lambda _: None)
    issue = fake.issues[int(ref.key.split("#")[1])]
    assert issue["parent"] == 1 and issue["blockedBy"] == [4] and issue["body"] == d.body
    assert "swarm:status:ready" not in issue["labels"]


def test_resume_after_a_failure_adds_only_the_missing_links(tracker, tmp_path):
    b, fake = tracker
    d = draft.parse(TEXT)
    ref = b.create_issue(d.title, d.body, draft.resolved_labels(b, d))
    b.set_parent(ref, b.parse_ref("1"))
    d.url = f"https://github.com/acme/plan/issues/{ref.key.split('#')[1]}"
    f = draft.plan_filing(d, b)
    assert not f.missing_epic and [r.key for r in f.missing_deps] == ["acme/plan#4"]
    before = len(fake.issues)
    draft.apply(f, b, echo=lambda _: None)
    assert len(fake.issues) == before and fake.issues[int(ref.key.split("#")[1])]["blockedBy"] == [4]


def test_move_to_filed_stamps_the_url_and_refuses_a_second_move(tmp_path):
    (tmp_path / "drafts").mkdir()
    src = tmp_path / "drafts" / "a.md"
    src.write_text(TEXT)
    d = draft.load(src)
    dest = draft.move_to_filed(src, d, "https://x/issues/9", tmp_path)
    assert not src.exists() and dest == tmp_path / "drafts" / "filed" / "a.md"
    assert draft.load(dest).url == "https://x/issues/9" and draft.is_filed(dest)
    src.write_text(TEXT)
    with pytest.raises(draft.DraftError, match="already exists"):
        draft.move_to_filed(src, d, "https://x/issues/10", tmp_path)


def test_issue_creation_uses_the_operators_login_not_the_bot_token(tracker):
    b, _ = tracker
    runner = gh._runner
    draft.apply(draft.plan_filing(draft.parse(TEXT), b), b, echo=lambda _: None)
    assert runner.calls and all(c["token"] is None for c in runner.calls)
