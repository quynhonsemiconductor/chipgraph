"""M2-04: the `rtl` section of an `rtl-author` task's context, built from a tinysoc plan with
dependencies (the `timer` fixture plan: a counter, an irq module, and the top that
instantiates both)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plan_helpers import tinysoc_project, write_plan

from chipgraph.app.build import load_rules
from chipgraph.app.context import AppContext
from chipgraph.core.model import ModelStore, default_model_db_path
from chipgraph.packs.digital_rtl.rtl.context import approved_plan_path, rtl_author_context


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tinysoc_project(tmp_path_factory.mktemp("rtl-context") / "tinysoc")
    write_plan(root, "timer")
    (root / "plan" / "timer.approved.json").write_text(json.dumps({"plan": "plan/timer.plan.yml"}))
    return root


def _section(root: Path, module: str | None, denied: tuple[str, ...] = ()) -> dict:  # type: ignore[type-arg]
    ctx = AppContext.load(root)
    model = ModelStore(default_model_db_path(root)).read()
    return rtl_author_context(
        root,
        ctx.require_profile(),
        load_rules(ctx),
        block="timer",
        module=module,
        model=model,
        is_denied=lambda path: path in denied,
    )


def test_the_top_gets_its_dependencies_as_declarations(project: Path) -> None:
    rtl = _section(project, "tiny_timer")
    assert rtl["module"]["depends_on"] == ["tiny_timer_counter", "tiny_timer_irq"]
    deps = {d["name"]: d for d in rtl["dependencies"]}
    assert set(deps) == {"tiny_timer_counter", "tiny_timer_irq"}
    counter = deps["tiny_timer_counter"]
    assert counter["source"] == "plan"  # a new module: the approved plan's declaration
    assert [p["name"] for p in counter["ports"]] == [
        "clk",
        "rst_n",
        "en",
        "load",
        "load_value",
        "count",
    ]
    assert "body" not in json.dumps(counter)
    ports = rtl["interface"]["ports"]
    assert {p["source"] for p in ports} == {"model"} and rtl["interface"]["top"] is True
    assert [r["id"] for r in rtl["requirements"]] == ["REQ-TIM-005"]


def test_an_internal_module_gets_its_plan_ports(project: Path) -> None:
    rtl = _section(project, "tiny_timer_counter")
    sources = {p["name"]: p["source"] for p in rtl["interface"]["ports"]}
    assert sources["load_value"] == "plan" and sources["count"] == "plan"
    assert rtl["interface"]["top"] is False and rtl["dependencies"] == []
    assert [r["id"] for r in rtl["requirements"]] == ["REQ-TIM-001", "REQ-TIM-002"]


def test_without_a_plan_entry_it_says_so(project: Path) -> None:
    rtl = _section(project, None)
    assert "no approved plan entry" in rtl["note"] and "module" not in rtl
    assert {r["id"] for r in rtl["requirements"]} >= {"REQ-TIM-001"}
    assert _section(project, "tiny_nope")["note"].startswith("module 'tiny_nope' is not in")
    assert approved_plan_path(project, "gpio") is None


def test_a_denied_exemplar_is_refused_with_a_message(project: Path) -> None:
    rtl = _section(project, "tiny_timer", denied=("rtl/tiny_gpio.sv",))
    by_path = {e["path"]: e for e in rtl["style"]["exemplars"]}
    assert "content" not in by_path["rtl/tiny_gpio.sv"]
    assert "must not read" in by_path["rtl/tiny_gpio.sv"]["refused"]
    assert by_path["rtl/tiny_timer.sv"]["content"].startswith("// SPDX")
