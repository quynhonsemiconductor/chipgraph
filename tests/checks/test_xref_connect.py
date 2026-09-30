"""Tests for `ConnectCheck` (M1-07 part B): top RTL wiring vs the chip contract.

Accept criteria (seeded on a throwaway copy of `examples/tinysoc`):

* tinysoc PASSES: both blocks are instantiated in the top and the timer irq is wired.
* `instance.missing`: removing the timer instance from `tiny_top.sv` is caught, at the
  top file.
* `interrupt.wiring`: leaving the timer's irq output unconnected is caught, at the
  instance's line.
* a missing model, and an unfindable top, are whole-check `error`s.
* findings are stored at layer 1 through `ProfileCheckRunner`.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from xref_b_helpers import (
    assert_findings_layer,
    find_issue,
    make_ctx,
    make_project,
    run_check,
)

from chipgraph.checks import ConnectCheck
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import Check


def test_is_a_registered_check() -> None:
    assert isinstance(ConnectCheck(), Check)
    registry = Registry()
    registry.discover()
    assert "connect" in registry.names("check")
    assert registry.get("check", "connect").id == "connect"


def test_tinysoc_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = run_check(ConnectCheck(), {}, make_ctx(root))
    assert result.status == "pass", [i.msg for i in result.issues]
    assert all(i.severity != "error" for i in result.issues)


def test_missing_instance_fails(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        path = root / "rtl" / "tiny_top.sv"
        text = path.read_text()
        # Drop the whole `tiny_timer u_timer (...);` instantiation.
        removed = re.sub(r"  tiny_timer u_timer \(.*?\);\n", "", text, flags=re.S)
        assert removed != text
        path.write_text(removed)

    root = make_project(tmp_path, edit=edit)
    result = run_check(ConnectCheck(), {}, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "instance.missing")
    assert issue is not None
    assert "timer" in issue.msg
    assert issue.file == "rtl/tiny_top.sv"


def test_unconnected_interrupt_fails(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        path = root / "rtl" / "tiny_top.sv"
        text = path.read_text()
        # Remove the irq connection from the timer instance (leave the instance itself).
        removed = text.replace("      .irq   (timer_irq)\n", "")
        assert removed != text
        path.write_text(removed)

    root = make_project(tmp_path, edit=edit)
    result = run_check(ConnectCheck(), {}, make_ctx(root))
    assert result.status == "fail"
    issue = find_issue(result, "interrupt.wiring")
    assert issue is not None
    assert issue.file == "rtl/tiny_top.sv"
    assert issue.line is not None  # points at the instance


def test_missing_model_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    spec = CheckSpec(id="connect", capability="connect", adapter="connect", args={})
    result = asyncio.run(ConnectCheck().run(spec, make_ctx(root)))
    assert result.status == "error"
    assert result.issues[0].rule == "model"


def test_unknown_top_file_is_an_error(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    result = run_check(ConnectCheck(), {"top_file": "rtl/does_not_exist.sv"}, make_ctx(root))
    assert result.status == "error"
    assert result.issues[0].rule == "top"


def test_findings_stored_at_layer_1(tmp_path: Path) -> None:
    def edit(root: Path) -> None:
        path = root / "rtl" / "tiny_top.sv"
        text = path.read_text()
        path.write_text(re.sub(r"  tiny_timer u_timer \(.*?\);\n", "", text, flags=re.S))

    root = make_project(tmp_path, edit=edit)
    assert_findings_layer(root, "connect", expected_layer=1)
