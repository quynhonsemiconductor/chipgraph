"""M2-09: the built-in `digital-rtl` pack: discovered, its review rule, its skill."""

from __future__ import annotations

import pytest

from chipgraph.app.build import builtin_packs_dir
from chipgraph.core.engine.rules import load_pack_rules
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.runtime.roles import SkillError, load_skills


@pytest.fixture(scope="module")
def pack() -> Pack:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    return discover_packs([packs_dir])["digital-rtl"]


def test_pack_is_discovered_among_builtins(pack: Pack) -> None:
    assert pack.manifest.version == "0.1.0"
    assert pack.root.name == "digital_rtl"
    provides = pack.manifest.provides
    assert provides.rules == ("rules",) and provides.skills == ("skills",)
    for field in ("agents", "checks", "workflows", "templates", "commands", "schemas"):
        assert getattr(provides, field) == (), field


def test_review_rule(pack: Pack) -> None:
    [rule] = [r for r in load_pack_rules(pack) if r.id == "digital-rtl/review"]
    assert rule.id == "digital-rtl/review"
    assert (rule.kind, rule.role, rule.foreach) == ("agent", "critic", "blocks")
    assert rule.outputs == ("reports/review/{block}.json",)
    assert [(i.source, i.selector) for i in rule.inputs] == [("model", "block/{block}")]
    assert rule.skills == ("review/diff",)
    assert rule.checks == ()
    # The tier is the role's (large): the budget sets only the tries.
    assert rule.budget.model_fields_set == {"tries"}


def test_review_skill_is_for_the_critic_only(pack: Pack) -> None:
    skills = load_skills([pack])
    [skill] = skills.resolve(["review/diff"], "critic")
    assert skill.roles == ("critic",)
    for needle in ("evidence", "no comments", "formatter", "invent"):
        assert needle in skill.text, needle
    with pytest.raises(SkillError, match="not for role 'author'"):
        skills.resolve(["review/diff"], "author")


def test_skill_names_every_category_the_evals_use(pack: Pack) -> None:
    text = load_skills([pack]).get("review/diff").text
    for category in (
        "spec_mismatch",
        "missing_req",
        "reset",
        "width",
        "counter",
        "register_map",
        "bit_order",
        "latch",
        "cdc",
        "hard_coded",
        "naming",
        "test_gap",
        "other",
    ):
        assert f"`{category}`" in text, category
