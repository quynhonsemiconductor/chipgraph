"""M2-12: the acceptance report (`docs/wrapper-claude-code/report.py`) on synthetic runs.

No model runs: a stream-json is made up as Claude Code would write it, with the subagent's
calls and the real `get_context` answer; the source is written (the sample's) and
submitted, and the generator runs as `run.sh` runs it after the session. A clean run
passes; a read, a changed vendor file, a source the subagent did not write, or a missing
expansion fails it. The clean case needs emacs and skips without.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rtl_author_helpers import load_module
from wrapper_helpers import (
    HAS_VERILATOR,
    REPO,
    create,
    get_context,
    needs_emacs,
    next_task,
    source_text,
    submit,
)

DOCS = REPO / "docs" / "wrapper-claude-code"
report = load_module("wrapper_acceptance_report", DOCS / "report.py")
fx = report._fx
CONTEXT = report.CHIPGRAPH_MCP + "get_context"


def _event(content: dict, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [content]}}


def _result(use_id: str, text: str, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": text}]},
    }


def _call(events: list[dict], use_id: str, name: str, args: dict, result: str, parent: str) -> None:
    events.append(_event({"type": "tool_use", "id": use_id, "name": name, "input": args}, parent))
    events.append(_result(use_id, result, parent))


def _run(
    out: Path,
    *,
    text: str | None = None,
    extra: list[tuple[str, dict, str]] | None = None,
    write: bool = True,
    generate: bool = True,
    subagent: str = "chipgraph:rtl-author",
) -> Path:
    proj = fx.make_wrapper_project(out / "tinysoc")
    events: list[dict] = [{"type": "system", "subtype": "init", "plugins": []}]
    answer = next_task(proj, fx.TASK)
    events.append(_event({"type": "tool_use", "id": "m1", "name": "next_task", "input": {}}))
    events.append(_result("m1", json.dumps(answer)))
    agent = {"subagent_type": subagent, "model": "sonnet", "prompt": f"task {fx.TASK!r}"}
    events.append(_event({"type": "tool_use", "id": "a1", "name": "Agent", "input": agent}))
    _call(events, "c1", CONTEXT, {}, json.dumps(get_context(proj, fx.TASK)), "a1")
    for i, (name, args, result) in enumerate(extra or []):
        _call(events, f"x{i}", name, args, result, "a1")
    source = text if text is not None else source_text()
    create(proj, fx.SRC, source)
    if write:
        args = {"file_path": str(proj / fx.SRC), "content": source}
        _call(events, "w1", "Write", args, "ok", "a1")
    answer = submit(proj, fx.TASK)
    events.append(_event({"type": "tool_use", "id": "m2", "name": "submit", "input": {}}))
    events.append(_result("m2", json.dumps(answer)))
    events.append(
        {"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.1234}
    )
    (out / "stream.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if generate:
        import asyncio

        from chipgraph.app.build import make_scheduler
        from chipgraph.app.context import AppContext

        ctx = AppContext.load(proj)
        asyncio.run(make_scheduler(ctx, fx.GEN_TASK).run(fx.GEN_TASK))
    return proj


@pytest.fixture(autouse=True)
def _no_lint_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report's re-run of the checks, without needing Verilator here."""
    if HAS_VERILATOR:
        return
    real = report.rerun_checks

    def rerun(proj: Path, checks: tuple[str, ...] = ("wrap_check", "naming")) -> dict[str, str]:
        return {"lint": "pass", **real(proj, ("wrap_check", "naming"))}

    monkeypatch.setattr(report, "rerun_checks", rerun)


@needs_emacs
def test_a_clean_run_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out)
    code = report.main(out)
    text = capsys.readouterr().out
    assert code == 0, text
    assert "task accepted: True" in text and "written by the subagent: True" in text
    assert "wrap_check', 'lint'" in text or "'wrap_check': 'pass'" in text
    assert "(c) vendor files byte-identical to vendor/MANIFEST.sha256: True" in text
    assert "used only get_context and Write: True" in text
    assert "check wrap_check: pass" in text  # the checks of each attempt are printed
    assert "cost: $0.1234" in text
    assert text.rstrip().endswith("PASS")


def test_a_read_or_a_shell_fails_the_tools_criterion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out, extra=[("Read", {"file_path": "vendor/vendor_fifo/vendor_fifo.sv"}, "refused")])
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "used only get_context and Write: False" in text and "- Read" in text
    assert text.rstrip().endswith("FAIL")


def test_a_bash_call_fails_the_tools_criterion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out, extra=[("Bash", {"command": "emacs --batch"}, "denied")])
    assert report.main(out) == 1
    assert "- Bash" in capsys.readouterr().out


def test_another_subagent_fails_the_tools_criterion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out, subagent="general-purpose")
    assert report.main(out) == 1
    assert "used only get_context and Write: False" in capsys.readouterr().out


def test_a_source_the_subagent_did_not_write_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out, write=False)
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert (
        "written by the subagent: False" in text
        and "used only get_context and Write: False" in text
    )


def test_a_changed_vendor_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cg-wrapper-test"
    proj = _run(out, generate=False)
    ip = proj / fx.IP_FILES[0]
    ip.write_text(ip.read_text().replace("err_o", "error_o"))
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "byte-identical to vendor/MANIFEST.sha256: False" in text
    assert "vendor_guard.modified" in text and "git status of vendor/" in text


def test_a_missing_expansion_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cg-wrapper-test"
    _run(out, generate=False)
    assert report.main(out) == 1
    text = capsys.readouterr().out
    assert "passes wrap_check, lint and naming: False" in text
    assert "(c) vendor files byte-identical to vendor/MANIFEST.sha256: True" in text


def test_a_run_that_did_not_start_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert report.main(tmp_path / "cg-wrapper-none") == 1
    assert "the run did not start" in capsys.readouterr().out


def test_the_run_script_refuses_an_out_dir_outside_tmp_and_prints_its_gates() -> None:
    import subprocess

    done = subprocess.run(
        ["bash", str(DOCS / "run.sh"), "/var/tmp/not-allowed"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2 and "refusing OUT_DIR" in done.stderr
    script = (DOCS / "run.sh").read_text()
    assert (
        'MAIN_MODEL="${MAIN_MODEL:-sonnet}"' in script
        and 'MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"' in script
    )
    assert "/chipgraph:run digital-rtl/wrapper" in script and "ANTHROPIC_API_KEY" in script
