"""M2-04: the acceptance report (`docs/rtl-claude-code/report.py`) on synthetic runs.

No model runs: for each exemplar set a stream-json is made up as Claude Code would write
it, with the subagent's calls and the real `get_context` answer, and the module is
written (in a given style) and submitted. A clean pair of runs passes and prints both
distances; a read of a testbench, testbench text in a tool result, or a module in the
other set's style fails that run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rtl_author_helpers import (
    OUTPUT,
    RTL_DOCS,
    TASK,
    fx,
    get_context,
    load_module,
    make_project,
    next_task,
    profile_of,
    render_pwm,
    submit,
    write,
)

report = load_module("rtl_acceptance_report", RTL_DOCS / "report.py")
NAME = report.CHIPGRAPH_MCP + "get_context"


def _event(content: dict, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [content]}}


def _result(use_id: str, text: str, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": text}]},
    }


def _one_run(out: Path, name: str, *, style: str, extra: list[dict] | None = None) -> None:
    proj = make_project(out / name / "tinysoc", exemplars=name)
    events: list[dict] = [{"type": "system", "subtype": "init", "plugins": []}]
    answer = next_task(proj)
    events.append(_event({"type": "tool_use", "id": "m1", "name": "next_task", "input": {}}))
    events.append(_result("m1", json.dumps(answer)))
    agent = {
        "subagent_type": "chipgraph:rtl-author",
        "model": "sonnet",
        "prompt": f"You are doing chipgraph task {TASK!r}.",
    }
    events.append(_event({"type": "tool_use", "id": "a1", "name": "Agent", "input": agent}))
    context = get_context(proj)
    events.append(_event({"type": "tool_use", "id": "c1", "name": NAME, "input": {}}, "a1"))
    events.append(_result("c1", json.dumps(context), "a1"))
    for item in extra or []:
        events.append(_event(item["use"], "a1"))
        events.append(_result(item["use"]["id"], item["result"], "a1"))
    text = render_pwm(profile_of(style))
    write(proj, OUTPUT, text)
    events.append(
        _event(
            {
                "type": "tool_use",
                "id": "w1",
                "name": "Write",
                "input": {"file_path": str(proj / OUTPUT), "content": text},
            },
            "a1",
        )
    )
    events.append(_result("w1", "ok", "a1"))
    submitted = submit(proj)
    events.append(_event({"type": "tool_use", "id": "m2", "name": "submit", "input": {}}))
    events.append(_result("m2", json.dumps(submitted)))
    (out / name / "stream.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _runs(tmp_path: Path, **kw: dict) -> Path:
    out = tmp_path / "cg-rtl-test"
    for name in fx.EXEMPLAR_SETS:
        options = kw.get(name, {})
        _one_run(out, name, style=options.get("style", name), extra=options.get("extra"))
    return out


@pytest.fixture(autouse=True)
def _no_lint_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report's re-run of lint and naming, without needing Verilator here."""
    real = report.rerun_checks

    def rerun(proj: Path, checks: tuple[str, ...] = ("naming",)) -> dict[str, str]:
        return {"lint": "pass", **real(proj, ("naming",))}

    monkeypatch.setattr(report, "rerun_checks", rerun)


def test_a_clean_pair_of_runs_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = _runs(tmp_path)
    code = report.main(out)
    text = capsys.readouterr().out
    assert code == 0, text
    assert text.count("task accepted: True") == 2
    assert text.count("read no testbench and saw none") == 2
    assert "(d) style distance to set a: 0.0000; to set b: 0." in text
    assert "(d) style distance to set b: 0.0000; to set a: 0." in text
    assert text.rstrip().endswith("PASS")


def test_a_read_of_a_testbench_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = {
        "use": {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "x"}},
        "result": "refused",
    }
    leak = {
        "use": {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": []}},
        "result": f"# {fx.FINGERPRINT}: dv/test_tiny_gpio.py",
    }
    out = _runs(tmp_path, a={"extra": [read]}, b={"extra": [leak]})
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "- Read" in text and "holds testbench text" in text
    assert f"{TASK} with exemplars a: FAIL" in text and f"{TASK} with exemplars b: FAIL" in text


def test_a_module_in_the_other_style_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _runs(tmp_path, b={"style": "a"})
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "follows its own exemplars: False" in text
    assert f"{TASK} with exemplars a: PASS" in text and f"{TASK} with exemplars b: FAIL" in text
