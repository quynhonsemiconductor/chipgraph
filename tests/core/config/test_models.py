"""Unit tests for the `Profile` and `UserConfig` pydantic models themselves."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chipgraph.core.config.models import PathRule, Profile, RequirementsCfg, UserConfig


def _minimal_profile(**overrides: object) -> Profile:
    data: dict[str, object] = {"project": "qsoc"}
    data.update(overrides)
    return Profile.model_validate(data)


def test_profile_requires_project() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({})


def test_profile_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({"project": "qsoc", "projct": "typo"})


def test_profile_defaults() -> None:
    profile = _minimal_profile()
    assert profile.runtime == "claude-code"
    assert profile.data.default == "internal"
    assert profile.state.backend == "local"
    assert profile.decisions.store == "repo"
    assert profile.target.kind == "asic"


def test_profile_rejects_autonomy_l5() -> None:
    with pytest.raises(ValidationError):
        _minimal_profile(autonomy={"rtl": "L5"})


def test_profile_accepts_autonomy_up_to_l4() -> None:
    profile = _minimal_profile(autonomy={"rtl": "L4"})
    assert profile.autonomy["rtl"] == "L4"


def test_profile_is_frozen() -> None:
    profile = _minimal_profile()
    with pytest.raises(ValidationError):
        profile.project = "other"  # type: ignore[misc]


def test_adapter_cfg_allows_extra_keys() -> None:
    profile = _minimal_profile(
        adapters={"lint": {"use": "make", "cmd": "make lint BLOCK={block}", "parser": "verilator"}}
    )
    assert profile.adapters["lint"].use == "make"
    assert profile.adapters["lint"].model_extra == {
        "cmd": "make lint BLOCK={block}",
        "parser": "verilator",
    }


def test_path_rule_default_needs_no_reason() -> None:
    PathRule()  # allow == on for everything: no reason needed


def test_path_rule_off_without_reason_rejected() -> None:
    with pytest.raises(ValidationError):
        PathRule(checks={"naming": "off"})


def test_path_rule_deny_without_reason_rejected() -> None:
    with pytest.raises(ValidationError):
        PathRule(write="deny")


def test_path_rule_off_with_reason_ok() -> None:
    rule = PathRule(checks={"naming": "off"}, reason="legacy code")
    assert rule.checks["naming"] == "off"


def test_block_override_only_allows_four_fields() -> None:
    profile = _minimal_profile(
        blocks={"uart": {"adapters": {}, "layout": {}, "autonomy": {}, "paths": {}}}
    )
    assert "uart" in profile.blocks
    with pytest.raises(ValidationError):
        _minimal_profile(blocks={"uart": {"target": {"kind": "fpga"}}})


def test_block_override_rejects_autonomy_l5() -> None:
    with pytest.raises(ValidationError):
        _minimal_profile(blocks={"uart": {"autonomy": {"rtl": "L5"}}})


def test_block_override_accepts_instances() -> None:
    profile = _minimal_profile(blocks={"timer": {"instances": ["timer_0", "timer_1"]}})
    assert profile.blocks["timer"].instances == ("timer_0", "timer_1")


def test_block_override_instances_default_empty() -> None:
    profile = _minimal_profile(blocks={"pwm": {}})
    assert profile.blocks["pwm"].instances == ()


def test_block_override_rejects_empty_instance_name() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        _minimal_profile(blocks={"timer": {"instances": [""]}})


def test_block_override_rejects_instance_name_with_colon_or_dot() -> None:
    with pytest.raises(ValidationError, match="must not contain"):
        _minimal_profile(blocks={"timer": {"instances": ["block:timer_0"]}})
    with pytest.raises(ValidationError, match="must not contain"):
        _minimal_profile(blocks={"timer": {"instances": ["timer.0"]}})


def test_block_override_rejects_duplicate_instance_within_block() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        _minimal_profile(blocks={"timer": {"instances": ["timer_0", "timer_0"]}})


def test_user_config_defaults() -> None:
    user = UserConfig()
    assert user.autonomy == {}
    assert user.notify is None


def test_user_config_rejects_layout() -> None:
    with pytest.raises(ValidationError, match="user layer may not change project conventions"):
        UserConfig.model_validate({"layout": {"rtl": "hw/{block}/{module}.v"}})


def test_user_config_rejects_unknown_typo_key() -> None:
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"noitfy": "slack"})


def test_user_config_rejects_autonomy_l5() -> None:
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"autonomy": {"rtl": "L5"}})


def test_user_config_allows_personal_fields() -> None:
    user = UserConfig.model_validate(
        {
            "autonomy": {"rtl": "L2"},
            "notify": "slack",
            "answer_language": "vi",
            "prefer_models": {"medium": "glm"},
            "editor": "vim",
        }
    )
    assert user.autonomy["rtl"] == "L2"
    assert user.prefer_models["medium"] == "glm"


def test_path_rule_accepts_unquoted_yaml_off() -> None:
    import yaml

    from chipgraph.core.config.models import PathRule

    raw = yaml.safe_load('checks: { naming: off, header: on }\nreason: "legacy code"\n')
    rule = PathRule.model_validate(raw)
    assert rule.checks == {"naming": "off", "header": "on"}


# --------------------------------------------------------------------------------------
# spec.requirements (D37)
# --------------------------------------------------------------------------------------


def test_requirements_defaults_to_declared_ids() -> None:
    cfg = Profile(project="x").spec.requirements
    assert cfg.infer == "off"
    assert cfg.infer_heading == "Verification"
    regex = cfg.id_regex()
    assert regex.fullmatch("REQ-TIM-004")
    assert not regex.fullmatch("REQ-tim-4")
    assert not regex.fullmatch("TIM-004")


def test_requirements_pattern_with_block_placeholder() -> None:
    cfg = RequirementsCfg(id_pattern=r"REQ-{BLOCK}-\d+")
    assert cfg.id_regex("timer").fullmatch("REQ-TIMER-12")
    assert not cfg.id_regex("timer").fullmatch("REQ-UART-12")
    with pytest.raises(ValueError, match="block name"):
        cfg.id_regex()


def test_requirements_rejects_bad_regex_and_unknown_mode() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        RequirementsCfg(id_pattern="REQ-[")
    with pytest.raises(ValidationError):
        RequirementsCfg(infer="everything")  # type: ignore[arg-type]


def test_requirements_infer_mode_from_profile_data() -> None:
    profile = Profile.model_validate(
        {"project": "x", "spec": {"requirements": {"infer": "verification"}}}
    )
    assert profile.spec.requirements.infer == "verification"
