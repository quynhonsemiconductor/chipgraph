"""Tests for InputSpec, Budget, RuleSpec, and RuleInstance."""

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import ArtifactRef, Budget, InputSpec, RuleInstance, RuleSpec


def _agent_rule() -> RuleSpec:
    return RuleSpec(
        id="digital-rtl/rtl_module",
        kind="agent",
        outputs=("design/{block}/rtl/m_{module}.sv",),
        role="rtl_writer",
        skills=("verilog",),
        budget=Budget(tries=2, tier="large"),
    )


def test_rule_spec_round_trip() -> None:
    rule = _agent_rule()
    assert RuleSpec.model_validate_json(rule.model_dump_json()) == rule


def test_rule_instance_round_trip() -> None:
    params = {"block": "timer", "module": "cnt"}
    instance = RuleInstance(
        rule_id="digital-rtl/rtl_module",
        params=params,
        outputs=(ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv"),),
        instance_id=RuleInstance.make_id("digital-rtl/rtl_module", params),
    )
    assert RuleInstance.model_validate_json(instance.model_dump_json()) == instance


def test_input_spec_short_form() -> None:
    spec = InputSpec.model_validate({"model": "block/{block}"})
    assert spec == InputSpec(source="model", selector="block/{block}")


def test_input_spec_short_form_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        InputSpec.model_validate({"bogus": "block/{block}"})


def test_budget_escalate_must_be_higher_tier() -> None:
    Budget(tier="small", escalate="large")  # ok
    with pytest.raises(ValidationError):
        Budget(tier="large", escalate="small")
    with pytest.raises(ValidationError):
        Budget(tier="medium", escalate="medium")


def test_rule_spec_agent_requires_role() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(
            id="digital-rtl/rtl_module",
            kind="agent",
            outputs=("design/{block}/rtl/m_{module}.sv",),
        )


def test_rule_spec_non_agent_rejects_role() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(
            id="digital-rtl/rtl_module",
            kind="gen",
            outputs=("design/{block}/rtl/m_{module}.sv",),
            role="rtl_writer",
        )


def test_rule_spec_non_agent_rejects_skills() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(
            id="digital-rtl/rtl_module",
            kind="gen",
            outputs=("design/{block}/rtl/m_{module}.sv",),
            skills=("verilog",),
        )


def test_rule_spec_non_agent_rejects_non_default_budget() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(
            id="digital-rtl/rtl_module",
            kind="gen",
            outputs=("design/{block}/rtl/m_{module}.sv",),
            budget=Budget(tries=5),
        )


def test_rule_spec_requires_at_least_one_output() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(id="digital-rtl/rtl_module", kind="gen", outputs=())


def test_rule_spec_rejects_dotdot_output_template() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(id="digital-rtl/rtl_module", kind="gen", outputs=("../escape/m.sv",))


def test_rule_instance_id_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        RuleInstance(
            rule_id="digital-rtl/rtl_module",
            params={"block": "timer"},
            outputs=(ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv"),),
            instance_id="digital-rtl/rtl_module[block=wrong]",
        )


def test_rule_instance_requires_at_least_one_output() -> None:
    with pytest.raises(ValidationError):
        RuleInstance(
            rule_id="digital-rtl/rtl_module",
            params={},
            outputs=(),
            instance_id=RuleInstance.make_id("digital-rtl/rtl_module", {}),
        )


def test_rule_id_pattern_rejected_when_invalid() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(id="NoNamespace", kind="gen", outputs=("a/b.sv",))


def test_rule_spec_frozen() -> None:
    rule = RuleSpec(id="digital-rtl/rtl_module", kind="gen", outputs=("a/b.sv",))
    with pytest.raises(ValidationError):
        rule.description = "changed"  # type: ignore[misc]
