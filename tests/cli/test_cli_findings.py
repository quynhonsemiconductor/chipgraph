"""CLI tests for `chipgraph findings` and `chipgraph waive` (M1-18)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import init_git, write_profile
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()

_LINT_PROFILE = (
    "project: demo\n"
    "adapters:\n"
    "  lint:\n"
    "    use: cmd\n"
    "    cmd:\n"
    "      - python3\n"
    "      - -c\n"
    "      - \"import sys; print('ERR design/m_cnt.sv:3: latch inferred'); sys.exit(1)\"\n"
    "    regex: '^ERR (?P<file>\\S+):(?P<line>\\d+): (?P<msg>.*)$'\n"
)


def _write_lint_project(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; endmodule\n")
    write_profile(tmp_path, _LINT_PROFILE)


def _finding_id(tmp_path: Path) -> str:
    listed = runner.invoke(app, ["--json", "-C", str(tmp_path), "findings", "--status", "all"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert len(rows) == 1
    return str(rows[0]["id"])


def test_help_lists_findings_and_waive_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "findings" in result.output
    assert "waive" in result.output


def test_check_then_findings_lists_them(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)

    checked = runner.invoke(app, ["-C", str(tmp_path), "check"])
    assert checked.exit_code == 1  # the lint check fails

    listed = runner.invoke(app, ["-C", str(tmp_path), "findings"])
    assert listed.exit_code == 0, listed.output
    assert "latch inferred" in listed.output
    assert "design/m_cnt.sv:3" in listed.output


def test_findings_json_is_a_list_of_finding_dumps(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)
    runner.invoke(app, ["-C", str(tmp_path), "check"])

    listed = runner.invoke(app, ["--json", "-C", str(tmp_path), "findings"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["claim"] == "latch inferred"
    assert rows[0]["evidence"][0]["file"] == "design/m_cnt.sv"
    assert rows[0]["status"] == "open"


def test_waive_then_findings_status_waived(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)
    runner.invoke(app, ["-C", str(tmp_path), "check"])
    finding_id = _finding_id(tmp_path)

    waived = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "waive",
            finding_id,
            "--reason",
            "tracked in JIRA-42",
            "--by",
            "tester",
        ],
    )
    assert waived.exit_code == 0, waived.output

    open_list = runner.invoke(app, ["-C", str(tmp_path), "findings", "--status", "open"])
    assert finding_id not in open_list.output

    waived_list = runner.invoke(app, ["-C", str(tmp_path), "findings", "--status", "waived"])
    assert finding_id in waived_list.output

    decisions_dir = tmp_path / ".chipgraph" / "decisions"
    assert list(decisions_dir.glob("*.yml"))


def test_editing_file_expires_waiver_back_to_open(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)
    runner.invoke(app, ["-C", str(tmp_path), "check"])
    finding_id = _finding_id(tmp_path)

    runner.invoke(
        app, ["-C", str(tmp_path), "waive", finding_id, "--reason", "tracked", "--by", "tester"]
    )
    waived_list = runner.invoke(app, ["-C", str(tmp_path), "findings", "--status", "waived"])
    assert finding_id in waived_list.output

    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; wire a; endmodule\n")

    open_list = runner.invoke(app, ["-C", str(tmp_path), "findings", "--status", "open"])
    assert finding_id in open_list.output


def test_waive_unknown_finding_exits_2(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nadapters: {}\n")
    result = runner.invoke(app, ["-C", str(tmp_path), "waive", "F-deadbeef", "--reason", "n/a"])
    assert result.exit_code == 2


def test_waive_requires_reason(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)
    runner.invoke(app, ["-C", str(tmp_path), "check"])
    finding_id = _finding_id(tmp_path)

    result = runner.invoke(app, ["-C", str(tmp_path), "waive", finding_id])
    assert result.exit_code == 2


def test_findings_filter_by_layer(tmp_path: Path) -> None:
    _write_lint_project(tmp_path)
    runner.invoke(app, ["-C", str(tmp_path), "check"])

    layer4 = runner.invoke(app, ["-C", str(tmp_path), "findings", "--layer", "4"])
    assert "latch inferred" in layer4.output

    layer1 = runner.invoke(app, ["-C", str(tmp_path), "findings", "--layer", "1"])
    assert "latch inferred" not in layer1.output
