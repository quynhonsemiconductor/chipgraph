"""M2-01: the plugin's role subagents are rendered from the role data, and stay equal to
it: the committed `plugin/agents/*.md` must be the render; `decider.md` (hand-written)
must match the `triage` role's tools and tier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from chipgraph.adapters.runtime.claude_code import agents
from chipgraph.core.runtime.roles import CAPABILITIES, get_role, list_roles

REPO = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO / "plugin" / "agents"
NO_READ = {"Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "NotebookRead", "LS"}


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    head, sep, _ = text[4:].partition("\n---\n")
    assert text.startswith("---\n") and sep
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def _tools(meta: dict[str, Any]) -> list[str]:
    return [t.strip() for t in str(meta["tools"]).split(",") if t.strip()]


@pytest.mark.parametrize("role_id", agents.RENDERED_ROLES)
def test_committed_agent_file_equals_the_render(role_id: str) -> None:
    path = AGENTS_DIR / f"{role_id}.md"
    assert path.read_text(encoding="utf-8") == agents.render_agent(get_role(role_id)), (
        f"{path} is stale: run python -m chipgraph.adapters.runtime.claude_code.agents --write"
    )


def test_check_command_passes_on_the_repo() -> None:
    assert agents.check(AGENTS_DIR) == []
    assert agents.main(["--check"]) == 0


def test_check_command_finds_a_stale_file(tmp_path: Path) -> None:
    assert agents.main(["--write", "--dir", str(tmp_path)]) == 0
    assert agents.main(["--check", "--dir", str(tmp_path)]) == 0
    stale = tmp_path / "critic.md"
    stale.write_text(stale.read_text() + "edited\n")
    (tmp_path / "planner.md").unlink()
    problems = agents.check(tmp_path)
    assert any("outdated" in p and "critic.md" in p for p in problems)
    assert any("missing" in p and "planner.md" in p for p in problems)
    assert agents.main(["--check", "--dir", str(tmp_path)]) == 1


@pytest.mark.parametrize("role_id", agents.RENDERED_ROLES)
def test_tool_table_equals_the_agent_file(role_id: str) -> None:
    role = get_role(role_id)
    meta = _frontmatter(AGENTS_DIR / f"{role_id}.md")
    assert meta["name"] == role_id
    assert meta["description"] == role.description
    assert meta["model"] == agents.DEFAULT_TIER_MODELS[role.default_tier]
    expected = [
        tool for cap in CAPABILITIES if cap in role.tools for tool in agents.CAPABILITY_TOOLS[cap]
    ]
    assert _tools(meta) == expected
    body = (AGENTS_DIR / f"{role_id}.md").read_text(encoding="utf-8")
    assert "{tool:" not in body
    assert "Bash" not in _tools(meta) and "PowerShell" not in _tools(meta)


def test_tb_author_agent_has_no_read_tools() -> None:
    meta = _frontmatter(AGENTS_DIR / "tb-author.md")
    tools = set(_tools(meta))
    assert not tools & NO_READ
    assert tools == {"Write", "Edit", "MultiEdit", f"{agents.MCP_TOOL_PREFIX}get_context"}


def test_write_tools_follow_the_write_scope() -> None:
    for role_id in agents.RENDERED_ROLES:
        role = get_role(role_id)
        writes = {"Write", "Edit", "MultiEdit"} & set(agents.tools_for(role))
        assert bool(writes) == (role.write_scope != "none"), role_id


def test_decider_matches_the_triage_role() -> None:
    triage = get_role("triage")
    meta = _frontmatter(AGENTS_DIR / "decider.md")
    assert agents.agent_name("triage") == "decider"
    assert _tools(meta) == list(agents.tools_for(triage))
    assert meta["model"] == agents.DEFAULT_TIER_MODELS[triage.default_tier]


def test_hand_written_agents_are_not_rendered() -> None:
    names = set(agents.rendered_files())
    assert "asker.md" not in names and "decider.md" not in names
    assert names == {f"{r}.md" for r in agents.RENDERED_ROLES}


def test_every_role_has_an_agent_file() -> None:
    for role in list_roles():
        assert (AGENTS_DIR / f"{agents.agent_name(role.id)}.md").is_file(), role.id


def test_capability_map_gives_no_shell_or_web() -> None:
    assert set(agents.CAPABILITY_TOOLS) == set(CAPABILITIES)
    for tools in agents.CAPABILITY_TOOLS.values():
        assert tools
        assert not {"Bash", "PowerShell", "WebFetch", "WebSearch"} & set(tools)


def test_agent_type_names() -> None:
    assert agents.agent_type("author") == "chipgraph:author"
    assert agents.agent_type("pack/tb-author") == "chipgraph:tb-author"
    assert agents.agent_type("triage") == "chipgraph:decider"
