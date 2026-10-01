"""M1-16: the MCP `init` tool behind `/chipgraph:init-chipgraph`.

It wraps `chipgraph init`: a preview first (nothing written), the write only with
`confirm=true`, never an overwrite without `force=true`, and `from_learn` like
`chipgraph init --from-learn`. It must work in a project with no `.chipgraph.yml`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.mcp.server import build_server


def _git_init_main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)


def _prefixed_repo(root: Path) -> Path:
    """A small repo `chipgraph learn` infers a layout and naming rules from."""
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/blk_timer.sv").write_text(
        "// SPDX-License-Identifier: Apache-2.0\n"
        "module blk_timer(input logic i_clk, output logic o_irq);\n"
        "  logic r_count;\n"
        "  blk_sub u_sub(.i_clk(i_clk));\n"
        "  assign o_irq = r_count;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    _git_init_main(root)
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _calls(root: Path, *calls: tuple[str, dict[str, Any]]) -> list[tuple[bool, Any]]:
    """Run tool calls in one MCP session; each gives (is_error, structured or text)."""

    async def _go() -> list[tuple[bool, Any]]:
        out: list[tuple[bool, Any]] = []
        async with Client(build_server(root)) as client:
            for name, args in calls:
                result = await client.call_tool(name, args)
                if result.is_error:
                    text = "".join(getattr(b, "text", "") for b in result.content)
                    out.append((True, text))
                else:
                    out.append((False, result.structured_content))
        return out

    return asyncio.run(_go())


def test_preview_in_a_project_with_no_config_writes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "chip"
    _git_init_main(repo)
    before = _tree(repo)

    [(err, reply)] = _calls(repo, ("init", {}))

    assert not err, reply
    assert reply["exists"] is False
    assert reply["written"] is False
    assert "project: chip" in reply["content"]
    assert "confirm=true" in reply["message"]
    assert _tree(repo) == before
    assert not (repo / ".chipgraph.yml").exists()


def test_preview_is_the_text_the_cli_writes(tmp_path: Path) -> None:
    via_mcp = tmp_path / "a" / "soc"
    via_cli = tmp_path / "b" / "soc"
    _git_init_main(via_mcp)
    _git_init_main(via_cli)

    [(err, reply)] = _calls(via_mcp, ("init", {"project": "demo", "preset": "small"}))
    cli = CliRunner().invoke(
        app, ["-C", str(via_cli), "init", "--project", "demo", "--preset", "small"]
    )

    assert not err, reply
    assert cli.exit_code == 0, cli.output
    assert reply["content"] == (via_cli / ".chipgraph.yml").read_text(encoding="utf-8")


def test_confirm_writes_and_the_next_call_sees_the_profile(tmp_path: Path) -> None:
    repo = tmp_path / "chip"
    _git_init_main(repo)

    (err0, preview), (err1, written), (err2, shown) = _calls(
        repo,
        ("init", {"project": "demo"}),
        ("init", {"project": "demo", "confirm": True}),
        ("config_show", {}),
    )

    assert not (err0 or err1 or err2), (preview, written, shown)
    assert written["written"] is True
    assert written["files_written"] == [".chipgraph.yml"]
    assert (repo / ".chipgraph.yml").read_text(encoding="utf-8") == preview["content"]
    assert written["next_steps"]
    # The server reloads the project per call: the new profile is live at once.
    assert shown["profile"]["project"] == "demo"


def test_never_overwrites_without_force(tmp_path: Path) -> None:
    repo = tmp_path / "chip"
    _git_init_main(repo)
    (repo / ".chipgraph.yml").write_text("project: mine\n", encoding="utf-8")

    (err0, preview), (err1, refused) = _calls(
        repo,
        ("init", {}),
        ("init", {"confirm": True}),
    )

    assert not err0
    assert preview["exists"] is True
    assert preview["written"] is False
    assert "force=true" in preview["message"]
    assert err1 is True
    assert "already exists" in refused
    assert (repo / ".chipgraph.yml").read_text(encoding="utf-8") == "project: mine\n"


def test_force_and_confirm_overwrite(tmp_path: Path) -> None:
    repo = tmp_path / "chip"
    _git_init_main(repo)
    (repo / ".chipgraph.yml").write_text("project: mine\n", encoding="utf-8")

    [(err, reply)] = _calls(repo, ("init", {"project": "new", "confirm": True, "force": True}))

    assert not err, reply
    assert reply["written"] is True
    assert "project: new" in (repo / ".chipgraph.yml").read_text(encoding="utf-8")


def test_from_learn_preview_matches_what_is_written(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    before = _tree(repo)

    (err0, preview), (err1, written) = _calls(
        repo,
        ("init", {"from_learn": True}),
        ("init", {"from_learn": True, "confirm": True}),
    )

    assert not (err0 or err1), (preview, written)
    assert preview["written"] is False
    # The preview wrote nothing; the confirmed call wrote exactly the previewed files.
    assert set(_tree(repo)) - set(before) == {".chipgraph.yml", ".chipgraph/naming.yml"}
    assert (repo / ".chipgraph.yml").read_text(encoding="utf-8") == preview["content"]
    [naming] = preview["other_files"]
    assert naming["path"] == ".chipgraph/naming.yml"
    assert (repo / naming["path"]).read_text(encoding="utf-8") == naming["content"]
    assert written["files_written"] == [".chipgraph.yml", ".chipgraph/naming.yml"]
    check = CliRunner().invoke(app, ["-C", str(repo), "config", "check"])
    assert check.exit_code == 0, check.output


def test_from_learn_preview_alone_writes_nothing(tmp_path: Path) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    before = _tree(repo)

    [(err, reply)] = _calls(repo, ("init", {"from_learn": True}))

    assert not err, reply
    assert "inferred from the repo" in reply["content"]
    assert _tree(repo) == before


def test_from_learn_force_overwrites_and_keeps_the_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _prefixed_repo(tmp_path / "repo")
    (repo / ".chipgraph.yml").write_text("project: mine\n", encoding="utf-8")

    def _boom(root: Path) -> object:
        raise RuntimeError("learn failed")

    monkeypatch.setattr("chipgraph.learn.init_from_learn", _boom)
    [(err, _)] = _calls(repo, ("init", {"from_learn": True, "confirm": True, "force": True}))
    assert err is True
    assert (repo / ".chipgraph.yml").read_text(encoding="utf-8") == "project: mine\n"

    monkeypatch.undo()
    [(err, reply)] = _calls(repo, ("init", {"from_learn": True, "confirm": True, "force": True}))
    assert not err, reply
    assert "inferred from the repo" in (repo / ".chipgraph.yml").read_text(encoding="utf-8")
