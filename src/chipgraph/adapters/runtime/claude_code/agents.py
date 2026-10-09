"""The plugin's role subagents, rendered from the role data (DESIGN.md 5.1, 5.5; D8, D35).

Each built-in role's tool table (`chipgraph.core.runtime.roles`) becomes a Claude Code
subagent file `plugin/agents/<role>.md`: its abstract capabilities mapped to Claude
Code tools (`CAPABILITY_TOOLS`), its first tier mapped to a model alias, its prompt with
the tool placeholders filled in. The files are generated; a test fails when a committed
one differs from the render::

    python -m chipgraph.adapters.runtime.claude_code.agents --write   # regenerate
    python -m chipgraph.adapters.runtime.claude_code.agents --check   # exit 1 if stale

`asker.md` and `decider.md` stay hand-written: `/chipgraph:ask` is not a role, and
`decider` serves the `triage` role through `decide()` (its tools and tier are checked
against that role by a test).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chipgraph.adapters.runtime.claude_code.runtime import PLUGIN_NAME
from chipgraph.core.contracts import ModelTier
from chipgraph.core.runtime.roles import CAPABILITIES, Capability, RoleSpec, get_role

MCP_TOOL_PREFIX = f"mcp__plugin_{PLUGIN_NAME}_{PLUGIN_NAME}__"
"""How Claude Code names the plugin's MCP server tools: `mcp__plugin_<p>_<server>__<t>`."""

CAPABILITY_TOOLS: dict[Capability, tuple[str, ...]] = {
    "read_files": ("Read",),
    "search_files": ("Glob", "Grep"),
    "write_outputs": ("Write", "Edit", "MultiEdit"),
    "engine_context": (f"{MCP_TOOL_PREFIX}get_context",),
    "engine_decisions": (f"{MCP_TOOL_PREFIX}pending_decisions",),
}
"""The Claude Code tools for each abstract capability. Nothing maps to a shell or to web
tools: roles get neither."""

DEFAULT_TIER_MODELS: dict[ModelTier, str] = {"small": "haiku", "medium": "sonnet", "large": "opus"}
"""Claude Code model aliases per tier, when the profile's `models.tiers` names none. The
agent file's `model` is its role's first tier; `next_task` passes the model per attempt."""

RENDERED_ROLES = ("author", "tb-author", "critic", "planner", "researcher")
"""The roles with a generated subagent file."""

HAND_WRITTEN_AGENTS = {"triage": "decider"}
"""Roles served by a hand-written subagent, and its name."""

_GENERATED_NOTE = (
    "# Generated from src/chipgraph/core/runtime/roles/data/{role}.md by\n"
    "# `python -m chipgraph.adapters.runtime.claude_code.agents --write`; do not edit.\n"
)


def agent_name(role_id: str) -> str:
    """The subagent file name (without `.md`) for a role: its id, or the hand-written one."""
    short = role_id.rsplit("/", 1)[-1]
    return HAND_WRITTEN_AGENTS.get(short, short)


def agent_type(role_id: str) -> str:
    """The `subagent_type` for a role: `chipgraph:<agent name>`."""
    return f"{PLUGIN_NAME}:{agent_name(role_id)}"


def tools_for(role: RoleSpec) -> tuple[str, ...]:
    """The Claude Code tools a role's subagent gets, in capability order."""
    return tuple(
        tool
        for capability in CAPABILITIES
        if capability in role.tools
        for tool in CAPABILITY_TOOLS[capability]
    )


def _quoted(text: str) -> str:
    """A single-quoted YAML scalar."""
    return "'" + text.replace("'", "''") + "'"


def render_agent(role: RoleSpec) -> str:
    """The subagent file for `role`."""
    names: dict[str, tuple[str, ...]] = {c: CAPABILITY_TOOLS[c] for c in role.tools}
    head = (
        "---\n"
        + _GENERATED_NOTE.format(role=role.id)
        + f"name: {agent_name(role.id)}\n"
        + f"description: {_quoted(role.description)}\n"
        + f"tools: {', '.join(tools_for(role))}\n"
        + f"model: {DEFAULT_TIER_MODELS[role.default_tier]}\n"
        + "---\n\n"
    )
    return head + role.render_prompt(names)


def rendered_files() -> dict[str, str]:
    """File name (`<role>.md`) to content, for every generated subagent file."""
    return {f"{agent_name(r)}.md": render_agent(get_role(r)) for r in RENDERED_ROLES}


def _default_agents_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate / "plugin" / "agents"
    raise FileNotFoundError(f"no pyproject.toml found above {here}")


def check(agents_dir: Path) -> list[str]:
    """Generated files in `agents_dir` that are missing or differ from the render."""
    problems = []
    for name, content in rendered_files().items():
        path = agents_dir / name
        if not path.is_file():
            problems.append(f"missing: {path}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"outdated: {path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render plugin/agents/ from the role data.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the agent files")
    mode.add_argument("--check", action="store_true", help="exit 1 if a file is out of date")
    parser.add_argument("--dir", type=Path, default=None, help="default: plugin/agents/")
    args = parser.parse_args(argv)
    agents_dir: Path = args.dir if args.dir is not None else _default_agents_dir()
    if args.check:
        problems = check(agents_dir)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name, content in rendered_files().items():
        (agents_dir / name).write_text(content, encoding="utf-8")
    print(f"wrote {len(RENDERED_ROLES)} agent files to {agents_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAPABILITY_TOOLS",
    "DEFAULT_TIER_MODELS",
    "HAND_WRITTEN_AGENTS",
    "MCP_TOOL_PREFIX",
    "RENDERED_ROLES",
    "agent_name",
    "agent_type",
    "check",
    "render_agent",
    "rendered_files",
    "tools_for",
]
