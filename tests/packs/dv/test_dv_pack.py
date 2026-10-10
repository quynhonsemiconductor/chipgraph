"""M2-06: the built-in `dv` pack: discovered, its `tb_module` rule, its skill."""

from __future__ import annotations

import pytest

from chipgraph.app.build import builtin_packs_dir
from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.rules import load_pack_rules
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.runtime.roles import (
    RoleError,
    SkillError,
    check_agent_rules,
    get_role,
    load_skills,
)


@pytest.fixture(scope="module")
def pack() -> Pack:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    return discover_packs([packs_dir])["dv"]


@pytest.fixture(scope="module")
def rule(pack: Pack) -> RuleSpec:
    [rule] = load_pack_rules(pack)
    return rule


def test_pack_is_discovered_among_builtins(pack: Pack) -> None:
    assert pack.manifest.version == "0.1.0"
    assert pack.root.name == "dv"
    assert pack.manifest.provides.rules == ("rules",)
    assert pack.manifest.provides.skills == ("skills/dv",)


def test_tb_module_rule(rule: RuleSpec) -> None:
    assert rule.id == "dv/tb_module"
    assert (rule.kind, rule.role, rule.foreach) == ("agent", "tb-author", "blocks")
    assert [(i.source, i.selector) for i in rule.inputs] == [
        ("model", "block/{block}"),
        ("model", "interface/{block}"),
    ]
    assert rule.outputs == ("dv/{block}/test_{block}.py",)
    assert rule.checks == ("tb_static",)
    assert rule.skills == ("dv/cocotb",)
    assert rule.budget.tries == 3
    assert rule.budget.model_fields_set == {"tries"}  # the tiers are the role's


def test_the_rule_gives_the_tb_author_no_rtl_input(rule: RuleSpec) -> None:
    graph = build_graph([rule], StaticForeach({"blocks": [{"block": "gpio"}, {"block": "x"}]}))
    check_agent_rules(graph.rules, graph.instances.values())
    assert sorted(graph.instances) == ["dv/tb_module[block=gpio]", "dv/tb_module[block=x]"]
    denied = set(get_role("tb-author").read_policy.deny_kinds)
    assert denied == {"rtl"}
    for instance in graph.instances.values():
        assert all(ref.kind not in denied and ref.path is None for ref in instance.inputs)
        assert [ref.model_key for ref in instance.inputs] == [
            f"block/{instance.params['block']}",
            f"interface/{instance.params['block']}",
        ]


def test_the_validator_refuses_an_rtl_input_added_to_the_rule(rule: RuleSpec) -> None:
    bad = rule.model_copy(
        update={"inputs": (*rule.inputs, *RuleSpec.model_validate(_with_rtl()).inputs)}
    )
    graph = build_graph([bad], StaticForeach({"blocks": [{"block": "gpio"}]}))
    with pytest.raises(RoleError, match=r"'tb-author' must not see 'rtl'.*rtl/tiny_gpio\.sv"):
        check_agent_rules(graph.rules, graph.instances.values())


def _with_rtl() -> dict[str, object]:
    return {
        "id": "x/y",
        "kind": "agent",
        "role": "tb-author",
        "inputs": [{"path": "rtl/tiny_{block}.sv"}],
        "outputs": ["o"],
    }


def test_cocotb_skill_is_for_the_tb_author_only(pack: Pack) -> None:
    skills = load_skills([pack])
    [skill] = skills.resolve(["dv/cocotb"], "tb-author")
    assert skill.roles == ("tb-author",) and skill.version == "0.1.0"
    with pytest.raises(SkillError, match="not for role 'author'"):
        skills.resolve(["dv/cocotb"], "author")


def test_cocotb_skill_says_what_the_brief_asks(pack: Pack) -> None:
    text = load_skills([pack]).get("dv/cocotb").text
    for needle in (
        "interface.ports",
        "interface.clock",
        "register map",
        "# verifies: REQ-",
        "One `@cocotb.test()`",
        "quotes the spec value",
        "no `open`",
        "no internal signal",
        "needs_human",
        "fixed seed",
        "deterministic",
    ):
        assert needle in text, needle
