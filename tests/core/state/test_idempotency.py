"""Tests for `chipgraph.core.state.idempotency`."""

from __future__ import annotations

from pathlib import Path

from chipgraph.core.contracts.check import CheckResult
from chipgraph.core.state.idempotency import IdempotencyStore, make_key
from chipgraph.core.state.layout import StateLayout


def make_result(key: str) -> CheckResult:
    return CheckResult(
        check_id="digital-rtl/lint",
        status="pass",
        duration_s=1.5,
        idempotency_key=key,
    )


def test_put_get_has(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    store = IdempotencyStore(layout, run_id="run-1")
    key = make_key({"cmd": "verilator --lint-only", "cwd": "/proj"})

    assert store.has(key) is False
    assert store.get(key) is None

    result = make_result(key)
    store.put(result)

    assert store.has(key) is True
    assert store.get(key) == result


def test_make_key_order_independent() -> None:
    a = make_key({"cmd": "x", "cwd": "/proj", "tool": "verilator@5.020"})
    b = make_key({"tool": "verilator@5.020", "cwd": "/proj", "cmd": "x"})
    assert a == b

    c = make_key({"cmd": "y", "cwd": "/proj", "tool": "verilator@5.020"})
    assert a != c


def test_store_survives_new_instance(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    key = make_key({"cmd": "x"})
    result = make_result(key)
    IdempotencyStore(layout, run_id="run-1").put(result)

    store2 = IdempotencyStore(layout, run_id="run-1")
    assert store2.has(key) is True
    assert store2.get(key) == result
