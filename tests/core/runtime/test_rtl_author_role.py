"""M2-04: the `rtl-author` role as data (the mirror of `tb-author`), its rendered agent
file, its read policy over a project, and the feedback filter for it."""

from __future__ import annotations

import re
from pathlib import Path

from chipgraph.adapters.runtime.claude_code import agents
from chipgraph.core.contracts import CheckResult, Issue
from chipgraph.core.engine.agent_rule import (
    TB_WITHHELD_NOTE,
    WITHHELD_NOTE,
    FeedbackFilter,
    render_feedback,
    withheld_note,
)
from chipgraph.core.runtime.roles import denied_kind, denied_reads, get_role, path_denied

REPO = Path(__file__).resolve().parents[3]


def test_the_role_mirrors_the_tb_author() -> None:
    rtl, tb = get_role("rtl-author"), get_role("tb-author")
    for field in ("default_tier", "escalate_to", "tools", "write_scope", "shell", "dispatch"):
        assert getattr(rtl, field) == getattr(tb, field), field
    assert (rtl.default_tier, rtl.escalate_to, rtl.write_scope) == ("medium", "large", "outputs")
    assert rtl.tools == ("write_outputs", "engine_context") and not rtl.can_read_files
    assert rtl.read_policy.mode == "deny" and rtl.read_policy.deny_kinds == ("tb",)
    assert {"test_*.py", "*/test_*.py", "tb_*.sv", "*_tb.sv"} <= set(rtl.read_policy.deny_globs)
    assert "{tool:read_files}" not in rtl.prompt and "{tool:engine_context}" in rtl.prompt
    assert "never from the testbench" in rtl.prompt


def test_the_agent_file_is_the_render_and_has_no_read_tool() -> None:
    path = REPO / "plugin" / "agents" / "rtl-author.md"
    assert path.read_text() == agents.render_agent(get_role("rtl-author"))
    head = path.read_text().split("\n---\n", 1)[0]
    tools = re.search(r"^tools:\s*(.+)$", head, re.MULTILINE)
    assert tools is not None
    assert [t.strip() for t in tools.group(1).split(",")] == [
        "Write",
        "Edit",
        "MultiEdit",
        f"{agents.MCP_TOOL_PREFIX}get_context",
    ]
    assert "rtl-author" in agents.RENDERED_ROLES
    assert agents.agent_type("rtl-author") == "chipgraph:rtl-author"


def test_testbench_files_are_denied_by_name_and_by_layout() -> None:
    role = get_role("rtl-author")
    for path in ("dv/test_gpio.py", "test_top.py", "sim/tb_gpio.sv", "x/gpio_tb.sv", "a/b/tb_c.py"):
        assert denied_kind(role, "script" if path.endswith(".py") else "rtl", path) == "tb", path
    for path in ("rtl/tiny_gpio.sv", "doc/specs/A.md", "scripts/gen_manifest.py", "rtl/tbx.sv"):
        assert denied_kind(role, "rtl", path) is None, path
    denied = denied_reads(
        role,
        artifacts=[],
        layout={"tb": "design/{block}/dv/**", "rtl": "design/{block}/rtl/*.sv"},
        block_layouts={"gpio": {}, "uart": {}},
    )
    assert "design/gpio/dv/**" in denied and "design/uart/dv/**" in denied
    assert not any("rtl" in d for d in denied)  # the RTL author may read RTL
    assert path_denied("design/gpio/dv/helpers.sv", denied)
    assert not path_denied("design/gpio/rtl/m_qnsc_gpio.sv", denied)


def test_the_feedback_filter_for_the_rtl_author() -> None:
    shown = FeedbackFilter.for_role("rtl-author", denied=("dv/**",), root="/p")
    assert shown is not None
    assert shown.kinds == ("tb",) and shown.placeholder == "<tb>"
    assert "dv/**" in shown.denied and "*/test_*.py" in shown.denied
    assert shown.note == TB_WITHHELD_NOTE
    result = CheckResult(
        check_id="sim",
        status="fail",
        issues=(
            Issue(file="dv/gpio/test_gpio.py", line=9, msg="assert dut.x == 2  # in the test"),
            Issue(msg="test_reads failed: rdata expected 0x2, got 0x0"),
            Issue(file="rtl/tiny_pwm.sv", line=4, msg="  12 |   assign x = y;"),
        ),
        log_tail="/p/dv/gpio/test_gpio.py:9: in test_reads\nrdata expected 0x2, got 0x0\n",
        duration_s=0.0,
        idempotency_key="k",
    )
    [visible], withheld = shown.apply([result])
    assert [i.msg for i in visible.issues] == ["test_reads failed: rdata expected 0x2, got 0x0"]
    assert visible.log_tail == "rdata expected 0x2, got 0x0"
    assert withheld == 3
    redo = render_feedback([visible], "verification", withheld=withheld, withheld_note=shown.note)
    assert "rdata expected 0x2, got 0x0" in redo and redo.endswith(TB_WITHHELD_NOTE)
    assert "test_gpio" not in redo


def test_each_side_gets_its_own_note() -> None:
    assert withheld_note(("rtl",)) == WITHHELD_NOTE
    assert withheld_note(()) == WITHHELD_NOTE
    assert withheld_note(("tb",)) == TB_WITHHELD_NOTE
    assert "sva artifacts" in withheld_note(("sva",))
    tb = FeedbackFilter.for_role("tb-author")
    assert tb is not None and tb.note == WITHHELD_NOTE  # unchanged for the testbench Author
    assert render_feedback([], "verification", withheld=1).endswith(WITHHELD_NOTE)
