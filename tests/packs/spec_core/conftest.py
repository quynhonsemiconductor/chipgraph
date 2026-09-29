"""Shared fixtures for the ``spec-core`` pack tests."""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "mas_fixtures"


@pytest.fixture
def mas_fixtures() -> Path:
    """The directory of verbatim QSoC MAS fixtures (Apache-2.0, commit 7a917d1)."""
    return _FIXTURES
