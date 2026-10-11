"""M2-08: the profile's `select:` section (best-of-N), and `config check`."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.config import Profile, SelectCfg, load


def _resolved(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chipgraph.yml").write_text(body, encoding="utf-8")
    resolved = load(tmp_path, user_config=tmp_path / "no-user.yml")
    assert resolved is not None
    return resolved


def _select_issues(resolved) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(i.severity, i.key) for i in resolved.check() if i.key.startswith("select.")]


def test_defaults() -> None:
    cfg = Profile(project="p").select
    assert cfg == SelectCfg(n=2, max_n=3, budget_factor=1.5, rules={})
    assert (cfg.effective_n(), cfg.effective_n(3), cfg.effective_n(9)) == (2, 3, 3)


def test_the_profile_sets_the_section(tmp_path: Path) -> None:
    resolved = _resolved(
        tmp_path,
        "project: p\nselect: { n: 3, budget_factor: 1.2, rules: { 'pk/rtl_module': 2 } }\n",
    )
    assert resolved.profile.select == SelectCfg(n=3, budget_factor=1.2, rules={"pk/rtl_module": 2})
    explained = {key: value for key, value, _ in resolved.explain()}
    assert explained["select.max_n"] == 3
    assert explained["select.budget_factor"] == 1.2


@pytest.mark.parametrize(
    "data",
    [{"n": 0}, {"max_n": 0}, {"budget_factor": 0}, {"rules": {"r": 0}}, {"bogus": 1}],
)
def test_invalid_values_are_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SelectCfg.model_validate(data)


def test_config_check_has_no_select_issue_by_default(tmp_path: Path) -> None:
    assert _select_issues(_resolved(tmp_path, "project: p\n")) == []


def test_config_check_warns_about_an_n_above_max_n(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nselect: { n: 5, rules: { 'pk/a': 4 } }\n")
    assert _select_issues(resolved) == [
        ("warning", "select.n"),
        ("warning", "select.rules.pk/a"),
    ]


def test_config_check_warns_when_the_shared_budget_is_n_runs(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nselect: { budget_factor: 2.0 }\n")
    [issue] = [i for i in resolved.check() if i.key.startswith("select.")]
    assert (issue.severity, issue.key) == ("warning", "select.budget_factor")
    assert "costs N runs" in issue.message


def test_config_check_notes_a_budget_below_one_run(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path, "project: p\nselect: { budget_factor: 0.5 }\n")
    assert _select_issues(resolved) == [("info", "select.budget_factor")]
