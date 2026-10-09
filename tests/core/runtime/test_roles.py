"""M2-01: the built-in roles as data (DESIGN.md 5.1): the registry, the tool table of each
role, the role file format, the model ladder, and the committed JSON Schemas."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.core.contracts import Budget
from chipgraph.core.runtime.roles import (
    RoleError,
    RoleSpec,
    find_role,
    get_role,
    list_roles,
    model_ladder,
    parse_role_file,
    roles_dir,
)
from chipgraph.core.runtime.roles import schema as role_schema

REPO = Path(__file__).resolve().parents[3]

EXPECTED = {
    # id: (default_tier, escalate_to, tools, write_scope, read mode, deny kinds, dispatch)
    "author": (
        "medium",
        "large",
        ("read_files", "search_files", "write_outputs", "engine_context"),
        "outputs",
        "any",
        (),
        True,
    ),
    "tb-author": (
        "medium",
        "large",
        ("write_outputs", "engine_context"),
        "outputs",
        "deny",
        ("rtl",),
        True,
    ),
    "critic": (
        "large",
        None,
        ("read_files", "search_files", "engine_context"),
        "none",
        "any",
        (),
        True,
    ),
    "planner": (
        "large",
        None,
        ("read_files", "search_files", "write_outputs", "engine_context"),
        "plan",
        "any",
        (),
        True,
    ),
    "researcher": (
        "medium",
        "large",
        ("read_files", "search_files", "write_outputs", "engine_context"),
        "proposal",
        "any",
        (),
        True,
    ),
    "triage": ("small", "large", ("engine_decisions",), "none", "any", (), False),
}


def _table(role: RoleSpec) -> tuple[object, ...]:
    return (
        role.default_tier,
        role.escalate_to,
        role.tools,
        role.write_scope,
        role.read_policy.mode,
        role.read_policy.deny_kinds,
        role.dispatch,
    )


def test_the_five_roles_and_tb_author_are_data() -> None:
    roles = {role.id: role for role in list_roles()}
    assert set(roles) == set(EXPECTED)
    for role_id, expected in EXPECTED.items():
        assert _table(roles[role_id]) == expected, role_id
        assert roles[role_id].shell is False
        assert (roles_dir() / f"{role_id}.md").is_file()


def test_choosing_a_role() -> None:
    assert get_role("author").id == "author"
    assert get_role("tb-author").can_read_files is False
    assert get_role("author").can_read_files is True
    assert get_role("some-pack/author").id == "author"  # a namespace is ignored
    assert find_role("nobody") is None


def test_an_unknown_role_is_a_clear_error() -> None:
    with pytest.raises(RoleError, match=r"unknown role 'writer'.*author, critic"):
        get_role("writer")


def test_triage_says_it_is_served_by_decide() -> None:
    triage = get_role("triage")
    assert "decide()" in triage.description and "next_task" in triage.description
    assert triage.dispatch is False


def test_tb_author_prompt_has_no_read_tools() -> None:
    prompt = get_role("tb-author").prompt
    assert "{tool:read_files}" not in prompt and "{tool:search_files}" not in prompt
    assert "{tool:engine_context}" in prompt


def test_prompts_name_tools_only_through_placeholders() -> None:
    for role in list_roles():
        for name in ("Read", "Glob", "Grep", "Write", "Bash", "mcp__"):
            assert f"`{name}`" not in role.prompt, (role.id, name)


def test_render_prompt_fills_placeholders() -> None:
    role = get_role("author")
    names = {
        "read_files": ("R",),
        "search_files": ("G1", "G2"),
        "write_outputs": ("W",),
        "engine_context": ("ctx",),
    }
    text = role.render_prompt(names)
    assert "{tool:" not in text and "`ctx`" in text and "`R`, `G1`, `G2`" in text
    with pytest.raises(RoleError, match="no tool for capability"):
        role.render_prompt({})


def _base(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "x-role",
        "description": "one line",
        "prompt": "do {tool:engine_context}",
        "default_tier": "medium",
        "tools": ["engine_context", "write_outputs"],
        "write_scope": "outputs",
    }
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"escalate_to": "small"}, "must be higher"),
        ({"write_scope": "none"}, "write_outputs is granted exactly"),
        ({"tools": ["engine_context"]}, "write_outputs is granted exactly"),
        ({"prompt": "use {tool:read_files}"}, "not granted"),
        ({"description": "two\nlines"}, "one line"),
        ({"shell": True}, "shell"),
        ({"tools": ["engine_context", "engine_context", "write_outputs"]}, "twice"),
        ({"tools": ["engine_context", "write_outputs", "run_shell"]}, "tools"),
        ({"read_policy": {"mode": "deny"}}, "needs deny_kinds"),
        ({"read_policy": {"mode": "any", "deny_kinds": ["rtl"]}}, "takes no deny"),
        ({"id": "Bad_Id"}, "id"),
    ],
)
def test_role_spec_rejects_a_broken_table(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RoleSpec.model_validate(_base(**changes))


def test_role_file_format(tmp_path: Path) -> None:
    good = tmp_path / "x-role.md"
    good.write_text(
        "---\nid: x-role\ndescription: one line\ndefault_tier: small\n"
        "tools: [engine_context]\nwrite_scope: none\n---\n\nThe prompt {tool:engine_context}.\n"
    )
    role = parse_role_file(good)
    assert role.prompt == "The prompt {tool:engine_context}.\n"

    misnamed = tmp_path / "other.md"
    misnamed.write_text(good.read_text())
    with pytest.raises(RoleError, match=r"name it x-role\.md"):
        parse_role_file(misnamed)

    no_front = tmp_path / "y.md"
    no_front.write_text("just a prompt\n")
    with pytest.raises(RoleError, match="frontmatter"):
        parse_role_file(no_front)

    prompt_key = tmp_path / "x-role.md"
    prompt_key.write_text(
        good.read_text().replace("write_scope: none", "write_scope: none\nprompt: x")
    )
    with pytest.raises(RoleError, match="body"):
        parse_role_file(prompt_key)


# --- model tiers and escalation ---------------------------------------------------------


def test_the_ladder_comes_from_the_role() -> None:
    assert model_ladder(get_role("author"), Budget(tries=3)) == ("medium", "large")
    assert model_ladder(get_role("critic"), Budget()) == ("large", None)
    assert model_ladder(get_role("triage"), Budget()) == ("small", "large")


def test_a_rule_that_sets_a_tier_owns_the_ladder() -> None:
    author = get_role("author")
    assert model_ladder(author, Budget(tier="small")) == ("small", None)
    assert model_ladder(author, Budget(tier="small", escalate="medium")) == ("small", "medium")
    assert model_ladder(author, Budget(escalate="large")) == ("medium", "large")
    assert model_ladder(author, Budget.model_validate({"tier": "large"})) == ("large", None)


def test_an_unknown_role_keeps_the_budget() -> None:
    assert model_ladder(None, Budget()) == ("medium", None)


# --- schemas ----------------------------------------------------------------------------


def test_committed_role_schemas_are_current() -> None:
    assert role_schema.check(REPO / "schemas" / "roles") == []


def test_schema_check_reports_missing_and_extra(tmp_path: Path) -> None:
    (tmp_path / "Old.schema.json").write_text("{}")
    problems = role_schema.check(tmp_path)
    assert "missing: roles/RoleSpec.schema.json" in problems
    assert "extra: roles/Old.schema.json" in problems
    assert role_schema.main(["--out", str(tmp_path)]) == 0
    assert role_schema.main(["--check", "--out", str(tmp_path)]) == 1  # the extra file
    (tmp_path / "Old.schema.json").unlink()
    assert role_schema.main(["--check", "--out", str(tmp_path)]) == 0
