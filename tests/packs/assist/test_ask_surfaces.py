"""M1-13: the `/ask` surfaces: the two MCP tools, `chipgraph ask`, and the plugin files."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from ask_helpers import copy_tinysoc, ingested_tinysoc
from mcp import Client
from typer.testing import CliRunner

from chipgraph.adapters.llm import FakeProvider
from chipgraph.app.context import AppContext
from chipgraph.cli import app
from chipgraph.mcp.server import build_server
from chipgraph.packs.assist.ask import answer as answer_mod

PLUGIN = Path(__file__).resolve().parents[3] / "plugin"
ASK_CONTEXT = "mcp__plugin_chipgraph_chipgraph__ask_context"
ASK_CHECK = "mcp__plugin_chipgraph_chipgraph__ask_check"
QUESTION = "What is the reset value of the timer COMPARE register?"


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("ask") / "tinysoc")


def _call(root: Path, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async def _run() -> tuple[bool, Any]:
        async with Client(build_server(root)) as client:
            result = await client.call_tool(tool, args)
            if result.is_error:
                return True, " ".join(getattr(c, "text", "") for c in result.content)
            return False, result.structured_content

    return asyncio.run(_run())


# --- MCP ------------------------------------------------------------------------------


def test_ask_context_tool(tinysoc: AppContext) -> None:
    error, payload = _call(tinysoc.root, "ask_context", {"question": QUESTION, "limit": 5})
    assert not error, payload
    assert payload["no_sources"] is False and len(payload["sources"]) == 5
    assert payload["sources"][0]["citation"] == "model:register:timer.COMPARE"


def test_ask_check_tool_accepts_and_rejects(tinysoc: AppContext) -> None:
    good = {"answer": "0", "citations": ["doc/specs/TINY_TIMER_MAS.md:60"], "unknown": False}
    error, payload = _call(tinysoc.root, "ask_check", good)
    assert not error and payload["ok"] is True
    assert payload["answer"]["citations"] == ["doc/specs/TINY_TIMER_MAS.md:60"]

    error, payload = _call(tinysoc.root, "ask_check", {"answer": "0x5"})
    assert not error and payload["ok"] is False and payload["reasons"]

    error, payload = _call(tinysoc.root, "ask_check", {"answer": "I don't know", "unknown": True})
    assert not error and payload["ok"] is True


def test_ask_tools_without_a_model_are_a_tool_error(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "t")
    error, message = _call(root, "ask_context", {"question": QUESTION})
    assert error and "chipgraph ingest" in message
    error, message = _call(root, "ask_check", {"answer": "x", "citations": ["chip.yml:1"]})
    assert error and "chipgraph ingest" in message


# --- CLI ------------------------------------------------------------------------------


def test_cli_ask_without_a_provider_prints_the_sources(tinysoc: AppContext) -> None:
    result = CliRunner().invoke(app, ["-C", str(tinysoc.root), "ask", QUESTION])
    assert result.exit_code == 0, result.output
    assert "model:register:timer.COMPARE" in result.output
    assert "/chipgraph:ask" in result.output


def test_cli_ask_json_without_a_provider(tinysoc: AppContext) -> None:
    result = CliRunner().invoke(app, ["-C", str(tinysoc.root), "--json", "ask", QUESTION])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "no_provider" and payload["answer"] is None


def test_cli_ask_with_a_fake_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ingested_tinysoc(
        tmp_path / "t",
        profile_extra="models:\n  providers:\n    fake: {}\n  tiers:\n    small: fake-small\n",
    )
    reply = json.dumps(
        {"answer": "COMPARE resets to 0.", "citations": ["rtl/tiny_timer.sv:29"], "unknown": False}
    )
    monkeypatch.setattr(
        answer_mod, "make_provider", lambda profile, name=None: FakeProvider([reply])
    )
    result = CliRunner().invoke(app, ["-C", str(ctx.root), "ask", QUESTION])
    assert result.exit_code == 0, result.output
    assert "COMPARE resets to 0." in result.output
    assert "rtl/tiny_timer.sv:29  " in result.output and "compare_q" in result.output


def test_cli_ask_exits_1_when_the_answer_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ingested_tinysoc(
        tmp_path / "t",
        profile_extra="models:\n  providers:\n    fake: {}\n  tiers:\n    small: fake-small\n",
    )
    invented = json.dumps({"answer": "0x5", "citations": ["model:register:timer.NOPE"]})
    monkeypatch.setattr(
        answer_mod, "make_provider", lambda profile, name=None: FakeProvider([invented] * 2)
    )
    result = CliRunner().invoke(app, ["-C", str(ctx.root), "ask", QUESTION])
    assert result.exit_code == 1
    assert "I don't know" in result.output and "0x5" not in result.stdout


def test_cli_ask_without_a_model_store_exits_2(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "t")
    result = CliRunner().invoke(app, ["-C", str(root), "ask", QUESTION])
    assert result.exit_code == 2
    assert "chipgraph ingest" in result.output


# --- plugin ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> dict[str, Any]:
    _, head, _ = path.read_text().split("---", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def test_asker_agent_has_only_the_two_ask_tools_and_haiku() -> None:
    meta = _frontmatter(PLUGIN / "agents" / "asker.md")
    assert meta["name"] == "asker"
    assert meta["model"] == "haiku"
    assert [t.strip() for t in meta["tools"].split(",")] == [ASK_CONTEXT, ASK_CHECK]


def test_ask_command_starts_the_asker_and_checks_its_answer() -> None:
    path = PLUGIN / "commands" / "ask.md"
    meta = _frontmatter(path)
    allowed = [t.strip() for t in meta["allowed-tools"].split(",")]
    assert allowed == ["Agent", ASK_CHECK]
    body = path.read_text()
    assert "chipgraph:asker" in body and "$ARGUMENTS" in body and "haiku" in body
