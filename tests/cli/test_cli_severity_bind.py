"""`adapters.<id>.severity` caps a check's issues, and `waive --bind` handles file-less findings."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import init_git, write_profile
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()

_FAILING_LINT = (
    "  lint:\n"
    "    use: cmd\n"
    "    cmd:\n"
    "      - python3\n"
    "      - -c\n"
    "      - \"import sys; print('ERR design/m_cnt.sv:3: latch inferred'); sys.exit(1)\"\n"
    "    regex: '^ERR (?P<file>\\S+):(?P<line>\\d+): (?P<msg>.*)$'\n"
)
# A check that cannot run and reports no file (like a `make` target that does not exist).
_BROKEN = "  broken:\n    use: cmd\n    cmd: [python3, -c, 'import sys; sys.exit(2)']\n"


def _project(tmp_path: Path, adapters: str) -> Path:
    init_git(tmp_path)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "m_cnt.sv").write_text("module m_cnt; endmodule\n")
    write_profile(tmp_path, "project: demo\nadapters:\n" + adapters)
    return tmp_path


def _json(tmp_path: Path, *args: str) -> object:
    result = runner.invoke(app, ["--json", "-C", str(tmp_path), *args])
    return json.loads(result.output), result.exit_code


def test_severity_warning_reports_but_does_not_fail(tmp_path: Path) -> None:
    root = _project(tmp_path, _FAILING_LINT + "    severity: warning\n")
    (results,) = _json(root, "check", "--only", "lint")[0]
    assert results["status"] == "pass"
    assert [i["severity"] for i in results["issues"]] == ["warning"]
    rows, _ = _json(root, "findings")
    assert [(r["severity"], r["claim"]) for r in rows] == [("warning", "latch inferred")]


def test_without_severity_the_check_still_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, _FAILING_LINT)
    (results,) = _json(root, "check", "--only", "lint")[0]
    assert results["status"] == "fail"
    assert results["issues"][0]["severity"] == "error"


def test_severity_does_not_hide_a_check_that_could_not_run(tmp_path: Path) -> None:
    root = _project(tmp_path, _BROKEN + "    severity: warning\n")
    (results,) = _json(root, "check", "--only", "broken")[0]
    assert results["status"] in ("error", "fail")
    assert results["status"] != "pass"


def _only_finding(root: Path) -> str:
    rows, _ = _json(root, "findings", "--status", "all")
    assert len(rows) == 1
    assert rows[0]["evidence"][0].get("file") is None  # a whole-check finding
    return str(rows[0]["id"])


def test_a_finding_without_a_file_needs_bind(tmp_path: Path) -> None:
    root = _project(tmp_path, _BROKEN)
    runner.invoke(app, ["-C", str(root), "check", "--only", "broken"])
    result = runner.invoke(
        app, ["-C", str(root), "waive", _only_finding(root), "--reason", "not there yet"]
    )
    assert result.exit_code != 0
    assert "--bind" in result.output


def test_bound_waiver_ends_when_the_missing_file_appears(tmp_path: Path) -> None:
    root = _project(tmp_path, _BROKEN)
    runner.invoke(app, ["-C", str(root), "check", "--only", "broken"])
    finding = _only_finding(root)
    waived = runner.invoke(
        app,
        [
            "-C",
            str(root),
            "waive",
            finding,
            "--reason",
            "block not merged yet",
            "--bind",
            "design/syscsr/syscsr.f",
        ],
    )
    assert waived.exit_code == 0, waived.output
    rows, _ = _json(root, "findings", "--status", "all")
    assert rows[0]["status"] == "waived"

    (root / "design" / "syscsr").mkdir()
    (root / "design" / "syscsr" / "syscsr.f").write_text("rtl/m_qnsc_wrap_syscsr.sv\n")
    rows, _ = _json(root, "findings", "--status", "all")
    assert rows[0]["status"] == "open"  # the bound file appeared: look again


def test_bound_waiver_ends_when_an_existing_file_changes(tmp_path: Path) -> None:
    root = _project(tmp_path, _BROKEN)
    runner.invoke(app, ["-C", str(root), "check", "--only", "broken"])
    finding = _only_finding(root)
    runner.invoke(
        app,
        [
            "-C",
            str(root),
            "waive",
            finding,
            "--reason",
            "top RTL not merged",
            "--bind",
            "design/m_cnt.sv",
        ],
    )
    rows, _ = _json(root, "findings", "--status", "all")
    assert rows[0]["status"] == "waived"
    (root / "design" / "m_cnt.sv").write_text("module m_cnt(input logic i_a); endmodule\n")
    rows, _ = _json(root, "findings", "--status", "all")
    assert rows[0]["status"] == "open"
