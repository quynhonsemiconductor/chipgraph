"""M2-04: the `format` check: the project's formatter run in place on a task's outputs, by
the engine; its failure paths; and which files it touches (`target_files`)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks import format as format_mod
from chipgraph.checks.format import FormatCheck, TargetError, target_files
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api.types import ToolContext

REPO = Path(__file__).resolve().parents[2]
TRIM = REPO / "docs" / "rtl-claude-code" / "fmt_trim.py"
MESSY = "module tiny_x;   \n  logic a;\t\nendmodule\n\n\n"
CLEAN = "module tiny_x;\n  logic a;\nendmodule\n"


def _project(tmp_path: Path, formatter: object | None = None) -> Path:
    root = tmp_path / "p"
    (root / "rtl").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "fmt_trim.py").write_text(TRIM.read_text())
    profile: dict[str, object] = {"project": "p", "adapters": {"format": {"use": "format"}}}
    if formatter is not None:
        profile["style"] = {"formatter": formatter}
    (root / ".chipgraph.yml").write_text(yaml.safe_dump(profile))
    (root / "rtl" / "tiny_x.sv").write_text(MESSY)
    return root


TRIM_CFG = {"use": "cmd", "cmd": ["python3", "scripts/fmt_trim.py", "{file}"]}


def _run(root: Path, args: dict[str, object], params: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    spec = CheckSpec(id="format", capability="format", adapter="format", args=args)
    ctx = ToolContext(repo_root=root, runner=LocalRunner(), params=params or {})
    return asyncio.run(FormatCheck().run(spec, ctx))


def test_the_formatter_runs_in_place(tmp_path: Path) -> None:
    root = _project(tmp_path, TRIM_CFG)
    result = _run(root, {"files": ["rtl/{module}.sv"]}, {"module": "tiny_x"})
    assert result.status == "pass", result.issues
    assert (root / "rtl" / "tiny_x.sv").read_text() == CLEAN
    assert [i.rule for i in result.issues] == ["format.changed"]
    again = _run(root, {"files": ["rtl/tiny_x.sv"]})
    assert again.status == "pass" and again.issues == ()  # nothing left to change
    assert again.idempotency_key == _run(root, {"files": ["rtl/tiny_x.sv"]}).idempotency_key


def test_an_inline_formatter_wins_over_the_profile(tmp_path: Path) -> None:
    root = _project(tmp_path, {"use": "cmd", "cmd": ["python3", "-c", "raise SystemExit(1)"]})
    result = _run(root, {"files": ["rtl/tiny_x.sv"], "formatter": TRIM_CFG})
    assert result.status == "pass" and (root / "rtl" / "tiny_x.sv").read_text() == CLEAN


def test_a_failing_formatter_is_an_error_and_the_file_is_kept(tmp_path: Path) -> None:
    breaks = (
        "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('broken'); "
        "print('cannot parse line 2'); sys.exit(3)"
    )
    root = _project(tmp_path, {"use": "cmd", "cmd": ["python3", "-c", breaks, "{file}"]})
    result = _run(root, {"files": ["rtl/tiny_x.sv"]})
    assert result.status == "error"
    [issue] = result.issues
    assert issue.rule == "format.failed" and issue.file == "rtl/tiny_x.sv"
    assert "cannot parse line 2" in issue.msg or "exited with 3" in issue.msg
    assert (root / "rtl" / "tiny_x.sv").read_text() == MESSY  # put back as it was


def test_a_missing_formatter_is_an_error(tmp_path: Path) -> None:
    root = _project(tmp_path, {"use": "cmd", "cmd": ["zqx-no-such-formatter", "{file}"]})
    result = _run(root, {"files": ["rtl/tiny_x.sv"]})
    assert result.status == "error"
    assert "zqx-no-such-formatter" in result.issues[0].msg
    unknown = _project(tmp_path / "u", {"use": "zqx_adapter"})
    assert _run(unknown, {"files": ["rtl/tiny_x.sv"]}).issues[0].rule == "format.adapter"


def test_no_formatter_and_no_verible_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(format_mod.shutil, "which", lambda _name: None)
    result = _run(root, {"files": ["rtl/tiny_x.sv"]})
    assert result.status == "skipped" and result.issues[0].rule == "format.no_formatter"
    assert format_mod.DEFAULT_FORMATTER in result.issues[0].msg
    assert (root / "rtl" / "tiny_x.sv").read_text() == MESSY


def test_no_formatter_uses_verible_when_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    fake = tmp_path / "fake-verible"
    fake.write_text(f'#!/bin/sh\n[ "$1" = --inplace ] && exec python3 {TRIM} "$2"\nexit 9\n')
    fake.chmod(0o755)
    monkeypatch.setattr(format_mod.shutil, "which", lambda _name: str(fake))
    result = _run(root, {"files": ["rtl/tiny_x.sv"]})
    assert result.status == "pass" and (root / "rtl" / "tiny_x.sv").read_text() == CLEAN


def test_without_task_files_it_is_skipped_and_touches_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, TRIM_CFG)
    result = _run(root, {}, {"block": "x"})  # `chipgraph check` over a block
    assert result.status == "skipped" and result.issues[0].rule == "format.no_files"
    assert (root / "rtl" / "tiny_x.sv").read_text() == MESSY
    assert _run(root, {"files": ["rtl/{module}.sv"]}, {"block": "x"}).status == "skipped"


def test_target_files_from_the_approved_plan(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "plan").mkdir(parents=True)
    approved = {
        "modules": [
            {"name": "tiny_x", "writes": ["rtl/tiny_x.sv", "dv/x/test_x.py", "rtl/x_pkg.svh"]}
        ]
    }
    (root / "plan" / "x.approved.json").write_text(json.dumps(approved))
    params = {"block": "x", "module": "tiny_x"}
    assert target_files({}, params, root) == ["rtl/tiny_x.sv", "rtl/x_pkg.svh"]
    assert target_files({}, {"block": "y", "module": "tiny_x"}, root) is None  # no plan
    with pytest.raises(TargetError):
        target_files({}, {"block": "x", "module": "tiny_nope"}, root)
    with pytest.raises(TargetError):
        target_files({"files": ["../outside.sv"]}, params, root)
    assert target_files({"files": "rtl/{BLOCK}.sv"}, params, root) == ["rtl/X.sv"]
