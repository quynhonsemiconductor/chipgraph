"""End-to-end tests for chipgraph against `examples/tinysoc` (task M0-16).

Runs the real CLI (`typer.testing.CliRunner` over `chipgraph.cli:app`) against a fresh,
git-initialized copy of `examples/tinysoc` (see `conftest.copy_tinysoc`), driving
Verilator for real. Skipped, not failed, when `verilator`/`make` are not on `PATH`
(there is no EDA toolchain in the default CI job; see `docker/` for the image that
will run this suite in CI, task M0-17).

`examples/tinysoc` has two rules (see its own `.chipgraph/packs/tinysoc/pack.yml`):
`tinysoc/rtl` (a `human` rule per block, whose output is the block's RTL file) and
`tinysoc/lint_manifest` (a `gen` rule per block that writes a small manifest and then
runs the `lint` check). Neither rule is named `check:all` — the
build graph's `select()` only understands `"*"`, a bare rule id (every `foreach`
instance of that rule, plus its dependencies), or `id[k=v]`. The bare rule id
`tinysoc/lint_manifest` is "build and lint everything this project has checks for",
and is what these tests use as chipgraph's equivalent of the plan's `check:all`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import copy_tinysoc, requires_eda_tools
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout

pytestmark = [pytest.mark.e2e, requires_eda_tools]

runner = CliRunner()

ALL_CHECKS_TARGET = "tinysoc/lint_manifest"

_BUG_MARKER = "bogus_signal"
_BUG_LINE = "  assign pin_out = bogus_signal;\n"


def _invoke(root: Path, *args: str, json_output: bool = False) -> object:
    cli_args = (["--json"] if json_output else []) + ["-C", str(root), *args]
    result = runner.invoke(app, cli_args)
    return result


def _invoke_json(root: Path, *args: str) -> dict:
    result = _invoke(root, *args, json_output=True)
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _inject_gpio_lint_error(root: Path) -> None:
    """Add a reference to an undeclared signal in `tiny_gpio.sv`: a real Verilator
    `%Error` (not just a warning), with a genuine file:line, that only Verilator
    itself can catch (not a chipgraph-side syntax check)."""
    path = root / "rtl" / "tiny_gpio.sv"
    text = path.read_text()
    marker = "assign pin_out = out_q;\n"
    assert marker in text
    path.write_text(text.replace(marker, marker + _BUG_LINE, 1))


def _remove_gpio_lint_error(root: Path) -> None:
    path = root / "rtl" / "tiny_gpio.sv"
    text = path.read_text()
    assert _BUG_LINE in text
    path.write_text(text.replace(_BUG_LINE, "", 1))


def _failure_labels(root: Path, run_id: str) -> dict[str, str]:
    layout = StateLayout(root)
    events = journal_mod.read(layout.journal(run_id)).events
    return {
        e.rule_instance: e.failure_label
        for e in events
        if e.type == "rule_fail" and e.rule_instance is not None
    }


# --- 1. config check / doctor -------------------------------------------------------


def test_config_check_and_doctor_ok(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")

    check = _invoke(root, "config", "check")
    assert check.exit_code == 0, check.output
    assert "no issues" in check.output

    doctor = _invoke(root, "doctor")
    assert doctor.exit_code == 0, doctor.output
    assert "MISSING" not in doctor.output


# --- 2. check: all three blocks pass -------------------------------------------------


def test_check_all_blocks_pass(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")

    result = _invoke(root, "check")
    assert result.exit_code == 0, result.output
    for block in ("timer", "gpio", "top"):
        assert re.search(rf"PASS\s+lint\s+{block}\s+0 issues", result.output), result.output


# --- 3. build everything, then rebuild fresh -----------------------------------------


def test_build_all_checks_then_rebuild_is_fresh(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")

    build1 = _invoke_json(root, "build", ALL_CHECKS_TARGET)
    assert build1["failed"] == []
    assert build1["waiting_gate"] == []
    expected_instances = {f"tinysoc/rtl[block={b}]" for b in ("timer", "gpio", "top")} | {
        f"tinysoc/lint_manifest[block={b}]" for b in ("timer", "gpio", "top")
    }
    assert set(build1["done"]) == expected_instances

    status = _invoke(root, "status")
    assert status.exit_code == 0, status.output
    for iid in expected_instances:
        assert f"done          {iid}" in status.output, status.output

    build2 = _invoke_json(root, "build", ALL_CHECKS_TARGET)
    assert build2["done"] == []
    assert set(build2["skipped_fresh"]) == expected_instances


# --- 4 & 5. a real lint error, then fixed and resumed --------------------------------


def test_lint_error_then_fix_and_resume(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    assert _invoke_json(root, "build", ALL_CHECKS_TARGET)["failed"] == []

    # A person breaks tiny_gpio.sv. `check` runs on the working tree: gpio and top fail
    # (top's filelist includes tiny_gpio.sv), pointing at the right file:line.
    _inject_gpio_lint_error(root)
    check = _invoke(root, "check")
    assert check.exit_code == 1, check.output
    assert re.search(r"FAIL\s+lint\s+gpio\s+1 issues", check.output), check.output
    assert re.search(r"FAIL\s+lint\s+top\s+1 issues", check.output), check.output
    assert re.search(r"PASS\s+lint\s+timer\s+0 issues", check.output), check.output
    assert "rtl/tiny_gpio.sv:52" in check.output

    # The edited RTL is a `human` output: stale, not a hand-edit failure. The gpio
    # rule reruns and its lint fails; the other blocks are fresh (see the README's
    # known limit on `top`).
    build = _invoke_json(root, "build", ALL_CHECKS_TARGET)
    run_id = build["run_id"]
    assert build["failed"] == ["tinysoc/lint_manifest[block=gpio]"]
    assert build["done"] == ["tinysoc/rtl[block=gpio]"]
    assert _failure_labels(root, run_id)["tinysoc/lint_manifest[block=gpio]"] == "verification"

    # Fix the file and resume the same run: the failed instance completes.
    _remove_gpio_lint_error(root)
    resume = _invoke_json(root, "resume", run_id)
    assert resume["run_id"] == run_id
    assert resume["failed"] == []
    assert "tinysoc/lint_manifest[block=gpio]" in resume["done"]

    assert _invoke(root, "check").exit_code == 0


# --- 6. hand-edit protection ----------------------------------------------------------


def test_hand_edited_generated_output_is_a_constraint_failure(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")

    build1 = _invoke_json(root, "build", ALL_CHECKS_TARGET)
    assert build1["failed"] == []

    manifest_path = root / "build" / "timer.manifest.json"
    assert manifest_path.is_file()
    with manifest_path.open("a") as f:
        f.write("// hand-edited, not written by gen_manifest.py\n")

    build2 = _invoke_json(root, "build", ALL_CHECKS_TARGET)
    assert build2["failed"] == ["tinysoc/lint_manifest[block=timer]"]
    assert set(build2["skipped_fresh"]) == {
        "tinysoc/rtl[block=timer]",
        "tinysoc/rtl[block=gpio]",
        "tinysoc/rtl[block=top]",
        "tinysoc/lint_manifest[block=gpio]",
        "tinysoc/lint_manifest[block=top]",
    }

    labels = _failure_labels(root, build2["run_id"])
    assert labels["tinysoc/lint_manifest[block=timer]"] == "constraint"
