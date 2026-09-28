"""Tests for the schemas export/check CLI."""

from pathlib import Path

from chipgraph.core.contracts.export import (
    check_schema_files,
    find_repo_root,
    generate_schema_files,
    main,
    write_schema_files,
)


def _repo_root() -> Path:
    return find_repo_root(Path(__file__))


def test_check_passes_on_committed_schemas() -> None:
    problems = check_schema_files(_repo_root() / "schemas")
    assert problems == []


def test_main_check_flag_returns_zero_on_committed_schemas() -> None:
    exit_code = main(["--check"])
    assert exit_code == 0


def test_check_fails_after_temp_change(tmp_path: Path) -> None:
    out_dir = tmp_path / "schemas"
    write_schema_files(out_dir)
    assert check_schema_files(out_dir) == []

    # Corrupt one generated file, and remove another, and add an unexpected one.
    (out_dir / "ArtifactRef.schema.json").write_text("{}", encoding="utf-8")
    (out_dir / "CheckSpec.schema.json").unlink()
    (out_dir / "Extra.schema.json").write_text("{}", encoding="utf-8")

    problems = check_schema_files(out_dir)
    assert any("outdated: ArtifactRef.schema.json" in p for p in problems)
    assert any("missing: CheckSpec.schema.json" in p for p in problems)
    assert any("extra: Extra.schema.json" in p for p in problems)

    exit_code = main(["--check", "--out", str(out_dir)])
    assert exit_code == 1


def test_generate_schema_files_deterministic() -> None:
    first = generate_schema_files()
    second = generate_schema_files()
    assert first == second


def test_write_schema_files_creates_out_dir(tmp_path: Path) -> None:
    out_dir = tmp_path / "nested" / "schemas"
    write_schema_files(out_dir)
    assert (out_dir / "README.md").is_file()
    assert (out_dir / "ArtifactRef.schema.json").is_file()
