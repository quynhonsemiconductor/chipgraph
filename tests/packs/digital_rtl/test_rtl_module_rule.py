"""M2-04: the rule `digital-rtl/rtl_module` and its skills, as the pack ships them."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.app.build import builtin_packs_dir
from chipgraph.checks._naming_rules import NamingRules
from chipgraph.checks.naming import _load_rules
from chipgraph.core.contracts import ArtifactRef, RuleInstance
from chipgraph.core.engine.graph import ForeachEntry, build_graph
from chipgraph.core.engine.rules import load_pack_rules
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.runtime.roles import RoleError, SkillError, check_agent_rules, load_skills
from chipgraph.packs.digital_rtl.rtl.context import naming_text

RULE = "digital-rtl/rtl_module"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def pack() -> Pack:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    return discover_packs([packs_dir])["digital-rtl"]


class _Modules:
    """`blocks` and one approved module, as the project's resolver gives them."""

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [dict(e.params) for e in self.expand_entries(expr)]

    def expand_entries(self, expr: str) -> list[ForeachEntry]:
        if expr == "blocks":
            return [ForeachEntry(params={"block": "pwm"})]
        if expr.startswith("wrappers"):
            return []
        return [
            ForeachEntry(
                params={"block": "pwm", "module": "tiny_pwm"},
                inputs=("plan/pwm.approved.json",),
                writes=frozenset({"rtl/tiny_pwm.sv"}),
            )
        ]


def test_the_rule_loads_with_its_role_inputs_checks_and_budget(pack: Pack) -> None:
    rules = {r.id: r for r in load_pack_rules(pack)}
    rule = rules[RULE]
    assert (rule.kind, rule.role, rule.foreach) == ("agent", "rtl-author", "plan.modules")
    assert [(i.source, i.selector) for i in rule.inputs] == [
        ("model", "block/{block}"),
        ("model", "interface/{module}"),
    ]
    assert rule.outputs == ("rtl/{module}.sv",)
    assert rule.skills == ("lang-sv/rtl", "org/naming")
    assert rule.checks == ("format", "lint", "naming", "style", "hardcode")  # in this order
    assert rule.budget.tries == 3 and rule.budget.model_fields_set == {"tries"}


def test_the_rule_passes_check_agent_rules(pack: Pack) -> None:
    graph = build_graph(load_pack_rules(pack), _Modules())
    check_agent_rules(graph.rules, graph.instances.values())  # does not raise
    inst = graph.instances[f"{RULE}[block=pwm,module=tiny_pwm]"]
    assert [r.path or r.model_key for r in inst.inputs] == [
        "block/pwm",
        "interface/tiny_pwm",
        "plan/pwm.approved.json",
    ]
    assert [r.path for r in inst.outputs] == ["rtl/tiny_pwm.sv"]


def test_a_testbench_input_breaks_the_rule(pack: Pack) -> None:
    rules = {r.id: r for r in load_pack_rules(pack)}
    rule = rules[RULE]
    inst = RuleInstance(
        rule_id=RULE,
        params={"block": "pwm", "module": "tiny_pwm"},
        inputs=(ArtifactRef(kind="script", path="dv/pwm/test_pwm.py"),),
        outputs=(ArtifactRef(kind="rtl", path="rtl/tiny_pwm.sv"),),
        instance_id=f"{RULE}[block=pwm,module=tiny_pwm]",
    )
    with pytest.raises(RoleError, match=r"'rtl-author' must not see 'tb'.*test_pwm\.py"):
        check_agent_rules({RULE: rule}, [inst])


def test_the_skills_resolve_for_the_rtl_author_only(pack: Pack) -> None:
    skills = load_skills([pack])
    found = skills.resolve(["lang-sv/rtl", "org/naming"], "rtl-author")
    assert [s.id for s in found] == ["lang-sv/rtl", "org/naming"]
    rtl, naming = found
    needles = (
        "always_ff",
        "always_comb",
        "no latches",
        "`default`",
        "REQ-",
        "needs_human",
        "Follow their style where it differs",
        "`#` delays",
        "every output",
    )
    for needle in needles:
        assert needle.lower() in rtl.text.lower(), needle
    for needle in ("rtl.naming.text", "Allowed names, per kind", "the rules win"):
        assert needle in naming.text, needle
    for role in ("author", "tb-author", "critic", "planner"):
        for skill in ("lang-sv/rtl", "org/naming"):
            with pytest.raises(SkillError, match=f"not for role '{role}'"):
                skills.resolve([skill], role)


def test_naming_rules_render_as_text() -> None:
    rules, _ = _load_rules(REPO / "src" / "chipgraph" / "orgs" / "qnsc" / "naming-v1.yml")
    assert isinstance(rules, NamingRules)
    text = naming_text(rules, "org:qnsc/naming-v1.yml")
    assert "QNSC_RTL_Design_Naming_Rule v1.1, from org:qnsc/naming-v1.yml" in text
    assert "- port: `^(?:i_|o_|io_)[a-z0-9_]+$` (1.2 port prefix)" in text
    assert "Lexical rules" in text and "irq -> int" in text
    assert "naming-check: ignore" in text
