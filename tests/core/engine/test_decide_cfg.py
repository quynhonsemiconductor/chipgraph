"""`DecideCfg` (M1-12): the `decide` section of the profile, its defaults and validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.config import DecideCfg, DecideOverride, Profile, load


def test_defaults() -> None:
    cfg = Profile(project="p").decide
    assert cfg == DecideCfg()
    assert cfg.small_min_confidence == 0.8
    assert cfg.large_min_confidence == 0.5
    assert cfg.enabled_tiers == ("small", "large")
    settings = cfg.for_question("anything")
    assert settings.threshold("small") == 0.8
    assert settings.threshold("large") == 0.5


def test_thresholds_must_be_between_0_and_1() -> None:
    for bad in ({"small_min_confidence": 1.5}, {"large_min_confidence": -0.1}):
        with pytest.raises(ValidationError):
            DecideCfg.model_validate(bad)
    with pytest.raises(ValidationError):
        DecideOverride(small_min_confidence=2)


def test_small_may_be_under_large() -> None:
    cfg = DecideCfg(small_min_confidence=0.3, large_min_confidence=0.9)
    assert cfg.for_question("q").threshold("small") == 0.3


def test_enabled_tiers_are_known_unique_and_ordered() -> None:
    assert DecideCfg(enabled_tiers=("large", "small")).enabled_tiers == ("small", "large")
    with pytest.raises(ValidationError, match="more than once"):
        DecideCfg(enabled_tiers=("small", "small"))
    with pytest.raises(ValidationError):
        DecideCfg.model_validate({"enabled_tiers": ["medium"]})
    with pytest.raises(ValidationError):
        DecideCfg.model_validate({"unknown": 1})


def test_prefix_overrides_apply_shortest_first() -> None:
    cfg = DecideCfg(
        overrides={
            "triage.": DecideOverride(small_min_confidence=0.9, enabled_tiers=("small",)),
            "triage.sim.": DecideOverride(small_min_confidence=0.7),
        }
    )
    sim = cfg.for_question("triage.sim.42")
    assert sim.small_min_confidence == 0.7
    assert sim.enabled_tiers == ("small",)  # from the shorter prefix
    assert sim.large_min_confidence == 0.5  # from the defaults
    lint = cfg.for_question("triage.lint.1")
    assert lint.small_min_confidence == 0.9
    other = cfg.for_question("risk.cmd")
    assert other.small_min_confidence == 0.8
    assert other.enabled_tiers == ("small", "large")
    with pytest.raises(ValidationError, match="must not be empty"):
        DecideCfg(overrides={"": DecideOverride()})


def test_the_profile_file_sets_decide(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / ".chipgraph.yml").write_text(
        "project: demo\n"
        "decide:\n"
        "  small_min_confidence: 0.85\n"
        "  enabled_tiers: [small]\n"
        "  overrides:\n"
        "    triage.:\n"
        "      large_min_confidence: 0.6\n"
        "      enabled_tiers: [small, large]\n"
    )
    resolved = load(tmp_path)
    assert resolved is not None
    cfg = resolved.profile.decide
    assert cfg.small_min_confidence == 0.85
    assert cfg.for_question("risk.x").enabled_tiers == ("small",)
    triage = cfg.for_question("triage.x")
    assert (triage.small_min_confidence, triage.large_min_confidence) == (0.85, 0.6)
    assert triage.enabled_tiers == ("small", "large")
