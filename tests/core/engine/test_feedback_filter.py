"""M2-06: `FeedbackFilter`, what a role that must not see RTL is shown of a failed check.

Each rule on its own, a Verilator build log with RTL excerpts (the M2-05 fixture log,
parsed by the real parser), `render_feedback`'s withheld note, and the in-process agent
loop: a `tb-author` task's redo text holds no RTL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_rule_helpers import FakeRuntime, Step, result

from chipgraph.adapters.parser.verilator import VerilatorParser
from chipgraph.core.contracts import Budget, CheckResult, InputSpec, Issue, RuleSpec
from chipgraph.core.contracts.rule import RuleInstance
from chipgraph.core.engine.agent_rule import (
    WITHHELD_NOTE,
    AgentRuleExecutor,
    FeedbackFilter,
    render_feedback,
)
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules

REPO = Path(__file__).resolve().parents[3]
BUILD_LOG = REPO / "tests" / "adapters" / "parser" / "logs" / "cocotb" / "verilator_build_error.log"
TB = "dv/gpio/test_gpio.py"
SHOWN = FeedbackFilter(denied=("rtl/tiny_gpio.sv", "rtl/**"), kinds=("rtl",), root="/proj")


def _one(**kw: object) -> CheckResult:
    return result("tb_static", "fail", **kw)  # type: ignore[arg-type]


def test_only_a_role_with_a_deny_policy_gets_a_filter() -> None:
    assert FeedbackFilter.for_role("author") is None
    assert FeedbackFilter.for_role(None) is None
    tb = FeedbackFilter.for_role("tb-author", denied=("rtl/**",), root="/p")
    assert tb is not None and tb.kinds == ("rtl",) and tb.denied == ("rtl/**",)
    assert tb.placeholder == "<rtl>"


def test_denies_by_path_glob_kind_and_absolute_path() -> None:
    assert SHOWN.denies("rtl/tiny_gpio.sv")
    assert SHOWN.denies("rtl/sub/notes.md")  # under a denied directory
    assert SHOWN.denies("/proj/rtl/x.txt")  # absolute under the root
    assert SHOWN.denies("/elsewhere/build/src/broken.sv")  # an RTL file anywhere
    assert SHOWN.denies("./ip/vendor.svh")
    assert not SHOWN.denies(TB)
    assert not SHOWN.denies("/proj/dv/gpio/test_gpio.py")
    assert not SHOWN.denies(None)


def test_an_issue_in_a_denied_file_is_dropped() -> None:
    res = _one(
        issues=(
            Issue(file="rtl/tiny_gpio.sv", line=30, rule="lint/x", msg="secret_q unused"),
            Issue(file=TB, line=12, rule="cocotb/AssertionError", msg="expected 0x2, got 0x0"),
        )
    )
    [shown], withheld = SHOWN.apply([res])
    assert withheld == 1
    assert [i.msg for i in shown.issues] == ["expected 0x2, got 0x0"]


def test_a_denied_path_in_a_message_is_replaced() -> None:
    res = _one(
        issues=(Issue(file=TB, line=5, msg="dut handle from /proj/rtl/tiny_gpio.sv:14 has no x"),)
    )
    [shown], withheld = SHOWN.apply([res])
    assert shown.issues[0].msg == "dut handle from <rtl>:14 has no x"
    assert withheld == 1


def test_source_excerpt_lines_are_cut_from_messages() -> None:
    msg = (
        "syntax error, unexpected ';'\n"
        "   12 |   assign zq_secret = ;\n"
        "      |              ^\n"
        "  ^~~~\n"
        "expected 0x2, got 0x0"
    )
    [shown], withheld = SHOWN.apply([_one(issues=(Issue(file=TB, line=3, msg=msg),))])
    assert shown.issues[0].msg == "syntax error, unexpected ';'\nexpected 0x2, got 0x0"
    assert "zq_secret" not in shown.issues[0].msg
    assert withheld == 3


def test_a_message_that_is_only_an_excerpt_is_dropped() -> None:
    res = _one(issues=(Issue(file=TB, line=3, msg="   12 |   assign zq_secret = ;"),))
    [shown], withheld = SHOWN.apply([res])
    assert shown.issues == () and withheld == 1


def test_behavioural_text_and_testbench_issues_are_kept() -> None:
    res = _one(
        issues=(Issue(file=TB, line=9, rule="cocotb/AssertionError", msg="expected 0x2, got 0x0"),),
        log_tail="tests=1 pass=0 fail=1\nexpected 0x2, got 0x0",
    )
    [shown], withheld = SHOWN.apply([res])
    assert shown == res and withheld == 0


def test_a_verilator_build_log_with_rtl_excerpts() -> None:
    log = BUILD_LOG.read_text()
    issues = VerilatorParser().parse(log)
    assert issues and all(i.file and i.file.endswith(".sv") for i in issues)
    res = CheckResult(
        check_id="tb_static",
        status="error",
        issues=issues,
        log_tail=log,
        duration_s=0.0,
        idempotency_key="k",
    )
    [shown], withheld = SHOWN.apply([res])
    assert shown.issues == ()
    assert withheld >= len(issues) + 3
    for gone in ("assign b = ;", "broken.sv", "   2 |", "^"):
        assert gone not in shown.log_tail, gone
    assert "%Error: Exiting due to 1 error(s)" in shown.log_tail
    assert "make: *** [Vtop.mk] Error 1" in shown.log_tail
    text = render_feedback([shown], "infra", withheld=withheld)
    assert "assign b" not in text and "broken.sv" not in text
    assert text.endswith(WITHHELD_NOTE)


def test_render_feedback_says_when_something_was_withheld() -> None:
    res = _one(issues=(Issue(file=TB, line=1, msg="bad"),))
    assert WITHHELD_NOTE not in render_feedback([res], "verification")
    noted = render_feedback([res], "verification", withheld=2)
    assert "fix the test only if the spec says so" in noted
    assert "needs_human" in noted
    capped = render_feedback([res], "verification", withheld=1, max_chars=len(WITHHELD_NOTE) + 40)
    assert capped.endswith(WITHHELD_NOTE) and len(capped) <= len(WITHHELD_NOTE) + 40


# --- the in-process loop -----------------------------------------------------------------


class _RtlQuotingChecks:
    """The first run fails quoting RTL; later runs pass."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        self.calls += 1
        if self.calls > 1:
            return result(check_id, "pass")
        return result(
            check_id,
            "fail",
            issues=(
                Issue(file="rtl/a.sv", line=7, rule="verilator/WIDTH", msg="ZQX_SECRET width"),
                Issue(file=TB, line=4, rule="cocotb/AssertionError", msg="expected 1, got 0"),
            ),
            log_tail="%Warning: rtl/a.sv:7: ZQX_SECRET\n    7 | assign ZQX_SECRET = 1;\n",
        )


