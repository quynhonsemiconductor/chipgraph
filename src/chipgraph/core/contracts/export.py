"""Export JSON Schema files for the core contracts.

Run as ``python -m chipgraph.core.contracts.export [--check] [--out DIR]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chipgraph.core.contracts import TOP_LEVEL_MODELS

_README_CONTENT = (
    "# schemas\n"
    "\n"
    "These JSON Schema files are generated from the pydantic models in\n"
    "`src/chipgraph/core/contracts/` by `make schemas`\n"
    "(`python -m chipgraph.core.contracts.export`). Do not edit them by hand.\n"
)


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` until a directory containing `pyproject.toml` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(f"no pyproject.toml found above {start}")


def generate_schema_files() -> dict[str, str]:
    """Build the mapping of {filename: contents} for every schema file to write."""
    files: dict[str, str] = {"README.md": _README_CONTENT}
    for model in TOP_LEVEL_MODELS:
        filename = f"{model.__name__}.schema.json"
        schema = model.model_json_schema()
        files[filename] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return files


def write_schema_files(out_dir: Path) -> None:
    """Write the generated schema files to `out_dir`, creating it if needed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in generate_schema_files().items():
        (out_dir / filename).write_text(content, encoding="utf-8")


def check_schema_files(out_dir: Path) -> list[str]:
    """Compare generated schema files against what's on disk in `out_dir`.

    Returns a list of human-readable problem descriptions; empty means up to date.
    """
    expected = generate_schema_files()
    problems: list[str] = []

    existing_names = {p.name for p in out_dir.glob("*")} if out_dir.is_dir() else set()

    for filename, content in expected.items():
        path = out_dir / filename
        if not path.is_file():
            problems.append(f"missing: {filename}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"outdated: {filename}")

    extra_names = existing_names - set(expected)
    for filename in sorted(extra_names):
        if filename.endswith(".schema.json") or filename == "README.md":
            problems.append(f"extra: {filename}")

    return problems


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: regenerate or check the `schemas/` directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that schemas/ matches the models, without writing; exit 1 if not",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: schemas/ at the repo root)",
    )
    args = parser.parse_args(argv)

    out_dir = args.out if args.out is not None else find_repo_root(Path(__file__)) / "schemas"

    if args.check:
        problems = check_schema_files(out_dir)
        if problems:
            print("schemas out of date:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(f"schemas up to date in {out_dir}")
        return 0

    write_schema_files(out_dir)
    print(f"wrote schemas to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
