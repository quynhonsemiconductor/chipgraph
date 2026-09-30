"""decide() in runtime claude-code (M1-12): deferred questions, the MCP tools
`pending_decisions` and `answer_decision`, and the `chipgraph:decider` subagent.

No model: the test plays the decider subagent by calling `answer_decision` itself.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from chipgraph.adapters.llm import NdaBlocked
from chipgraph.adapters.llm.decide_backend import question_prompt
from chipgraph.adapters.runtime.claude_code.decisions import (
    DECIDER_AGENT,
    ClaudeCodeDecideBackend,
    DecisionQueue,
    DecisionStateError,
)
from chipgraph.core.config import load
from chipgraph.core.config.models import DecideCfg, ModelsCfg
from chipgraph.core.contracts import Decision
from chipgraph.core.engine.decide import (
    DecisionLog,
    Deferred,
    ModelBackend,
    Question,
    decide,
)
from chipgraph.core.state.layout import StateLayout
from chipgraph.mcp.server import build_server

PLUGIN = Path(__file__).resolve().parents[3] / "plugin"
QID = "triage.sim.1"


def _project(root: Path, profile: str = "project: demo\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / ".chipgraph.yml").write_text(profile)
    return root


def _question(**kw: object) -> Question:
    fields: dict[str, Any] = {
        "id": QID,
        "prompt": "Why did the simulation fail?",
        "choices": ("infra", "rtl", "tb", "spec"),
        "context": "ERROR: tb_timer.sv:40 assertion irq_expected failed at 120ns",
    }
    fields.update(kw)
    return Question(**fields)


class Project:
    """One tmp project: decide() with the claude-code backend, and the MCP tools."""

    def __init__(self, root: Path, models: ModelsCfg | None = None) -> None:
        self.root = root
        self.layout = StateLayout(root)
        self.backend = ClaudeCodeDecideBackend(self.layout, models)
        self.log = DecisionLog.for_layout(self.layout)
        self.server = build_server(root)

    def decide(self, question: Question, cfg: DecideCfg | None = None) -> Decision | Deferred:
        return asyncio.run(decide(question, backend=self.backend, cfg=cfg, log=self.log))

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        async def _call() -> Any:
            async with Client(self.server) as client:
                return await client.call_tool(tool, args or {})

        return asyncio.run(_call())

    def ok(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.call(tool, args)
        assert not result.is_error, result.content
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    def pending(self) -> list[dict[str, Any]]:
        decisions = self.ok("pending_decisions")["decisions"]
        assert isinstance(decisions, list)
        return decisions

    def answer(self, value: str, confidence: float, reason: str = "r") -> dict[str, Any]:
        return self.ok(
            "answer_decision",
            {"question_id": QID, "value": value, "confidence": confidence, "reason": reason},
        )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(_project(tmp_path / "proj"))


def test_the_tools_are_listed(project: Project) -> None:
    async def _names() -> set[str]:
        async with Client(project.server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert {"pending_decisions", "answer_decision"} <= asyncio.run(_names())


def test_pending_then_answer_then_decide_returns_it(project: Project) -> None:
    assert project.pending() == []
    question = _question()

    deferred = project.decide(question)
    assert deferred == Deferred(
        question_id=QID, tier="small", model="haiku", message=deferred.message
    )
    assert "pending_decisions" in deferred.message
    state = project.root / ".chipgraph" / "state" / "runtime" / "decisions"
    assert len(list(state.glob("*.json"))) == 1

    [entry] = project.pending()
    assert entry["question_id"] == QID
    assert (entry["tier"], entry["model"], entry["agent"]) == ("small", "haiku", DECIDER_AGENT)
    assert entry["choices"] == ["infra", "rtl", "tb", "spec"]
    assert entry["prompt"] == question_prompt(question)
    assert "assertion irq_expected failed" in entry["prompt"]

    # a second decide() before the answer queues nothing new
    assert isinstance(project.decide(question), Deferred)
    assert len(project.pending()) == 1

    recorded = project.answer("TB", 0.9, "the testbench assertion fired")
    assert recorded == {
        "question_id": QID,
        "tier": "small",
        "value": "tb",
        "confidence": 0.9,
        "status": "answered",
    }
    assert project.pending() == []

    decision = project.decide(question)
    assert decision == Decision(question_id=QID, value="tb", confidence=0.9, backend="small")
    events = [(e.event, e.tier, e.model) for e in project.log.read()]
    assert events == [
        ("deferred", "small", "haiku"),
        ("deferred", "small", "haiku"),
        ("decided", "small", "haiku"),
    ]
    # asking again returns the same recorded answer
    assert project.decide(question) == decision


def test_low_confidence_is_requeued_for_the_large_model(project: Project) -> None:
    question = _question()
    project.decide(question)
    project.answer("rtl", 0.4)

    deferred = project.decide(question)
    assert isinstance(deferred, Deferred)
    assert (deferred.tier, deferred.model) == ("large", "opus")
    [entry] = project.pending()
    assert (entry["tier"], entry["model"]) == ("large", "opus")

    project.answer("tb", 0.85)
    decision = project.decide(question)
    assert decision == Decision(question_id=QID, value="tb", confidence=0.85, backend="large")
    record = DecisionQueue(project.layout).get(QID)
    assert record is not None
    assert {t: a.value for t, a in record.answers.items()} == {"small": "rtl", "large": "tb"}
    assert [e.event for e in project.log.read()] == [
        "deferred",
        "escalated",
        "deferred",
        "escalated",
        "decided",
    ]


def test_low_confidence_stays_small_when_large_is_disabled(project: Project) -> None:
    question = _question()
    cfg = DecideCfg(enabled_tiers=("small",))
    project.decide(question, cfg)
    project.answer("rtl", 0.4)
    decision = project.decide(question, cfg)
    assert isinstance(decision, Decision)
    assert (decision.backend, decision.confidence) == ("small", 0.4)
    assert project.log.read()[-1].low_confidence
    assert project.pending() == []


def test_answer_decision_validates(project: Project) -> None:
    unknown = project.call(
        "answer_decision", {"question_id": "nope", "value": "rtl", "confidence": 1.0}
    )
    assert unknown.is_error and "no queued question" in unknown.content[0].text

    project.decide(_question())
    for value, confidence, message in (
        ("timing", 0.9, "is not a choice"),
        ("rtl", 1.5, "between 0 and 1"),
    ):
        result = project.call(
            "answer_decision",
            {"question_id": QID, "value": value, "confidence": confidence},
        )
        assert result.is_error and message in result.content[0].text
    assert len(project.pending()) == 1  # still waiting

    project.answer("rtl", 0.9)
    again = project.call("answer_decision", {"question_id": QID, "value": "rtl", "confidence": 1})
    assert again.is_error and "not waiting" in again.content[0].text


def test_a_changed_question_starts_afresh(project: Project) -> None:
    project.decide(_question())
    project.answer("rtl", 0.95)
    changed = _question(context="ERROR: license checkout failed")
    deferred = project.decide(changed)
    assert isinstance(deferred, Deferred) and deferred.tier == "small"
    [entry] = project.pending()
    assert "license checkout failed" in entry["prompt"]


def test_an_nda_question_is_never_queued(project: Project) -> None:
    with pytest.raises(NdaBlocked):
        project.decide(_question(labels=("nda",)))
    assert project.pending() == []
    assert project.log.read()[0].event == "error"


def test_the_profile_models_name_the_subagent_model(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "proj",
        "project: demo\nmodels:\n  tiers:\n    small: claude-haiku-4-5\n",
    )
    resolved = load(root)
    assert resolved is not None
    project = Project(root, resolved.profile.models)
    deferred = project.decide(_question())
    assert isinstance(deferred, Deferred) and deferred.model == "claude-haiku-4-5"
    assert project.pending()[0]["model"] == "claude-haiku-4-5"
    assert project.backend.model_for("large") == "opus"


def test_the_queue_rejects_answers_directly(tmp_path: Path) -> None:
    queue = DecisionQueue(StateLayout(tmp_path))
    with pytest.raises(DecisionStateError):
        queue.answer(QID, "rtl", 0.5)
    assert queue.all() == []


def test_the_backend_is_a_model_backend(tmp_path: Path) -> None:
    assert isinstance(ClaudeCodeDecideBackend(StateLayout(tmp_path)), ModelBackend)


def _frontmatter(path: Path) -> dict[str, Any]:
    _, head, body = path.read_text().split("---", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    data["_body"] = body
    return data


def test_the_decider_agent_is_small_and_has_no_write_or_shell() -> None:
    meta = _frontmatter(PLUGIN / "agents" / "decider.md")
    assert meta["name"] == "decider"
    assert f"{meta['name']}" == DECIDER_AGENT.split(":", 1)[1]
    assert meta["model"] == "haiku"
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert tools == ["mcp__plugin_chipgraph_chipgraph__pending_decisions"]
    assert '"value"' in meta["_body"] and '"confidence"' in meta["_body"]
