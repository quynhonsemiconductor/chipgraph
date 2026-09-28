from typer.testing import CliRunner

import chipgraph
from chipgraph.cli import app

runner = CliRunner()


def test_version_is_nonempty_string() -> None:
    assert isinstance(chipgraph.__version__, str)
    assert chipgraph.__version__ != ""


def test_cli_version_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"chipgraph {chipgraph.__version__}"
