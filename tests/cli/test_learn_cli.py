"""CLI tests for `chipgraph learn`, `chipgraph try`, and `chipgraph init --from-learn`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from chipgraph.cli import app

_EXAMPLE_TINYSOC = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _git_init_main(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _prefixed_repo(root: Path) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/blk_timer.sv").write_text(
        "// SPDX-License-Identifier: Apache-2.0\n"
        "module blk_timer(input logic i_clk, output logic o_irq);\n"
        "  logic r_count;\n"
        "  blk_sub u_sub(.i_clk(i_clk));\n"
        "  assign o_irq = r_count;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    _git_init_main(root)
    return root


def test_learn_prints_coverage_and_draft(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    result = CliRunner().invoke(app, ["learn", str(repo)])
    assert result.exit_code == 0, result.output
    assert "layout (kind: template" in result.output
    assert "naming (kind: pattern" in result.output
    assert "draft profile:" in result.output


def test_learn_json_is_machine_readable(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    result = CliRunner().invoke(app, ["--json", "learn", str(repo)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project"] == "repo"
    assert any(rule["kind"] == "port" for rule in payload["naming"])


def test_learn_out_writes_draft_files_and_not_into_repo(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["learn", str(repo), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "chipgraph.draft.yml").is_file()
    assert (out / "chipgraph.draft.naming.yml").is_file()
    assert not (repo / ".chipgraph.yml").exists()


def test_try_runs_read_only(tmp_path: Path) -> None:
    dest = tmp_path / "tinysoc"
    shutil.copytree(_EXAMPLE_TINYSOC, dest)
    _git_init_main(dest)
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout
    result = CliRunner().invoke(app, ["try", str(dest)])
    assert result.exit_code == 0, result.output
    assert "read-only" in result.output
    assert "files with issues:" in result.output
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout
    assert before == after


def test_try_json_reports_audit_status(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    result = CliRunner().invoke(app, ["--json", "try", str(repo)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["audit_available"] is False
    assert payload["audit"] == "audit not available yet"


def test_init_from_learn_writes_profile(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    result = CliRunner().invoke(app, ["-C", str(repo), "init", "--from-learn"])
    assert result.exit_code == 0, result.output
    profile_path = repo / ".chipgraph.yml"
    assert profile_path.is_file()
    assert "inferred from" in result.output
    # The learned naming rules land under .chipgraph/ and the profile is valid.
    check = CliRunner().invoke(app, ["-C", str(repo), "config", "check"])
    assert check.exit_code == 0, check.output


def test_init_from_learn_refuses_to_overwrite(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    (repo / ".chipgraph.yml").write_text("project: existing\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["-C", str(repo), "init", "--from-learn"])
    assert result.exit_code == 2
    assert "already exists" in result.output
