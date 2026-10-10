"""M2-06: the `dv/tb_module` rule through the runtime claude-code tools, in process, on
the acceptance fixture (`docs/tb-claude-code/fixture.py`: tinysoc with the `dv` pack).
No model: the test plays the subagent's part.

The permission tests: the tb-author task gets no rtl-kind input and denies every RTL
path; its agent has no read tools; its context holds the interface, the spec, the
requirements and the skill, and no RTL body text whichever source the interface came
from. A marker planted in the RTL body (a comment, a signal name, a parameter used only
inside) never reaches any `next_task`, `get_context` or `submit` answer, the redo text or
the task record; a port declared with a marker name may.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from tb_author_helpers import (
    BODY_MARKERS,
    GOOD_GPIO_TEST,
    GPIO,
    PORT_MARKER,
    REPO,
    TIMER,
    fx,
    get_context,
    leaks,
    next_task,
    plant_markers,
    record,
    submit,
    write,
)

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.core.engine.agent_rule import WITHHELD_NOTE
from chipgraph.core.runtime.roles import get_role

GPIO_OUT = fx.OUTPUTS["gpio"]


def _project(tmp_path: Path, *, ingest: bool = True, port: bool = True) -> Path:
    """The fixture with the body markers planted in tiny_gpio (committed), ingested."""
    root = fx.make_tb_project(tmp_path / "tinysoc", ingest=False, blocks=fx.BLOCKS)
    plant_markers(root, port=port)
    _commit(root)
    if ingest:
        run_ingest(AppContext.load(root))
    return root


def _commit(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "commit", "-qam", "markers"], cwd=root, check=True)


def _tasks(answer: dict) -> dict[str, dict]:
    return {t["task_id"]: t for t in answer["tasks"]}


# --- dispatch ----------------------------------------------------------------------------


def test_each_block_gets_a_tb_author_task(tmp_path: Path) -> None:
    root = _project(tmp_path)
    tasks = _tasks(next_task(root))
    assert set(tasks) == {GPIO, TIMER}
    gpio = tasks[GPIO]
    assert (gpio["agent"], gpio["role"], gpio["skills"]) == (
        "chipgraph:tb-author",
        "tb-author",
        ["dv/cocotb"],
    )
    assert gpio["outputs"] == [GPIO_OUT]


def test_the_runtime_gives_the_tb_author_no_rtl(tmp_path: Path) -> None:
    root = _project(tmp_path)
    next_task(root)
    rec = record(root, GPIO)
    assert {"rtl/tiny_gpio.sv", "rtl/tiny_timer.sv", "rtl/**"} <= set(rec.denied_reads)
    context = get_context(root, GPIO)
    kinds = [i.get("kind") for i in context["inputs"] if "path" in i]
    assert "rtl" not in kinds and kinds == ["spec"]
    assert not any(str(i.get("path", "")).startswith("rtl/") for i in context["inputs"])
    assert "no file-reading tools" in context["instructions"]


def test_the_tb_author_agent_has_no_read_tools() -> None:
    role = get_role("tb-author")
    assert not role.can_read_files and role.read_policy.deny_kinds == ("rtl",)
    agent = (REPO / "plugin" / "agents" / "tb-author.md").read_text()
    head = agent.split("\n---\n", 1)[0]
    tools = re.search(r"^tools:\s*(.+)$", head, re.MULTILINE)
    assert tools is not None
    for read_tool in ("Read", "Glob", "Grep", "Bash", "LS"):
        assert not re.search(rf"\b{read_tool}\b", tools.group(1)), read_tool


# --- the context ----------------------------------------------------------------------------


def test_the_context_has_the_interface_spec_requirements_and_skill(tmp_path: Path) -> None:
    root = _project(tmp_path, port=False)
    next_task(root)
    context = get_context(root, GPIO)
    iface = context["interface"]
    assert (iface["module"], iface["source"], iface["clock"], iface["reset"]) == (
        "tiny_gpio",
        "spec",
        "clk",
        "rst_n",
    )
    assert [p["name"] for p in iface["ports"]] == [
        "clk", "rst_n", "addr", "wr_en", "wdata", "rdata", "pin_in", "pin_out", "pin_dir",
    ]  # fmt: skip
    assert all(p["source"] == "spec" and "note" not in p for p in iface["ports"])
    assert [r["id"] for r in context["requirements"]] == [
        "REQ-GPIO-001",
        "REQ-GPIO-002",
        "REQ-GPIO-003",
    ]
    [spec] = [i for i in context["inputs"] if i.get("kind") == "spec"]
    assert spec["path"] == "doc/specs/TINY_GPIO_MAS.md" and "REQ-GPIO-001" in spec["content"]
    block = json.loads(context["inputs"][0]["content"])
    assert {r["name"] for r in block["registers"]} == {"DATA_OUT", "DATA_IN", "DIR"}
    assert "modules" not in block and "ports" not in block
    assert "dv/cocotb" in context["skill_texts"]
    assert "`interface`" in context["instructions"]
    assert not leaks(context)
    assert "tiny_gpio.sv" not in json.dumps(context)


def test_a_port_the_rtl_declares_beyond_the_spec_is_noted_by_name(tmp_path: Path) -> None:
    root = _project(tmp_path)
    next_task(root)
    iface = get_context(root, GPIO)["interface"]
    assert PORT_MARKER not in {p["name"] for p in iface["ports"]}
    assert any(PORT_MARKER in n and "ports_diff" in n for n in iface["notes"])


def test_the_interface_from_the_model_s_rtl_ports(tmp_path: Path) -> None:
    root = fx.make_tb_project(tmp_path / "t", ingest=False, blocks=fx.BLOCKS)
    plant_markers(root)
    (root / "doc" / "specs" / "TINY_GPIO_MAS.md").unlink()  # no spec ports for gpio
    _commit(root)
    run_ingest(AppContext.load(root))
    next_task(root)
    context = get_context(root, GPIO)
    iface = context["interface"]
    assert iface["source"] == "model_rtl"
    assert {p["source"] for p in iface["ports"]} == {"model_rtl"}
    assert PORT_MARKER in {p["name"] for p in iface["ports"]}  # a declaration: allowed
    assert not leaks(context)


def test_the_interface_from_the_declaration_without_a_model(tmp_path: Path) -> None:
    root = _project(tmp_path, ingest=False)
    answer = next_task(root)
    context = get_context(root, GPIO)
    iface = context["interface"]
    assert (iface["source"], iface["module"]) == ("rtl_declaration", "tiny_gpio")
    names = [p["name"] for p in iface["ports"]]
    assert names[-1] == PORT_MARKER and "pin_dir" in names
    assert {p["source"] for p in iface["ports"]} == {"rtl_declaration"}
    assert next(p for p in iface["ports"] if p["name"] == "wdata")["packed"] == "[31:0]"
    assert "requirements" not in context  # no model, no requirement list
    assert not leaks(context) and not leaks(answer)
    assert "line" not in json.dumps(iface)


# --- submit and the redo text ---------------------------------------------------------------

_FAKE_SIM = """\
import sys

