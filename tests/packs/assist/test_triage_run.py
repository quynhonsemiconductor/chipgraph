"""M1-14: `run_triage` end to end on a tinysoc copy, its surfaces, and the plugin files.

No real model: `FakeProvider` plays the model tiers through `LlmDecideBackend` (API
runtime), and the Claude Code path is driven through the decision queue the
`pending_decisions` / `answer_decision` MCP tools use.
"""

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
from chipgraph.adapters.llm.decide_backend import LlmDecideBackend
from chipgraph.adapters.runtime.claude_code.decisions import (
    DECIDER_AGENT,
    ClaudeCodeDecideBackend,
    answer_decision,
    pending_decisions,
)
from chipgraph.app.context import AppContext
from chipgraph.cli import app
from chipgraph.core.config.models import ModelsCfg
from chipgraph.core.engine.decide import DecisionLog
from chipgraph.mcp.server import build_server
from chipgraph.packs.assist.triage import LABELS, PROMPT, default_backend, run_triage
from chipgraph.packs.assist.triage import run as run_mod

PLUGIN = Path(__file__).resolve().parents[3] / "plugin"
TRIAGE = "mcp__plugin_chipgraph_chipgraph__triage"
PENDING = "mcp__plugin_chipgraph_chipgraph__pending_decisions"
ANSWER = "mcp__plugin_chipgraph_chipgraph__answer_decision"
MODELS = ModelsCfg(tiers={"small": "small-model-1", "large": "large-model-1"})

RTL_LOG = (
    "verilator --lint-only -Wall -f filelists/gpio.f --top-module tiny_gpio\n"
    "%Error: rtl/tiny_gpio.sv:51:20: Can't find definition of variable: 'out_x'\n"
    "%Error: Exiting due to 1 error(s)\n"
    "make: *** [lint] Error 1\n"
)
INFRA_LOG = (
    "verilator --lint-only -Wall -f filelists/gpio.f --top-module tiny_gpio\n"
    "make: verilator: No such file or directory\nmake: *** [lint] Error 1\n"
)
SPEC_LOG = (
    "FAIL   ports_diff gpio 1 issues\n"
    "  rtl/tiny_gpio.sv:21  [ports_diff.width] port 'pin_in' of IP 'gpio': spec width 16 "
    "but RTL width 8\n"
)
SIM_LOG = (
    "tb: reset released\n"
    "tb: read  addr=0x1 data=0xa5\n"
    "FAIL DATA_IN with pin_in=0x5a: expected 0x5a got 0xa5 (rst_n=1)\n"
    "[101000] %Fatal: tb_gpio.sv:96: Assertion failed in tb_gpio: tb_gpio failed\n"
    "%Error: tb/tb_gpio.sv:96: Verilog $stop\n"
)


def _reply(value: str, confidence: float, reason: str = "because") -> str:
    return json.dumps({"value": value, "confidence": confidence, "reason": reason})


@pytest.fixture(scope="module")
def tinysoc(tmp_path_factory: pytest.TempPathFactory) -> AppContext:
    return ingested_tinysoc(tmp_path_factory.mktemp("triage") / "tinysoc")


# --- rules through decide() -----------------------------------------------------------------


def test_a_rule_decides_and_the_report_says_where(tinysoc: AppContext) -> None:
    report = run_triage(tinysoc, RTL_LOG, backend=None)
    assert (report.status, report.label, report.backend) == ("decided", "rtl", "rule")
    assert report.rule == "rtl_lint" and report.confidence == 1.0
    assert not report.low_confidence and not report.retry_without_counting
    assert report.evidence == ("rtl/tiny_gpio.sv:51",)
    assert "rtl/tiny_gpio.sv:51" in report.summary and "out_x" in report.summary
    assert report.suggestion.startswith("Fix the RTL at rtl/tiny_gpio.sv:51")
    assert report.question_id.startswith("triage.")


