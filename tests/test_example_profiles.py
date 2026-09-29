"""The example profiles in docs/examples stay valid as the config schema changes."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app

EXAMPLES = sorted((Path(__file__).parent.parent / "docs" / "examples").glob("*.chipgraph.yml"))


@pytest.mark.parametrize("profile", EXAMPLES, ids=lambda p: p.name)
def test_example_profile_is_valid(profile: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["-C", str(tmp_path), "--profile", str(profile), "config", "check"]
    )
    assert result.exit_code == 0, result.output
    assert "no issues" in result.output


def test_there_is_an_example() -> None:
    assert EXAMPLES


def test_qsoc_example_declares_block_ids_and_infers_the_rest() -> None:
    from chipgraph.core.config.loader import load

    profile_path = Path(__file__).parent.parent / "docs" / "examples" / "qsoc.chipgraph.yml"
    resolved = load(profile_path.parent, profile_path=profile_path)
    assert resolved is not None
    req = resolved.profile.spec.requirements
    assert req.infer == "verification"
    regex = req.id_regex("dma")
    assert regex.fullmatch("DMA_001")
    assert not regex.fullmatch("UART_001")
