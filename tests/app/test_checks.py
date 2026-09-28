"""Tests for `chipgraph.app.checks.ProfileCheckRunner`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import init_git, make_instance, write_profile

from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.core.state.findings import FindingStore


def test_check_runner_maps_to_cmd_tool(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        'project: demo\nadapters:\n  hello:\n    use: cmd\n    cmd: "python3 -c \\"print(1)\\""\n',
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("hello", make_instance()))

    assert result.status == "pass"
    assert result.check_id == "hello"


def test_check_runner_maps_to_layout_check(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "design" / "a").mkdir(parents=True)
    (tmp_path / "design" / "a" / "m_a.sv").write_text("module m_a; endmodule\n")
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  layout:\n"
        "    use: layout\n"
        "    scope: ['design/**']\n"
        "    templates:\n"
        "      rtl: 'design/{block}/m_{block}.sv'\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("layout", make_instance()))

    assert result.status == "pass"


def test_check_runner_unknown_check_id(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters:\n  lint:\n    use: cmd\n")
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("nope", make_instance()))

    assert result.status == "error"
    assert "nope" in result.issues[0].msg
    assert "lint" in result.issues[0].msg


def test_check_runner_caches_cmd_result_on_resume(tmp_path: Path) -> None:
    """A `cmd` check's key is computable before running (`CmdTool.key_for`), so a
    resumed run (same `run_id`) reuses the cached result instead of re-running it.
    """
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  count:\n"
        "    use: cmd\n"
        "    cmd: [python3, -c, \"open('counter.txt', 'a').write('x')\"]\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx, run_id="run-1")
    counter = tmp_path / "counter.txt"

    result1 = asyncio.run(runner.run("count", make_instance()))
    assert result1.status == "pass"
    assert counter.read_text(encoding="utf-8") == "x"

    result2 = asyncio.run(runner.run("count", make_instance()))
    assert result2.status == "pass"
    assert counter.read_text(encoding="utf-8") == "x"  # not re-run: cached, not appended again


def test_check_runner_reruns_when_an_output_changed(tmp_path: Path) -> None:
    """Editing a file of the instance between a kill and `resume` runs the check again."""
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  count:\n"
        "    use: cmd\n"
        "    cmd: [python3, -c, \"open('counter.txt', 'a').write('x')\"]\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx, run_id="run-1")
    report = tmp_path / "out" / "report.json"
    report.parent.mkdir()
    report.write_text("1", encoding="utf-8")

    asyncio.run(runner.run("count", make_instance()))
    report.write_text("2", encoding="utf-8")
    asyncio.run(runner.run("count", make_instance()))

    assert (tmp_path / "counter.txt").read_text(encoding="utf-8") == "xx"


def test_check_runner_never_reuses_a_failure(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  flaky:\n"
        "    use: cmd\n"
        "    cmd: [python3, -c, \"open('counter.txt', 'a').write('x'); raise SystemExit(1)\"]\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx, run_id="run-1")

    assert not asyncio.run(runner.run("flaky", make_instance())).ok
    assert not asyncio.run(runner.run("flaky", make_instance())).ok
    assert (tmp_path / "counter.txt").read_text(encoding="utf-8") == "xx"


def test_check_runner_unknown_adapter_name(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters:\n  lint:\n    use: does-not-exist\n")
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("lint", make_instance()))

    assert result.status == "error"
    assert "does-not-exist" in result.issues[0].msg


def test_check_runner_records_findings_with_file_line_evidence(tmp_path: Path) -> None:
    """A failing `cmd` check with a regex parser (M1-18) should leave a `Finding` behind,
    with the parsed file:line as its evidence, at the layer 4 lint checks belong to.
    """
    init_git(tmp_path)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; endmodule\n")
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  lint:\n"
        "    use: cmd\n"
        "    cmd:\n"
        "      - python3\n"
        "      - -c\n"
        "      - \"import sys; print('ERR design/m_cnt.sv:3: latch inferred'); sys.exit(1)\"\n"
        "    regex: '^ERR (?P<file>\\S+):(?P<line>\\d+): (?P<msg>.*)$'\n",
    )
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    result = asyncio.run(runner.run("lint", make_instance()))

    assert not result.ok
    assert result.issues[0].file == "design/m_cnt.sv"

    store = FindingStore(ctx.layout)
    findings = store.list()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.layer == 4
    assert finding.source == "check:lint"
    assert finding.evidence[0].file == "design/m_cnt.sv"
    assert finding.evidence[0].line == 3
    assert finding.claim == "latch inferred"


def test_check_runner_records_findings_for_unconfigured_check(tmp_path: Path) -> None:
    """Even a check-configuration error (no file involved) is recorded as a finding,
    with the check id itself as the evidence's model key.
    """
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters: {}\n")
    ctx = AppContext.load(tmp_path)
    runner = ProfileCheckRunner(ctx)

    asyncio.run(runner.run("nope", make_instance()))

    store = FindingStore(ctx.layout)
    (finding,) = store.list()
    assert finding.evidence[0].model_key == "check:nope"
