"""Tests for the filelist reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.adapters.tool.filelist import FilelistError, read_filelist


def test_read_empty_filelist(tmp_path: Path) -> None:
    """Read an empty filelist."""
    f = tmp_path / "empty.f"
    f.write_text("")
    result = read_filelist(f)
    assert result.sources == ()
    assert result.incdirs == ()
    assert result.defines == {}


def test_read_sources(tmp_path: Path) -> None:
    """Read source file paths."""
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "b.sv").write_text("")
    f = tmp_path / "test.f"
    f.write_text("a.sv\nb.sv\n")
    result = read_filelist(f)
    assert len(result.sources) == 2
    assert result.sources[0].name == "a.sv"
    assert result.sources[1].name == "b.sv"


def test_read_incdir(tmp_path: Path) -> None:
    """Read +incdir+ options."""
    (tmp_path / "inc1").mkdir()
    (tmp_path / "inc2").mkdir()
    f = tmp_path / "test.f"
    f.write_text("+incdir+inc1 +incdir+inc2\n")
    result = read_filelist(f)
    assert len(result.incdirs) == 2
    assert result.incdirs[0].name == "inc1"
    assert result.incdirs[1].name == "inc2"


def test_read_define_without_value(tmp_path: Path) -> None:
    """Read +define+ without a value."""
    f = tmp_path / "test.f"
    f.write_text("+define+RELEASE\n")
    result = read_filelist(f)
    assert result.defines == {"RELEASE": None}


def test_read_define_with_value(tmp_path: Path) -> None:
    """Read +define+ with a value."""
    f = tmp_path / "test.f"
    f.write_text("+define+WIDTH=32 +define+DEPTH=1024\n")
    result = read_filelist(f)
    assert result.defines == {"WIDTH": "32", "DEPTH": "1024"}


def test_read_nested_filelist(tmp_path: Path) -> None:
    """Read nested filelists with -f."""
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "b.sv").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.sv").write_text("")

    sub_f = tmp_path / "sub" / "sub.f"
    sub_f.write_text("c.sv\n")

    main_f = tmp_path / "main.f"
    main_f.write_text("a.sv\n-f sub/sub.f\nb.sv\n")

    result = read_filelist(main_f)
    assert len(result.sources) == 3
    assert result.sources[0].name == "a.sv"
    assert result.sources[1].name == "c.sv"
    assert result.sources[2].name == "b.sv"


def test_read_with_filelist_relative_style(tmp_path: Path) -> None:
    """Read paths relative to the filelist directory with -F style."""
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.sv").write_text("")
    (tmp_path / "sub" / "main.f").write_text("../a.sv\nb.sv\n")

    result = read_filelist(tmp_path / "sub" / "main.f", relative_to="filelist")
    assert len(result.sources) == 2
    assert result.sources[0].name == "a.sv"
    assert result.sources[1].name == "b.sv"


def test_read_with_cwd_relative_style(tmp_path: Path) -> None:
    """Read paths relative to cwd with -f style."""
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.sv").write_text("")
    (tmp_path / "sub" / "main.f").write_text("a.sv\nsub/b.sv\n")

    result = read_filelist(tmp_path / "sub" / "main.f", relative_to="cwd")
    # Paths resolve relative to cwd (which is tmp_path in tests typically)
    assert len(result.sources) == 2


def test_skip_unknown_option_with_warning(tmp_path: Path) -> None:
    """Unknown options are skipped with a warning."""
    f = tmp_path / "test.f"
    f.write_text("--top-module mytop\na.sv\n")
    (tmp_path / "a.sv").write_text("")

    result = read_filelist(f)
    assert result.sources[0].name == "a.sv"
    assert len(result.warnings) > 0
    assert "--top-module" in result.warnings[0] or "unsupported" in result.warnings[0]


def test_skip_unknown_flag_with_warning(tmp_path: Path) -> None:
    """Unknown flags are skipped with a warning."""
    f = tmp_path / "test.f"
    f.write_text("-Wno-dangerous-unknown\na.sv\n")
    (tmp_path / "a.sv").write_text("")

    result = read_filelist(f)
    assert result.sources[0].name == "a.sv"
    assert len(result.warnings) > 0


def test_comments_stripped(tmp_path: Path) -> None:
    """Comments are stripped from lines."""
    f = tmp_path / "test.f"
    f.write_text("# This is a comment\na.sv  // inline comment\n// Full line comment\nb.sv\n")
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "b.sv").write_text("")

    result = read_filelist(f)
    assert len(result.sources) == 2
    assert result.sources[0].name == "a.sv"
    assert result.sources[1].name == "b.sv"


def test_blank_lines_ignored(tmp_path: Path) -> None:
    """Blank lines are ignored."""
    f = tmp_path / "test.f"
    f.write_text("\n\na.sv\n\n\nb.sv\n\n")
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "b.sv").write_text("")

    result = read_filelist(f)
    assert len(result.sources) == 2


def test_environment_variable_expansion(tmp_path: Path) -> None:
    """Environment variables in paths are expanded."""
    import os

    os.environ["TEST_VAR"] = str(tmp_path / "src")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.sv").write_text("")

    f = tmp_path / "test.f"
    f.write_text("$TEST_VAR/a.sv\n")

    result = read_filelist(f)
    assert len(result.sources) == 1
    assert result.sources[0].name == "a.sv"


def test_filelist_not_found(tmp_path: Path) -> None:
    """FilelistError is raised for missing filelist."""
    with pytest.raises(FilelistError, match="not found"):
        read_filelist(tmp_path / "nonexistent.f")


def test_deduplicate_sources(tmp_path: Path) -> None:
    """Duplicate sources are deduplicated, keeping first occurrence."""
    (tmp_path / "a.sv").write_text("")
    f = tmp_path / "test.f"
    f.write_text("a.sv\na.sv\na.sv\n")

    result = read_filelist(f)
    assert len(result.sources) == 1


def test_deduplicate_incdirs(tmp_path: Path) -> None:
    """Duplicate incdirs are deduplicated."""
    (tmp_path / "inc").mkdir()
    f = tmp_path / "test.f"
    f.write_text("+incdir+inc +incdir+inc +incdir+inc\n")

    result = read_filelist(f)
    assert len(result.incdirs) == 1


def test_cycle_detection(tmp_path: Path) -> None:
    """Circular filelist includes are detected."""
    f1 = tmp_path / "f1.f"
    f2 = tmp_path / "f2.f"

    f1.write_text("-f f2.f\na.sv\n")
    f2.write_text("-f f1.f\n")
    (tmp_path / "a.sv").write_text("")

    result = read_filelist(f1)
    assert len(result.warnings) > 0
    assert "cycle" in result.warnings[0].lower()


def test_libdir_recorded_with_warning(tmp_path: Path) -> None:
    """Library directories (-y/-v) are recorded with a warning."""
    (tmp_path / "lib").mkdir()
    f = tmp_path / "test.f"
    f.write_text("-y lib\n")

    result = read_filelist(f)
    assert len(result.libdirs) == 1
    assert result.libdirs[0].name == "lib"
    assert len(result.warnings) > 0
    assert "library" in result.warnings[0].lower()


def test_libext_parsed(tmp_path: Path) -> None:
    """Library extensions (+libext+) are parsed."""
    f = tmp_path / "test.f"
    f.write_text("+libext+.v+.sv\n")

    result = read_filelist(f)
    assert result.libexts == (".v", ".sv")
