"""M1-17: `chipgraph eval` (fake runtime: no model)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()


def test_eval_runs_a_suite_and_writes_the_report(tmp_path: Path) -> None:
    out = tmp_path / "report"
    result = runner.invoke(app, ["eval", "triage", "--only", "log-01,log-05", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "chipgraph eval `triage`: PASS" in result.output
    assert f"report: {out}" in result.output
    names = {p.name for p in out.iterdir()}
    assert names == {"answers.jsonl", "logs", "summary.json", "summary.md"}
    assert len(list((out / "logs").glob("*.json"))) == 1


def test_eval_json_prints_the_summary(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--json", "eval", "ask", "--only", "q01 q16", "--out", str(tmp_path / "r")]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["suite"] == "ask" and summary["verdict"] == "PASS"
    assert [i["id"] for i in summary["items"]] == ["q01", "q16"]


def test_eval_defaults_to_a_temporary_report_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = runner.invoke(app, ["eval", "triage", "--only", "log-02"])
    assert result.exit_code == 0, result.output
    out = Path(result.output.rsplit("report: ", 1)[1].strip())
    assert out.parent == tmp_path and out.name.startswith("cg-eval-triage-")
    assert (out / "summary.json").is_file()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["eval", "nope"], "unknown suite"),
        (["eval", "triage", "--only", "log-99"], "no such id"),
    ],
)
def test_eval_errors_exit_2(args: list[str], message: str, tmp_path: Path) -> None:
    result = runner.invoke(app, [*args, "--out", str(tmp_path / "r")])
    assert result.exit_code == 2
    assert message in result.output


def test_eval_without_inspect_ai_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "inspect_ai", None)  # `import inspect_ai` now fails
    result = runner.invoke(app, ["eval", "ask", "--out", str(tmp_path / "r")])
    assert result.exit_code == 2
    assert "needs Inspect AI" in result.output and "uv sync --extra evals" in result.output
    assert not (tmp_path / "r").exists()
