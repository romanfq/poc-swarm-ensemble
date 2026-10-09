"""GH-114: `auto-pr` is now `self-approve`. Old names are read everywhere and never written."""
import pytest

import autonomy
import resolve as rv
from backends.base import AUTONOMY_TIERS, TaskRef, parse_labels
from dags import ledger as L
from dags import work


def test_canonical_maps_the_old_name_and_rejects_unknown_values():
    assert autonomy.canonical("auto-pr") == "self-approve"
    for tier in AUTONOMY_TIERS:
        assert autonomy.canonical(tier) == tier
    with pytest.raises(ValueError, match="autonomy must be one of"):
        autonomy.canonical("whatever")
    assert autonomy.is_self_approve("auto-pr") and autonomy.is_self_approve("self-approve")
    assert not autonomy.is_self_approve("human-must-review") and not autonomy.is_self_approve(None)
    assert "auto-pr" not in AUTONOMY_TIERS


def test_an_issue_with_only_the_old_label_reads_as_self_approve():
    assert parse_labels(["swarm:autonomy:auto-pr"])["autonomy"] == "self-approve"
    assert parse_labels(["swarm:autonomy:self-approve"])["autonomy"] == "self-approve"
    assert parse_labels(["swarm:autonomy:bogus"])["autonomy"] is None


def _old_world_task(world):
    """A task whose issue carries the old label and whose ledger meta says `auto-pr`."""
    world.backend.add("T9", title="x", labels=["repo:OWNER/app", "swarm:autonomy:auto-pr"])
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T9"]
    L.set_autonomy(a, d, "auto-pr", "old record", human="roman")      # a pre-rename meta revision
    assert rv.read_meta(d)["autonomy"] == "auto-pr"
    return a, d


def test_ledger_meta_saying_auto_pr_behaves_as_self_approve(world):
    a, d = _old_world_task(world)
    assert work.submit_plan(a, d, "plan") == "approved"
    work.block(a, d, "store cancelled matches?")                       # a plan question demotes it
    assert rv.read_meta(d)["autonomy"] == "human-must-review"
    assert world.backend.get_task(TaskRef("T9")).autonomy == "human-must-review"


def test_set_autonomy_with_the_old_name_writes_the_new_one(world):
    from dags import actions
    world.backend.add("T9", title="x", labels=["repo:OWNER/app", "swarm:autonomy:human-must-review"])
    a = world.machine("mac-a")
    world.scheduler(a, worker="claude").cycle()
    d = rv.index(a.root)["T9"]
    out = actions.set_autonomy(a, d, "auto-pr", "scoped")
    assert out.endswith("human-must-review -> self-approve") and "deprecat" not in out
    assert rv.read_meta(d)["autonomy"] == "self-approve"
    assert world.backend.get_task(TaskRef("T9")).autonomy == "self-approve"
    assert "swarm:autonomy:self-approve" in world.backend.get_task(TaskRef("T9")).labels