print("%Warning-UNUSEDSIGNAL: rtl/tiny_gpio.sv:31:21: Signal is not used: 'zqx_body_signal'")
print("   31 |   logic [ZQX_BODY_PARAM:0] zqx_body_signal;")
print("      |                            ^~~~~~~~~~~~~~~")
print("%Error: rtl/tiny_gpio.sv:29:3: syntax error near ZQXBODYCOMMENT")
print("        ... in ROOT/rtl/tiny_gpio.sv, see zqx_body_signal")
print("   29 |   // ZQXBODYCOMMENT: a comment in the body")
print("%Error: Exiting due to 1 error(s)")
print("dv/gpio/test_gpio.py:51: AssertionError: REQ-GPIO-001: pin_out expected 0xa5, got 0x0")
sys.exit(1)
"""


def _with_fake_sim(root: Path) -> None:
    """`tb_static.sim`: a command printing a Verilator-like log that quotes the RTL body."""
    write(root, "scripts/fake_sim.py", _FAKE_SIM.replace("ROOT", str(root.resolve())))
    profile_path = root / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["adapters"]["tb_static"]["sim"] = {
        "use": "cmd",
        "cmd": ["python3", "scripts/fake_sim.py"],
        "parser": "verilator",
    }
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    _commit_all(root)


def _commit_all(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fake sim"], cwd=root, check=True)


@pytest.mark.parametrize("ingest", [True, False], ids=["model", "declaration"])
def test_no_answer_leaks_the_rtl_body(tmp_path: Path, ingest: bool) -> None:
    root = _project(tmp_path, ingest=ingest)
    _with_fake_sim(root)
    seen: list[object] = [next_task(root)]
    seen.append(get_context(root, GPIO))
    write(root, GPIO_OUT, GOOD_GPIO_TEST)
    rejected = submit(root, GPIO)
    seen.append(rejected)
    assert rejected["status"] == "rejected", rejected["reasons"]
    redo = rejected["redo"]
    assert "pin_out expected 0xa5, got 0x0" in redo  # behavioural text stays
    assert WITHHELD_NOTE in redo
    assert "tiny_gpio.sv" not in redo and "   31 |" not in redo
    rec = record(root, GPIO)
    seen.extend([rec.model_dump(mode="json"), rec.state.redo])
    seen.append(next_task(root))
    again = get_context(root, GPIO)
    seen.append(again)
    assert again["previous_rejection"] == [redo]
    for answer in seen:
        assert not leaks(answer), leaks(answer)
    # the engine itself kept the full results, for a person (journal, not the agent)
    journal = (root / ".chipgraph" / "state" / "runs").rglob("journal.jsonl")
    assert any(BODY_MARKERS[1] in p.read_text() for p in journal)


def test_a_good_test_is_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path, port=False)
    next_task(root)
    get_context(root, GPIO)
    write(root, GPIO_OUT, GOOD_GPIO_TEST)
    answer = submit(root, GPIO)
    assert answer["accepted"] is True, (answer["reasons"], answer["checks"])


def test_tb_static_failures_are_shown_in_full(tmp_path: Path) -> None:
    root = _project(tmp_path, port=False)
    next_task(root)
    get_context(root, GPIO)
    write(root, GPIO_OUT, GOOD_GPIO_TEST + "    dut.out_q.value = 1\n    open('rtl/x.sv')\n")
    answer = submit(root, GPIO)
    assert answer["status"] == "rejected"
    [check] = answer["failed_checks"]
    rules = [i["rule"] for i in check["issues"]]
    assert "tb_static.unknown_port" in rules and "tb_static.forbidden" in rules
    assert all(i["at"].startswith(GPIO_OUT) for i in check["issues"])
    assert "dut.out_q" in answer["redo"]