def test_the_in_process_loop_filters_the_tb_author_s_redo(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("spec\n")
    rule = RuleSpec(
        id="p/tb",
        kind="agent",
        role="tb-author",
        inputs=(InputSpec(source="path", selector="spec.md"),),
        outputs=(TB,),
        checks=("sim",),
        budget=Budget(tries=3),
    )
    graph = build_graph([rule], StaticForeach({}))
    [instance] = graph.instances.values()
    runtime = FakeRuntime(tmp_path, [Step(content="import cocotb\n")])
    checks = _RtlQuotingChecks()
    executor = AgentRuleExecutor(
        runtime, store=ArtifactStore(tmp_path, LabelRules()), checks=checks
    )
    outcome = asyncio.run(executor.execute(rule, instance))
    assert outcome.ok and runtime.calls == 2
    feedback = runtime.tasks[1].context["feedback"]
    assert "ZQX_SECRET" not in feedback and "rtl/a.sv" not in feedback
    assert "expected 1, got 0" in feedback and WITHHELD_NOTE in feedback


def test_the_in_process_loop_leaves_an_author_s_redo_alone(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("spec\n")
    rule = RuleSpec(
        id="p/rtl",
        kind="agent",
        role="author",
        inputs=(InputSpec(source="path", selector="spec.md"),),
        outputs=("out/a.txt",),
        checks=("sim",),
    )
    graph = build_graph([rule], StaticForeach({}))
    [instance] = graph.instances.values()
    runtime = FakeRuntime(tmp_path, [Step(content="x\n")])
    executor = AgentRuleExecutor(
        runtime, store=ArtifactStore(tmp_path, LabelRules()), checks=_RtlQuotingChecks()
    )
    asyncio.run(executor.execute(rule, instance))
    feedback = runtime.tasks[1].context["feedback"]
    assert "ZQX_SECRET" in feedback and WITHHELD_NOTE not in feedback
