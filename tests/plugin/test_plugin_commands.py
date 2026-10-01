"""M1-16: static checks of the Claude Code plugin (`plugin/`), without Claude Code.

`claude plugin validate` is run by the reviewer; these tests check what chipgraph itself
relies on: every command's frontmatter (a description; `allowed-tools` only chipgraph MCP
tools plus `Agent`/`AskUserQuestion`; `disallowed-tools` keeping file, shell, skill and web
tools out), that every MCP tool a command or agent names exists in the server, that every
subagent a command starts is one of the plugin's agents, and that the manifests parse.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from chipgraph.mcp.server import build_server

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin"
COMMANDS = sorted((PLUGIN / "commands").glob("*.md"))
AGENTS = sorted((PLUGIN / "agents").glob("*.md"))

MCP_PREFIX = "mcp__plugin_chipgraph_chipgraph__"
MCP_NAME = re.compile(rf"{MCP_PREFIX}(\w+)")
# A subagent type the body tells the session to start (not a `/chipgraph:<command>`).
SUBAGENT_REF = re.compile(r"(?<![/\w:-])chipgraph:([a-z_]+)")
NON_MCP_ALLOWED = {"Agent", "AskUserQuestion"}
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
# Every command must keep these out (`allowed-tools` only pre-approves; this removes).
ALWAYS_DENIED = {"Bash", "NotebookEdit", "Skill", "WebFetch", "WebSearch"}
# ... and these too, unless a subagent it starts needs file tools (`/chipgraph:run`'s
# role subagents write their task's outputs; the write guard bounds them).
FILE_DENIED = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"}

EXPECTED_COMMANDS = {"ask", "decide", "init-chipgraph", "run", "status", "trace", "triage"}


def _split(text: str) -> tuple[dict[str, Any], str]:
    assert text.startswith("---\n"), "frontmatter must open on the first line"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, "frontmatter is not closed"
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data, body


def _tools(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(v).strip() for v in value}
    return {t for t in re.split(r"[,\s]+", str(value)) if t}


@pytest.fixture(scope="module")
def server_tools(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    root = tmp_path_factory.mktemp("proj")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    async def _list() -> set[str]:
        async with Client(build_server(root)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    return asyncio.run(_list())


def _agents() -> dict[str, dict[str, Any]]:
    return {path.stem: _split(path.read_text(encoding="utf-8"))[0] for path in AGENTS}


def _started_agents(body: str) -> set[str]:
    return set(SUBAGENT_REF.findall(body))


def test_the_v0_commands_exist() -> None:
    assert {path.stem for path in COMMANDS} == EXPECTED_COMMANDS


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_command_frontmatter(path: Path, server_tools: set[str]) -> None:
    meta, body = _split(path.read_text(encoding="utf-8"))
    assert isinstance(meta.get("description"), str) and meta["description"].strip()
    assert "name" not in meta and "paths" not in meta  # not accepted in command files

    allowed = _tools(meta.get("allowed-tools"))
    denied = _tools(meta.get("disallowed-tools"))
    assert allowed, "list the tools the command uses in allowed-tools"
    for tool in allowed:
        if tool in NON_MCP_ALLOWED:
            continue
        assert tool.startswith(MCP_PREFIX), f"{tool} is not a chipgraph MCP tool"
        assert tool.removeprefix(MCP_PREFIX) in server_tools, f"{tool} is not a server tool"
    assert not allowed & denied, f"both allowed and disallowed: {allowed & denied}"

    agents = _agents()
    started = _started_agents(body)
    needs_files = any(FILE_TOOLS & _tools(agents[name].get("tools")) for name in started)
    required = ALWAYS_DENIED if needs_files else ALWAYS_DENIED | FILE_DENIED
    if "Agent" not in allowed:
        required = required | {"Agent"}
    assert required <= denied, f"disallowed-tools misses {sorted(required - denied)}"


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_command_body_names_only_real_allowed_tools(path: Path, server_tools: set[str]) -> None:
    meta, body = _split(path.read_text(encoding="utf-8"))
    allowed = _tools(meta.get("allowed-tools"))
    for name in MCP_NAME.findall(body):
        assert name in server_tools, f"{path.name} names an unknown MCP tool {name!r}"
        assert f"{MCP_PREFIX}{name}" in allowed, f"{path.name}: {name} not in allowed-tools"
    started = _started_agents(body)
    assert started <= set(_agents()), f"{path.name} starts unknown agents {started}"
    if started:
        assert "Agent" in allowed


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_frontmatter(path: Path, server_tools: set[str]) -> None:
    meta, body = _split(path.read_text(encoding="utf-8"))
    assert meta.get("name") == path.stem
    assert isinstance(meta.get("description"), str) and meta["description"].strip()
    tools = _tools(meta.get("tools"))
    assert tools
    assert "Bash" not in tools  # role subagents never get a shell (the guard denies it)
    for tool in tools | {f"{MCP_PREFIX}{n}" for n in MCP_NAME.findall(body)}:
        if tool.startswith(MCP_PREFIX):
            assert tool.removeprefix(MCP_PREFIX) in server_tools, f"{tool} is not a server tool"


def test_trace_and_status_and_init_reach_the_server_tools_they_need() -> None:
    expected = {
        "status": {"status"},
        "trace": {"model_trace", "model_find"},
        "init-chipgraph": {"init"},
        "ask": {"ask_check"},
        "triage": {"triage", "pending_decisions", "answer_decision"},
    }
    for name, tools in expected.items():
        meta, _ = _split((PLUGIN / "commands" / f"{name}.md").read_text(encoding="utf-8"))
        allowed = {t.removeprefix(MCP_PREFIX) for t in _tools(meta.get("allowed-tools"))}
        assert tools <= allowed, name


def test_init_command_writes_only_after_a_yes() -> None:
    _, body = _split((PLUGIN / "commands" / "init-chipgraph.md").read_text(encoding="utf-8"))
    assert "`confirm` =\n   false" in body or "`confirm` = false" in body
    assert "--yes" in body and "AskUserQuestion" in body


def test_manifests_parse_and_point_at_real_files() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "chipgraph"
    assert manifest["description"]
    for key in ("version", "license", "repository"):
        assert manifest[key]

    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "chipgraph"
    assert market["owner"]["name"]
    [entry] = [p for p in market["plugins"] if p["name"] == "chipgraph"]
    assert (REPO / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    assert entry["description"]

    mcp = json.loads((PLUGIN / ".mcp.json").read_text())
    server = mcp["mcpServers"]["chipgraph"]
    assert server["command"] == "uvx"
    assert server["args"][-3:] == ["-C", "${CLAUDE_PROJECT_DIR}", "mcp"]

    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    for group in hooks["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            for arg in hook.get("args", []):
                if arg.startswith("${CLAUDE_PLUGIN_ROOT}/"):
                    assert (PLUGIN / arg.removeprefix("${CLAUDE_PLUGIN_ROOT}/")).is_file()


def test_descriptions_name_the_commands(server_tools: set[str]) -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = [p for p in market["plugins"] if p["name"] == "chipgraph"]
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    for command in EXPECTED_COMMANDS:
        assert f"/chipgraph:{command}" in readme, f"README does not document {command}"
    for text in (manifest["description"], entry["description"]):
        for command in ("ask", "status", "trace", "triage", "init-chipgraph"):
            assert f"/{command}" in text or f":{command}" in text, (command, text)
    # The README's tool list stays current: every server tool is named there.
    for tool in server_tools:
        assert f"`{tool}`" in readme or f"`{tool}(" in readme, f"README misses tool {tool}"
