"""Tests for `chipgraph agents-md` (task M1-15): print, --write, --check, --path."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.packs.spec_core.gen.agents_md import BEGIN_MARKER, END_MARKER

runner = CliRunner()

_TINYSOC = Path(__file__).resolve().parents[3] / "examples" / "tinysoc"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed copy of examples/tinysoc in its own git repo."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "tinysoc"
    shutil.copytree(_TINYSOC, root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return root


def _run(root: Path, *args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, ["-C", str(root), "agents-md", *args])
    return result.exit_code, result.stdout, result.stderr


def test_default_prints_the_file_and_writes_nothing(project: Path) -> None:
    ignored_before = _git(project, "status", "--porcelain", "--ignored")
    code, out, _ = _run(project)
    assert code == 0
    assert out.startswith(BEGIN_MARKER + "\n")
    assert out.endswith(END_MARKER + "\n")
    assert "- Project: `tinysoc`" in out
    assert not (project / "AGENTS.md").exists()
    assert _git(project, "status", "--porcelain") == ""
    # Not even an ignored file (e.g. a state directory) appears.
    assert _git(project, "status", "--porcelain", "--ignored") == ignored_before


def test_default_prints_team_file_with_block_but_leaves_it(project: Path) -> None:
    team = "# Team\n\nOur own rules.\n"
    (project / "AGENTS.md").write_text(team, encoding="utf-8")
    _git(project, "add", "AGENTS.md")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "a")
    code, out, _ = _run(project)
    assert code == 0
    assert out.startswith(team + "\n" + BEGIN_MARKER)
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == team
    assert _git(project, "status", "--porcelain") == ""


def test_check_fails_when_missing_then_write_then_check_passes(project: Path) -> None:
    code, _, err = _run(project, "--check")
    assert code == 1
    assert "out of date" in err

    code, out, _ = _run(project, "--write")
    assert code == 0
    assert out.strip() == "wrote AGENTS.md"
    written = (project / "AGENTS.md").read_bytes()
    printed = _run(project)[1]
    assert written.decode("utf-8") == printed

    code, out, _ = _run(project, "--check")
    assert code == 0
    assert "up to date" in out


def test_write_twice_does_not_touch_the_file(project: Path) -> None:
    assert _run(project, "--write")[0] == 0
    path = project / "AGENTS.md"
    before = path.stat().st_mtime_ns
    content = path.read_bytes()
    code, out, _ = _run(project, "--write")
    assert code == 0
    assert "up to date" in out
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == before


def test_check_fails_after_a_hand_edit_inside_the_block(project: Path) -> None:
    assert _run(project, "--write")[0] == 0
    path = project / "AGENTS.md"
    path.write_text(path.read_text().replace("tinysoc", "edited", 1), encoding="utf-8")
    assert _run(project, "--check")[0] == 1


def test_team_text_outside_the_block_is_kept_on_write(project: Path) -> None:
    before = "# Team AGENTS.md\n\nHand-written rules.\n\n"
    after = "\n## Notes\n\nMore team text.\n"
    path = project / "AGENTS.md"
    path.write_bytes(f"{before}{BEGIN_MARKER}\nstale\n{END_MARKER}{after}".encode())
    assert _run(project, "--write")[0] == 0
    text = path.read_bytes().decode("utf-8")
    assert text.startswith(before + BEGIN_MARKER + "\n")
    assert text.endswith(END_MARKER + after)
    assert "stale" not in text
    assert _run(project, "--check")[0] == 0


def test_path_option_manages_another_file(project: Path) -> None:
    (project / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    assert _run(project, "--path", "CLAUDE.md", "--write")[0] == 0
    text = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# Claude\n\n" + BEGIN_MARKER)
    assert not (project / "AGENTS.md").exists()
    assert _run(project, "--path", "CLAUDE.md", "--check")[0] == 0


def test_malformed_markers_exit_2_and_write_nothing(project: Path) -> None:
    path = project / "AGENTS.md"
    original = f"team\n{BEGIN_MARKER}\nno end\n"
    path.write_text(original, encoding="utf-8")
    for args in ((), ("--write",), ("--check",)):
        code, out, err = _run(project, *args)
        assert code == 2, args
        assert "has no matching" in err
        assert out == ""
    assert path.read_text(encoding="utf-8") == original


def test_write_and_check_together_is_a_usage_error(project: Path) -> None:
    assert _run(project, "--write", "--check")[0] == 2


def test_no_profile_is_an_error(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    code, _, err = _run(tmp_path)
    assert code == 2
    assert ".chipgraph.yml" in err


def test_help_says_a_pr_is_out_of_scope() -> None:
    # CI may force colour (rich splits option names with ANSI codes): strip them.
    result = runner.invoke(
        app, ["agents-md", "--help"], env={"TERMINAL_WIDTH": "200", "NO_COLOR": "1"}
    )
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    flat = " ".join(plain.replace("\u2502", " ").split())
    assert "Opening a PR is out of scope" in flat
    for option in ("--write", "--check", "--path"):
        assert option in flat
