"""M2-01: roles through the runtime claude-code MCP tools, in process, on the acceptance
fixture (`docs/roles-claude-code/fixture.py`: an `author` task and a `tb-author` task on
a tinysoc copy). No model: the test plays the subagents' part.

Covers choosing the role's subagent and model, escalating after a rejection, the
`tb-author` task's `denied_reads` and context (no RTL), skills in the context, and the
rule validator (unknown role, unknown or misused skill, an rtl input for `tb-author`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from mcp import Client
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout
from chipgraph.mcp.server import build_server

REPO = Path(__file__).resolve().parents[3]


def _load_fixture() -> ModuleType:
    path = REPO / "docs" / "roles-claude-code" / "fixture.py"
    spec = importlib.util.spec_from_file_location("roles_acceptance_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["roles_acceptance_fixture"] = module
    spec.loader.exec_module(module)
    return module


fx = _load_fixture()
RTL_TEXT = ("always_ff", "module tiny_gpio (")  # in rtl/tiny_gpio.sv, not in the spec


class Session:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.server = build_server(root)

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

    def error(self, tool: str, args: dict[str, Any] | None = None) -> str:
        result = self.call(tool, args)
        assert result.is_error, result.structured_content
        return " ".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return fx.make_roles_project(tmp_path / "tinysoc")


def _record(root: Path, task_id: str) -> Any:
    return TaskQueue(StateLayout(root)).require(task_id)


def _edit_rule(root: Path, name: str, **changes: Any) -> None:
    path = root / ".chipgraph" / "packs" / "roles" / "rules" / f"{name}.yml"
    rule = yaml.safe_load(path.read_text())
    rule.update(changes)
    path.write_text(yaml.safe_dump(rule, sort_keys=False))


def _tasks(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["task_id"]: t for t in answer["tasks"]}


def test_each_task_gets_its_role_s_subagent_and_model(project: Path) -> None:
    tasks = _tasks(Session(project).ok("next_task", {"target": fx.TARGET}))
    assert set(tasks) == {fx.AUTHOR_TASK, fx.TB_TASK}
    author, tb = tasks[fx.AUTHOR_TASK], tasks[fx.TB_TASK]
    assert (author["agent"], author["model"], author["tier"]) == (
        "chipgraph:author",
        "sonnet",
        "medium",
    )
    assert (tb["agent"], tb["model"], tb["tier"]) == ("chipgraph:tb-author", "sonnet", "medium")
    assert tb["role"] == "tb-author"


def test_the_profile_maps_the_role_tiers(tmp_path: Path) -> None:
    root = fx.make_roles_project(tmp_path / "t", tiers={"medium": "haiku", "large": "sonnet"})
    tasks = _tasks(Session(root).ok("next_task", {"target": fx.TARGET}))
    assert {t["model"] for t in tasks.values()} == {"haiku"}


def test_escalation_after_a_rejected_attempt(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": fx.TARGET})
    rejected = s.ok("submit", {"task_id": fx.AUTHOR_TASK})  # no output written
    assert rejected["status"] == "rejected"
    again = _tasks(s.ok("next_task", {"target": fx.TARGET}))[fx.AUTHOR_TASK]
    assert (again["tier"], again["model"], again["attempt"]) == ("large", "opus", 2)


def test_tb_author_task_denies_the_rtl(project: Path) -> None:
    Session(project).ok("next_task", {"target": fx.TARGET})
    tb = _record(project, fx.TB_TASK)
    assert {"rtl/tiny_gpio.sv", "rtl/tiny_timer.sv", "rtl/tiny_top.sv", "rtl/**"} <= set(
        tb.denied_reads
    )
    assert fx.TB_OUTPUT not in tb.denied_reads
    assert _record(project, fx.AUTHOR_TASK).denied_reads == ()


def test_tb_author_context_has_no_rtl(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": fx.TARGET})
    context = s.ok("get_context", {"task_id": fx.TB_TASK})
    text = json.dumps(context)
    assert not any(snippet in text for snippet in RTL_TEXT)
    assert [i["path"] for i in context["inputs"]] == [fx.SPEC, fx.TB_TASK_FILE]
    assert "no file-reading tools" in context["instructions"]
    assert context["role"] == "tb-author" and context["skill_texts"] == {}
    author = s.ok("get_context", {"task_id": fx.AUTHOR_TASK})
    assert "You may read other project files" in author["instructions"]


def test_both_tasks_run_and_the_build_goes_on(project: Path) -> None:
    s = Session(project)
    s.ok("next_task", {"target": fx.TARGET})
    for task_id, output, text in (
        (fx.AUTHOR_TASK, fx.AUTHOR_OUTPUT, "- pin_out: out, 8\n"),
        (fx.TB_TASK, fx.TB_OUTPUT, "async def test_ports(dut):\n    pass\n"),
    ):
        s.ok("get_context", {"task_id": task_id})
        (project / output).parent.mkdir(parents=True, exist_ok=True)
        (project / output).write_text(text)
        answer = s.ok("submit", {"task_id": task_id})
        assert answer["accepted"] is True, (answer["reasons"], answer["checks"])
    assert s.ok("next_task", {"target": fx.TARGET})["done"] is True


def test_an_input_on_a_denied_path_is_refused(project: Path) -> None:
    (project / "rtl" / "NOTES.md").write_text("internal notes about the RTL\n")
    _edit_rule(project, "gpio_port_test", inputs=[{"spec": fx.SPEC}, {"path": "rtl/NOTES.md"}])
    s = Session(project)
    s.ok("next_task", {"target": fx.TARGET})
    message = s.error("get_context", {"task_id": fx.TB_TASK})
    assert "must not see" in message and "rtl/NOTES.md" in message
    assert "internal notes" not in message
    assert _record(project, fx.TB_TASK).status == "needs_human"
    after = s.ok("next_task", {"target": fx.TARGET})
    assert fx.TB_TASK not in {t["task_id"] for t in after["tasks"] + after["in_progress"]}


def test_an_unknown_role_is_rejected(project: Path) -> None:
    _edit_rule(project, "gpio_summary", role="writer")
    message = Session(project).error("next_task", {"target": fx.TARGET})
    assert "unknown role 'writer'" in message
    result = CliRunner().invoke(app, ["-C", str(project), "build", fx.TARGET])
    assert result.exit_code != 0 and "unknown role 'writer'" in result.output


def test_a_tb_author_rule_with_an_rtl_input_is_rejected(project: Path) -> None:
    _edit_rule(project, "gpio_port_test", inputs=[{"spec": fx.SPEC}, {"path": fx.RTL}])
    message = Session(project).error("next_task", {"target": fx.TARGET})
    assert "'tb-author' must not see 'rtl'" in message and fx.RTL in message


def _add_skill_pack(root: Path, roles: list[str]) -> None:
    pack = root / ".chipgraph" / "packs" / "roles_skills"
    (pack / "skills").mkdir(parents=True)
    (pack / "pack.yml").write_text(
        "name: roles-skills\nversion: 0.1.0\nprovides:\n  skills: [skills]\n"
    )
    (pack / "skills" / "cocotb.md").write_text(
        f"---\nid: test-dv/cocotb\ndescription: cocotb tests.\nroles: {json.dumps(roles)}\n"
        "version: 0.1.0\n---\nOne test per requirement.\n"
    )
    profile_path = root / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = [*profile["packs"], "roles-skills"]
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))


def test_get_context_returns_the_skill_text(project: Path) -> None:
    _add_skill_pack(project, ["tb-author"])
    _edit_rule(project, "gpio_port_test", skills=["test-dv/cocotb"])
    s = Session(project)
    tasks = _tasks(s.ok("next_task", {"target": fx.TARGET}))
    assert tasks[fx.TB_TASK]["skills"] == ["test-dv/cocotb"]
    context = s.ok("get_context", {"task_id": fx.TB_TASK})
    assert context["skill_texts"] == {"test-dv/cocotb": "One test per requirement.\n"}
    assert "skill_texts" in context["instructions"]


def test_a_skill_not_for_the_role_is_rejected(project: Path) -> None:
    _add_skill_pack(project, ["tb-author"])
    _edit_rule(project, "gpio_summary", skills=["test-dv/cocotb"])
    message = Session(project).error("next_task", {"target": fx.TARGET})
    assert "'test-dv/cocotb' is not for role 'author'" in message


def test_an_unknown_skill_is_rejected(project: Path) -> None:
    _edit_rule(project, "gpio_summary", skills=["lang-sv/rtl"])
    message = Session(project).error("next_task", {"target": fx.TARGET})
    assert "unknown skill 'lang-sv/rtl'" in message
