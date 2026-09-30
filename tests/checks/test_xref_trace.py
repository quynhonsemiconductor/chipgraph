"""Tests for `TraceCheck` (M1-07 part B): declared REQ traceability by test files.

Accept criteria (seeded on a throwaway copy of `examples/tinysoc`):

* tinysoc PASSES: every declared REQ-ID is named by a dv stub.
* `req.no_test`: a declared REQ removed from a stub is no longer traced.
* `test.unknown_req`: a stub naming `REQ-TIM-999` is caught.
* inferred requirements produce one `info` per block (not one failure each), and the
  message says they are inferred (D37).
* `--block` scopes the requirements considered.
* a missing model is a whole-check `error`.
* findings are stored at layer 5 through `ProfileCheckRunner`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from xref_b_helpers import (
    assert_findings_layer,
    find_issue,
    make_ctx,
    make_project,
    replace_in_file,
    run_check,
)

from chipgraph.checks import TraceCheck
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import Check

_TESTS = {"tests": ["dv/**/*.py", "dv/**/*.sv"]}


def test_is_a_registered_check() -> None:
    assert isinstance(TraceCheck(), Check)
    registry = Registry()
    registry.discover()
    assert "trace" in registry.names("check")
    assert registry.get("check", "trace").id == "trace"


def test_tinysoc_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = run_check(TraceCheck(), _TESTS, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]
    assert result.issues == ()


def test_declared_req_without_a_test_fails(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        replace_in_file(root, "dv/test_tiny_timer.py", "REQ-TIM-001", "REQ-TIM-00X")

    root = make_project(tmp_path, edit=edit)
    result = run_check(TraceCheck(), _TESTS, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "req.no_test")
    assert issue is not None
    assert "REQ-TIM-001" in issue.msg
    assert issue.file == "doc/specs/TINY_TIMER_MAS.md"
    assert issue.line is not None


def test_test_naming_an_unknown_req_fails(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        path = root / "dv" / "test_tiny_timer.py"
        path.write_text(
            path.read_text() + "\ndef test_ghost():\n    # verifies: REQ-TIM-999\n    pass\n"
        )

    root = make_project(tmp_path, edit=edit)
    result = run_check(TraceCheck(), _TESTS, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "test.unknown_req")
    assert issue is not None
    assert "REQ-TIM-999" in issue.msg
    assert issue.file == "dv/test_tiny_timer.py"
    # The added test still names every real REQ, so no_test never fires here.
    assert find_issue(result, "req.no_test") is None


def test_inferred_requirements_produce_one_info_per_block(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        prof = root / ".chipgraph.yml"
        doc = yaml.safe_load(prof.read_text())
        # A pattern the MAS's REQ-TIM-* IDs do not match, plus infer: every Verification
        # item becomes an inferred requirement instead (D37).
        doc["spec"]["requirements"] = {"id_pattern": r"{BLOCK}_\d{3}", "infer": "verification"}
        prof.write_text(yaml.safe_dump(doc, sort_keys=False))

    root = make_project(tmp_path, edit=edit)
    args = {**_TESTS, "id_pattern": r"{BLOCK}_\d{3}"}
    result = run_check(TraceCheck(), args, make_ctx(root))
    assert result.status == "pass"  # info only, never blocks
    infos = [i for i in result.issues if i.rule == "req.inferred_untraceable"]
    assert {i.severity for i in infos} == {"info"}
    assert len(infos) == 2  # one per block (timer, gpio)
    for info in infos:
        assert "inferred" in info.msg


def test_block_param_scopes_the_requirements(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        # Break a gpio REQ trace; scoping to `timer` must not see it.
        replace_in_file(root, "dv/test_tiny_gpio.py", "REQ-GPIO-001", "REQ-GPIO-00X")

    root = make_project(tmp_path, edit=edit)
    scoped = run_check(TraceCheck(), _TESTS, make_ctx(root, block="timer"))
    assert find_issue(scoped, "req.no_test") is None
    unscoped = run_check(TraceCheck(), _TESTS, make_ctx(root))
    assert find_issue(unscoped, "req.no_test") is not None


def test_missing_model_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    ctx = make_ctx(root)
    spec = CheckSpec(id="trace", capability="trace", adapter="trace", args=_TESTS)
    result = asyncio.run(TraceCheck().run(spec, ctx))
    assert result.status == "error"
    assert result.issues[0].rule == "model"


def test_missing_tests_arg_is_an_error(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = run_check(TraceCheck(), {}, make_ctx(root))
    assert result.status == "error"


@pytest.mark.parametrize("tests_arg", [_TESTS])
def test_findings_stored_at_layer_5(tmp_path: Path, tests_arg: dict[str, object]) -> None:
    def edit(root: Path) -> None:
        replace_in_file(root, "dv/test_tiny_timer.py", "REQ-TIM-001", "REQ-TIM-00X")

    root = make_project(tmp_path, edit=edit)
    assert_findings_layer(root, "trace", expected_layer=5)