def test_infra_says_retry_without_counting_a_try(tinysoc: AppContext) -> None:
    report = run_triage(tinysoc, INFRA_LOG, check_id="lint", backend=None)
    assert (report.label, report.rule) == ("infra", "tool_missing")
    assert report.retry_without_counting and not report.ask_person
    assert "`verilator`" in report.suggestion and "does not count a try" in report.suggestion
    assert "rerun `lint`" in report.suggestion


def test_spec_names_the_mas_line_and_asks_the_owner(tinysoc: AppContext) -> None:
    report = run_triage(tinysoc, SPEC_LOG, backend=None)
    assert (report.label, report.rule, report.check_id) == (
        "spec",
        "spec_cross_check",
        "ports_diff",
    )
    assert report.ask_person and not report.retry_without_counting
    assert "spec owner" in report.suggestion
    # The issue points at the RTL port; the suggestion points at the MAS line for it.
    assert any(s.location.startswith("doc/specs/TINY_GPIO_MAS.md:") for s in report.spec_lines)
    assert "doc/specs/TINY_GPIO_MAS.md:" in report.suggestion


def test_every_step_is_in_the_decision_log(tinysoc: AppContext) -> None:
    report = run_triage(tinysoc, RTL_LOG, backend=None)
    entries = [
        e
        for e in DecisionLog.for_layout(tinysoc.layout).read()
        if e.question_id == report.question_id
    ]
    assert entries and entries[-1].event == "decided" and entries[-1].backend == "rule"


def test_no_backend_and_no_rule_is_undecided(tinysoc: AppContext) -> None:
    report = run_triage(tinysoc, SIM_LOG, backend=None)
    assert (report.status, report.label) == ("undecided", None)
    assert "no model is configured" in report.message
    assert report.summary.startswith("simulation failed")


# --- the model tiers (API runtime, FakeProvider) ---------------------------------------------


def test_a_confident_small_answer_decides(tinysoc: AppContext) -> None:
    fake = FakeProvider([_reply("rtl", 0.93, "DATA_IN must read pin_in")])
    report = run_triage(tinysoc, SIM_LOG, backend=LlmDecideBackend(fake, MODELS))
    assert (report.status, report.label, report.backend) == ("decided", "rtl", "small")
    assert report.confidence == 0.93 and not report.low_confidence
    assert report.reason == "DATA_IN must read pin_in" and report.model == "small-model-1"
    assert [r.model for r in fake.requests] == ["small-model-1"]
    assert "the RTL behind the failing self-check" in report.suggestion


def test_an_unsure_small_answer_goes_to_the_large_model(tinysoc: AppContext) -> None:
    fake = FakeProvider([_reply("rtl", 0.4), _reply("tb", 0.9, "the tb expects the wrong value")])
    report = run_triage(tinysoc, SIM_LOG, backend=LlmDecideBackend(fake, MODELS))
    assert (report.label, report.backend, report.model) == ("tb", "large", "large-model-1")
    assert [r.model for r in fake.requests] == ["small-model-1", "large-model-1"]


def test_an_unsure_large_answer_is_low_confidence_advice(tinysoc: AppContext) -> None:
    fake = FakeProvider([_reply("rtl", 0.3), _reply("tb", 0.35)])
    report = run_triage(tinysoc, SIM_LOG, backend=LlmDecideBackend(fake, MODELS))
    assert report.status == "decided" and report.low_confidence
    assert (report.label, report.backend) == ("tb", "large")


def test_the_question_shows_the_spec_lines_not_rtl_or_tb(tinysoc: AppContext) -> None:
    fake = FakeProvider([_reply("rtl", 0.95)])
    report = run_triage(tinysoc, SIM_LOG, backend=LlmDecideBackend(fake, MODELS))
    [request] = fake.requests
    prompt = request.messages[-1].content
    assert PROMPT.splitlines()[0] in prompt
    assert all(f'"{label}"' in prompt for label in LABELS)
    assert "FAIL DATA_IN with pin_in=0x5a" in prompt
    assert "Spec lines" in prompt and "doc/specs/TINY_GPIO_MAS.md" in prompt
    assert report.spec_lines
    for line in report.spec_lines:
        assert not line.location.startswith(("rtl/", "tb/")), line
    assert request.labels == ("internal",)


