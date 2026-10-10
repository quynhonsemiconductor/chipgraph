"""M2-03: the profile's `plan:` section (the Planner's limits), and `config check`."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.config import PlanCfg, Profile, load


def _resolved(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chipgraph.yml").write_text(body, encoding="utf-8")
    resolved = load(tmp_path, user_config=tmp_path / "no-user.yml")
    assert resolved is not None
    return resolved


def test_defaults() -> None:
    assert Profile(project="p").plan == PlanCfg(max_modules=12, max_depth=4, max_total_tries=36)


def test_the_profile_sets_the_limits(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nplan: { max_modules: 6, max_depth: 2 }\n")
    assert resolved.profile.plan == PlanCfg(max_modules=6, max_depth=2, max_total_tries=36)
    explained = {key: value for key, value, _ in resolved.explain()}
    assert explained["plan.max_modules"] == 6
    assert explained["plan.max_total_tries"] == 36


@pytest.mark.parametrize("data", [{"max_modules": 0}, {"max_depth": -1}, {"bogus": 1}])
def test_invalid_limits_are_rejected(data: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        PlanCfg.model_validate(data)


def test_config_check_reports_limits_that_cannot_fit(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nplan: { max_modules: 6, max_total_tries: 4 }\n")
    [issue] = [i for i in resolved.check() if i.key.startswith("plan.")]
    assert (issue.severity, issue.key) == ("warning", "plan.max_total_tries")
    assert "a plan of 6 modules can never pass" in issue.message


def test_config_check_notes_a_depth_above_the_module_limit(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nplan: { max_modules: 2, max_depth: 3 }\n")
    [issue] = [i for i in resolved.check() if i.key.startswith("plan.")]
    assert (issue.severity, issue.key) == ("info", "plan.max_depth")


def test_config_check_has_no_plan_issue_by_default(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\n")
    assert not [i for i in resolved.check() if i.key.startswith("plan.")]
