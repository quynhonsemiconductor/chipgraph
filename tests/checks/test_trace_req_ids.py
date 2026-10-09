"""`trace` and requirement IDs: missing IDs counted as untraceable, unknown references.

* `req.inferred_untraceable` counts Verification items *missing* their ID (in a file that
  declares IDs) alongside inferred requirements, and says they are not declared IDs.
* `test.unknown_req` (the "unknown reference" rule) catches a test naming an ID-shaped
  string that is no requirement, with declared `REQ-` IDs and with the QSoC
  `{BLOCK}_\\d{3}` pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from xref_b_helpers import make_ctx, make_project, replace_in_file, run_check

from chipgraph.checks import TraceCheck

_TESTS = {"tests": ["dv/**/*.py", "dv/**/*.sv"]}
_TIMER_MAS = "doc/specs/TINY_TIMER_MAS.md"
_LAST_ITEM = (
    "5. Read path (`REQ-TIM-005`): `CTRL` reads back enable and pending; offset `0x3` reads 0.\n"
)


def _add_item_without_id(root: Path) -> None:
    replace_in_file(
        root, _TIMER_MAS, _LAST_ITEM, _LAST_ITEM + "6. A write to `COMPARE` takes effect.\n"
    )


def _set_requirements(root: Path, requirements: dict[str, str]) -> None:
    prof = root / ".chipgraph.yml"
    doc = yaml.safe_load(prof.read_text(encoding="utf-8"))
    doc["spec"]["requirements"] = requirements
    prof.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _infos(result: object) -> list[object]:
    return [i for i in result.issues if i.rule == "req.inferred_untraceable"]  # type: ignore[attr-defined]


def test_missing_items_are_counted_as_untraceable(tmp_path: Path) -> None:
    root = make_project(tmp_path, edit=_add_item_without_id)
    result = run_check(TraceCheck(), _TESTS, make_ctx(root))
    assert result.status == "pass"  # info only; the error is spec_schema's
    (info,) = _infos(result)
    assert info.severity == "info"  # type: ignore[attr-defined]
    assert info.msg == (  # type: ignore[attr-defined]
        "1 requirement(s) in block:timer cannot be traced by ID: 1 Verification item(s) "
        "missing an ID (requirement.missing_id); none of them is a declared REQ-ID"
    )


def test_missing_and_inferred_are_counted_together(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        # With infer on, tinysoc's items that only cite an ID mid-text stay inferred.
        _set_requirements(root, {"infer": "verification"})
        _add_item_without_id(root)

    root = make_project(tmp_path, edit=edit)
    result = run_check(TraceCheck(), _TESTS, make_ctx(root))
    msgs = {i.msg for i in _infos(result)}  # type: ignore[attr-defined]
    assert msgs == {
        "3 inferred requirement(s) in block:gpio cannot be traced by ID "
        "(they are inferred, not declared REQ-IDs)",
        "6 requirement(s) in block:timer cannot be traced by ID: 5 inferred, "
        "1 Verification item(s) missing an ID (requirement.missing_id); "
        "none of them is a declared REQ-ID",
    }
    scoped = run_check(TraceCheck(), _TESTS, make_ctx(root, block="gpio"))
    assert len(_infos(scoped)) == 1


def _to_qsoc_ids(root: Path) -> None:
    """Rename tinysoc's `REQ-TIM-00n` / `REQ-GPIO-00n` to QSoC-style `TIMER_00n` / `GPIO_00n`."""
    for rel in (
        _TIMER_MAS,
        "doc/specs/TINY_GPIO_MAS.md",
        "dv/test_tiny_timer.py",
        "dv/test_tiny_gpio.py",
    ):
        path = root / rel
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("REQ-TIM-", "TIMER_").replace("REQ-GPIO-", "GPIO_"), encoding="utf-8"
        )
    _set_requirements(root, {"id_pattern": "{BLOCK}_\\d{3}"})


@pytest.mark.parametrize(
    ("mode", "ghost", "args"),
    [
        ("declared-req", "REQ-TIM-999", _TESTS),
        ("qsoc-block", "TIMER_999", {**_TESTS, "id_pattern": "{BLOCK}_\\d{3}"}),
    ],
)
def test_unknown_reference_in_a_test_is_an_error(
    tmp_path: Path, mode: str, ghost: str, args: dict[str, object]
) -> None:
    def edit(root: Path) -> None:
        if mode == "qsoc-block":
            _to_qsoc_ids(root)
        path = root / "dv" / "test_tiny_timer.py"
        path.write_text(
            path.read_text() + f"\ndef test_ghost():\n    # verifies: {ghost}\n    pass\n"
        )

    root = make_project(tmp_path, edit=edit)
    result = run_check(TraceCheck(), args, make_ctx(root))
    assert result.status == "fail"
    unknown = [i for i in result.issues if i.rule == "test.unknown_req"]
    assert [(i.file, i.severity) for i in unknown] == [("dv/test_tiny_timer.py", "error")]
    assert ghost in unknown[0].msg
    # Every real ID is still declared and cited by a test.
    assert not [i for i in result.issues if i.rule == "req.no_test"]
    assert not _infos(result)
