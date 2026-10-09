"""JSON Schema files for the role and skill files, under `schemas/roles/`.

    python -m chipgraph.core.runtime.roles.schema [--check] [--out DIR]

`make schemas` runs it after the contract schemas; `make schemas-check` with `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chipgraph.core.runtime.roles.skills import SkillSpec
from chipgraph.core.runtime.roles.spec import RoleSpec

SCHEMA_MODELS = (RoleSpec, SkillSpec)
"""The models whose schemas are committed, as `<Model>.schema.json`."""


def generate() -> dict[str, str]:
    """File name to content, for every schema file."""
    files = {}
    for model in SCHEMA_MODELS:
        schema = model.model_json_schema()
        files[f"{model.__name__}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return files


def _repo_schemas_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate / "schemas" / "roles"
    raise FileNotFoundError(f"no pyproject.toml found above {here}")


def check(out_dir: Path) -> list[str]:
    """Problems with the committed files in `out_dir` (empty: up to date)."""
    expected = generate()
    problems = []
    for name, content in expected.items():
        path = out_dir / name
        if not path.is_file():
            problems.append(f"missing: roles/{name}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"outdated: roles/{name}")
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.schema.json")):
            if path.name not in expected:
                problems.append(f"extra: roles/{path.name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="check, do not write; exit 1 if out of date"
    )
    parser.add_argument("--out", type=Path, default=None, help="default: schemas/roles/")
    args = parser.parse_args(argv)
    out_dir: Path = args.out if args.out is not None else _repo_schemas_dir()
    if args.check:
        problems = check(out_dir)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        if problems:
            print("role schemas out of date: run `make schemas`", file=sys.stderr)
            return 1
        print(f"role schemas up to date in {out_dir}")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in generate().items():
        (out_dir / name).write_text(content, encoding="utf-8")
    print(f"wrote role schemas to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
