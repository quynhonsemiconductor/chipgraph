"""Tests for the Design Model key grammar (`chipgraph.core.model.keys`)."""

from __future__ import annotations

import pytest

from chipgraph.core.model.keys import InvalidKeyError, key_kind, make_key, parse_key


def test_make_key_single_part() -> None:
    assert make_key("block", "timer") == "block:timer"


def test_make_key_multiple_parts() -> None:
    assert make_key("register", "timer", "CTRL") == "register:timer.CTRL"
    assert make_key("field", "timer", "CTRL", "EN") == "field:timer.CTRL.EN"


def test_make_key_and_parse_key_round_trip() -> None:
    for kind, parts in [
        ("project", ("qsoc",)),
        ("block", ("timer",)),
        ("module", ("tiny_timer",)),
        ("port", ("tiny_timer", "i_clk")),
        ("register", ("timer", "CTRL")),
        ("field", ("timer", "CTRL", "EN")),
        ("requirement", ("REQ-TIM-004",)),
    ]:
        key = make_key(kind, *parts)
        assert parse_key(key) == (kind, parts)
        assert key_kind(key) == kind


def test_make_key_examples_from_design_doc() -> None:
    assert make_key("block", "timer") == "block:timer"
    assert make_key("module", "tiny_timer") == "module:tiny_timer"
    assert make_key("port", "tiny_timer", "i_clk") == "port:tiny_timer.i_clk"
    assert make_key("register", "timer", "CTRL") == "register:timer.CTRL"
    assert make_key("field", "timer", "CTRL", "EN") == "field:timer.CTRL.EN"
    assert make_key("requirement", "REQ-TIM-004") == "requirement:REQ-TIM-004"


def test_make_key_requires_at_least_one_part() -> None:
    with pytest.raises(InvalidKeyError):
        make_key("block")


def test_make_key_rejects_empty_kind() -> None:
    with pytest.raises(InvalidKeyError):
        make_key("", "timer")


def test_make_key_rejects_kind_with_colon() -> None:
    with pytest.raises(InvalidKeyError):
        make_key("bad:kind", "timer")


def test_make_key_rejects_empty_part() -> None:
    with pytest.raises(InvalidKeyError):
        make_key("port", "tiny_timer", "")


def test_parse_key_rejects_missing_separator() -> None:
    with pytest.raises(InvalidKeyError):
        parse_key("block-timer")


def test_parse_key_rejects_empty_kind() -> None:
    with pytest.raises(InvalidKeyError):
        parse_key(":timer")


def test_parse_key_rejects_no_parts() -> None:
    with pytest.raises(InvalidKeyError):
        parse_key("block:")


def test_key_kind_extracts_just_the_kind() -> None:
    assert key_kind("field:timer.CTRL.EN") == "field"
