"""Tests for `spec_schema`'s `requirement.missing_id` on a copy of `examples/tinysoc`.

tinysoc's MAS files declare `REQ-TIM-*` / `REQ-GPIO-*` IDs and their Verification items
cite them, so the pass case has no finding. Each fail case adds one Verification item with
no ID to the timer MAS and re-ingests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from cross_a_helpers import edit_and_reingest, rules, run_check, tinysoc_project
from typer.testing import CliRunner

from chipgraph.checks import SpecSchemaCheck
from chipgraph.cli import app

_TIMER_MAS = "doc/specs/TINY_TIMER_MAS.md"
_LAST_ITEM = (
    "5. Read path (`REQ-TIM-005`): `CTRL` reads back enable and pending; offset `0x3` reads 0.\n"
)
_NEW_ITEM = "6. A write to `COMPARE` takes effect on the next clock.\n"
_NEW_ITEM_LINE = 124
_MSG = (
    "Verification item has no ID; the other items of this file have one. "
    "Suggested next ID: REQ-TIM-006"
)


def _set_infer(root: Path, infer: str) -> None:
    prof = root / ".chipgraph.yml"
    doc = yaml.safe_load(prof.read_text(encoding="utf-8"))
    doc["spec"]["requirements"] = {"infer": infer}
    prof.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _add_item(root: Path) -> None:
    edit_and_reingest(root, _TIMER_MAS, _LAST_ITEM, _LAST_ITEM + _NEW_ITEM)


def _missing(result: object) -> list[object]:
    return [i for i in result.issues if i.rule == "requirement.missing_id"]  # type: ignore[attr-defined]


def test_tinysoc_has_no_missing_id(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    result = run_check(SpecSchemaCheck(), root)
    assert result.status == "pass"
    assert _missing(result) == []


@pytest.mark.parametrize("infer", ["off", "verification"])
def test_item_without_id_is_an_error_with_line_and_next_id(tmp_path: Path, infer: str) -> None:
    root = tinysoc_project(tmp_path)
    _set_infer(root, infer)
    _add_item(root)
    result = run_check(SpecSchemaCheck(), root)
    assert result.status == "fail"
    assert rules(result) == ["requirement.missing_id"]
    (issue,) = result.issues
    assert issue.severity == "error"
    assert issue.file == _TIMER_MAS
    assert issue.line == _NEW_ITEM_LINE
    assert issue.msg == _MSG


def test_no_hint_when_the_next_id_does_not_fit_the_pattern(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    prof = root / ".chipgraph.yml"
    doc = yaml.safe_load(prof.read_text(encoding="utf-8"))
    # Only IDs ending in 1-5 are valid: REQ-TIM-006 would not match, so no hint.
    doc["spec"]["requirements"] = {"id_pattern": "REQ-[A-Z]+-00[1-5]"}
    prof.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _add_item(root)
    result = run_check(SpecSchemaCheck(), root)
    (issue,) = _missing(result)
    assert issue.severity == "error"  # type: ignore[attr-defined]
    assert issue.msg == (  # type: ignore[attr-defined]
        "Verification item has no ID; the other items of this file have one."
    )


def test_block_scope(tmp_path: Path) -> None:
    root = tinysoc_project(tmp_path)
    _add_item(root)
    assert _missing(run_check(SpecSchemaCheck(), root, block="gpio")) == []
    scoped = _missing(run_check(SpecSchemaCheck(), root, block="timer"))
    assert [(i.file, i.line) for i in scoped] == [(_TIMER_MAS, _NEW_ITEM_LINE)]  # type: ignore[attr-defined]


def test_finding_can_be_waived(tmp_path: Path) -> None:
    """The finding goes through the normal findings path and a waiver silences it."""
    root = tinysoc_project(tmp_path)
    _add_item(root)
    cli = CliRunner()

    checked = cli.invoke(app, ["-C", str(root), "check", "--only", "spec_schema"])
    assert checked.exit_code == 1, checked.output

    listed = cli.invoke(app, ["--json", "-C", str(root), "findings"])
    assert listed.exit_code == 0, listed.output
    rows = [r for r in json.loads(listed.output) if r["source"] == "check:spec_schema"]
    assert len(rows) == 1
    evidence = rows[0]["evidence"][0]
    assert (evidence["file"], evidence["line"]) == (_TIMER_MAS, _NEW_ITEM_LINE)
    assert evidence["note"] == "requirement.missing_id"
    row = rows[0]

    waived = cli.invoke(
        app,
        ["-C", str(root), "waive", row["id"], "--reason", "ID agreed with the owner", "--by", "t"],
    )
    assert waived.exit_code == 0, waived.output

    open_rows = json.loads(cli.invoke(app, ["--json", "-C", str(root), "findings"]).output)
    assert row["id"] not in {r["id"] for r in open_rows}
    waived_rows = json.loads(
        cli.invoke(app, ["--json", "-C", str(root), "findings", "--status", "waived"]).output
    )
    assert row["id"] in {r["id"] for r in waived_rows}
