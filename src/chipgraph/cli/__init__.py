"""chipgraph CLI entry point.

This is a minimal bootstrap CLI (task M0-01): it only supports `--version`.
The full CLI (commands to run rules, checks, gates, etc.) is task M0-12.
"""

import typer

from chipgraph import __version__

app = typer.Typer(no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"chipgraph {__version__}")
        raise typer.Exit(code=0)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the chipgraph version and exit.",
    ),
) -> None:
    """chipgraph CLI (bootstrap)."""