def test_a_model_never_sees_an_nda_log(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(tmp_path / "t", profile_extra="data:\n  nda_paths: ['secret/**']\n")
    fake = FakeProvider([_reply("rtl", 0.95)])
    report = run_triage(
        ctx, SIM_LOG, source="secret/run.log", backend=LlmDecideBackend(fake, MODELS)
    )
    assert report.status == "undecided" and "nda" in report.message
    assert fake.requests == []
    # Rules still answer: they run here, not in a cloud model.
    ruled = run_triage(
        ctx, RTL_LOG, source="secret/run.log", backend=LlmDecideBackend(fake, MODELS)
    )
    assert (ruled.label, ruled.backend) == ("rtl", "rule")


def test_the_api_runtime_uses_the_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = (
        "runtime: generic\nmodels:\n  providers:\n    fake: {}\n"
        "  tiers:\n    small: fake-small\n    large: fake-large\n"
    )
    ctx = ingested_tinysoc(tmp_path / "t", profile_extra=extra)
    fake = FakeProvider([_reply("rtl", 0.9)])
    monkeypatch.setattr(run_mod, "make_provider", lambda profile, name=None: fake)
    assert isinstance(default_backend(ctx), LlmDecideBackend)
    report = run_triage(ctx, SIM_LOG)
    assert (report.label, report.backend, report.model) == ("rtl", "small", "fake-small")


def test_the_api_runtime_without_a_provider_is_rules_only(tmp_path: Path) -> None:
    ctx = ingested_tinysoc(tmp_path / "t", profile_extra="runtime: generic\n")
    assert default_backend(ctx) is None
    assert run_triage(ctx, SIM_LOG).status == "undecided"
    assert run_triage(ctx, RTL_LOG).label == "rtl"


# --- runtime claude-code: deferred, then answered through the queue ---------------------------


def test_claude_code_defers_and_picks_up_the_answers(tinysoc: AppContext) -> None:
    assert isinstance(default_backend(tinysoc), ClaudeCodeDecideBackend)
    log = SIM_LOG.replace("0xa5", "0xa6")  # a question of its own
    first = run_triage(tinysoc, log)
    assert first.status == "deferred" and first.label is None
    assert "/chipgraph:triage" in first.message and first.model == "haiku"

    [entry] = [
        d
        for d in pending_decisions(tinysoc.layout)["decisions"]
        if d["question_id"] == first.question_id
    ]
    assert (entry["agent"], entry["tier"], entry["model"]) == (DECIDER_AGENT, "small", "haiku")
    assert entry["choices"] == list(LABELS) and "FAIL DATA_IN" in entry["prompt"]

    # The small model is unsure: the next triage asks the large one.
    answer_decision(tinysoc.layout, first.question_id, "rtl", 0.5, "maybe")
    second = run_triage(tinysoc, log)
    assert second.status == "deferred" and second.model == "opus"
    answer_decision(tinysoc.layout, first.question_id, "RTL", 0.9, "spec says pin_in")
    third = run_triage(tinysoc, log)
    assert (third.status, third.label, third.backend) == ("decided", "rtl", "large")
    assert third.reason == "spec says pin_in" and third.model == "opus"


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_triage_a_log_file(tinysoc: AppContext) -> None:
    log = tinysoc.root / "lint.log"
    log.write_text(RTL_LOG)
    result = CliRunner().invoke(app, ["-C", str(tinysoc.root), "triage", str(log)])
    assert result.exit_code == 0, result.output
    assert "label: rtl (rule rtl_lint, confidence 1.00)" in result.output
    assert "rtl/tiny_gpio.sv:51" in result.output


def test_cli_triage_stdin_json(tinysoc: AppContext) -> None:
    result = CliRunner().invoke(
        app, ["-C", str(tinysoc.root), "triage", "-", "--check", "lint", "--json"], input=INFRA_LOG
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["label"], payload["check_id"]) == ("infra", "lint")
    assert payload["retry_without_counting"] is True


def test_cli_triage_deferred_exits_1(tinysoc: AppContext) -> None:
    result = CliRunner().invoke(
        app, ["-C", str(tinysoc.root), "triage", "-"], input=SIM_LOG.replace("0xa5", "0xa7")
    )
    assert result.exit_code == 1
    assert "/chipgraph:triage" in result.output


def test_cli_triage_missing_file_exits_2(tinysoc: AppContext) -> None:
    result = CliRunner().invoke(app, ["-C", str(tinysoc.root), "triage", "nope.log"])
    assert result.exit_code == 2
    assert "cannot read the log" in result.output


# --- MCP ----------------------------------------------------------------------------------------


def _call(root: Path, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async def _run() -> tuple[bool, Any]:
        async with Client(build_server(root)) as client:
            result = await client.call_tool(tool, args)
            if result.is_error:
                return True, " ".join(getattr(c, "text", "") for c in result.content)
            return False, result.structured_content

    return asyncio.run(_run())


def test_mcp_triage_runs_the_decider_loop(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "t")
    (root / "logs").mkdir()
    (root / "logs" / "run.log").write_text(SIM_LOG)

    error, report = _call(root, "triage", {"path": "logs/run.log"})
    assert not error, report
    assert report["status"] == "deferred" and report["source"] == "logs/run.log"
    error, pending = _call(root, "pending_decisions", {})
    [entry] = pending["decisions"]
    assert entry["question_id"] == report["question_id"]
    error, recorded = _call(
        root,
        "answer_decision",
        {"question_id": entry["question_id"], "value": "rtl", "confidence": 0.95},
    )
    assert not error and recorded["status"] == "answered"
    error, final = _call(root, "triage", {"path": "logs/run.log"})
    assert not error, final
    assert (final["status"], final["label"], final["backend"]) == ("decided", "rtl", "small")


def test_mcp_triage_with_text_and_a_rule(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "t")
    error, report = _call(root, "triage", {"log": SPEC_LOG})
    assert not error, report
    assert (report["label"], report["backend"]) == ("spec", "rule")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "exactly one"),
        ({"path": "a.log", "log": "x"}, "exactly one"),
        ({"path": "../outside.log"}, "outside the project"),
        ({"path": "missing.log"}, "no log file"),
    ],
)
def test_mcp_triage_refuses_bad_arguments(
    tmp_path: Path, args: dict[str, Any], message: str
) -> None:
    root = copy_tinysoc(tmp_path / "t")
    (tmp_path / "outside.log").write_text(RTL_LOG)
    error, text = _call(root, "triage", args)
    assert error and message in text


# --- plugin ---------------------------------------------------------------------------------------


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    _, head, body = path.read_text().split("---", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data, body


def test_the_triage_command_uses_the_engine_and_the_decider_loop() -> None:
    meta, body = _frontmatter(PLUGIN / "commands" / "triage.md")
    tools = [t.strip() for t in meta["allowed-tools"].split(",")]
    assert tools == ["Agent", TRIAGE, PENDING, ANSWER]
    assert "argument-hint" in meta
    for needle in ("chipgraph:decider", "pending_decisions", "answer_decision", "deferred"):
        assert needle in body
    assert "/chipgraph:decide" in body


def test_the_decide_command_is_the_decider_loop() -> None:
    meta, body = _frontmatter(PLUGIN / "commands" / "decide.md")
    tools = [t.strip() for t in meta["allowed-tools"].split(",")]
    assert tools == ["Agent", PENDING, ANSWER]
    assert "parallel" in body and "`model`" in body
    assert "until none is pending" in meta["description"]
    assert "Never answer a question yourself" in body
