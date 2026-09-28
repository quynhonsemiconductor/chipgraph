"""End-to-end tests for the chipgraph CLI (task M0-12), via `typer.testing.CliRunner`."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import git_status, init_git, write_profile
from typer.testing import CliRunner

from chipgraph.cli import app

runner = CliRunner()

_PACK_MANIFEST = {"name": "demo", "version": "0.1.0", "provides": {"rules": ["rules"]}}

_GEN_CMD = [
    "python3",
    "-c",
    "import pathlib; pathlib.Path('out').mkdir(exist_ok=True); "
    "pathlib.Path('out/{block}.txt').write_text('hi')",
]


def _write_pack(root: Path, *, gated: bool = False) -> None:
    pack_dir = root / ".chipgraph" / "packs" / "demo"
    (pack_dir / "rules").mkdir(parents=True)
    (pack_dir / "pack.yml").write_text(yaml.safe_dump(_PACK_MANIFEST))
    body: dict[str, object] = {
        "rule": "gen_out",
        "kind": "gen",
        "foreach": "blocks",
        "outputs": ["out/{block}.txt"],
        "run": {"use": "cmd", "args": {"cmd": _GEN_CMD}},
    }
    if gated:
        body["inputs"] = [{"path": "spec/{block}.md"}]
        body["gate"] = "spec:{block}"
    (pack_dir / "rules" / "gen_out.yml").write_text(yaml.safe_dump(body))


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "init",
        "check",
        "build",
        "resume",
        "rewind",
        "status",
        "approve",
        "doctor",
        "config",
    ):
        assert name in result.output


def test_init_creates_file_and_refuses_to_overwrite(tmp_path: Path) -> None:
    init_git(tmp_path)

    result = runner.invoke(app, ["-C", str(tmp_path), "init", "--project", "demo"])
    assert result.exit_code == 0, result.output
    profile_path = tmp_path / ".chipgraph.yml"
    assert profile_path.is_file()
    assert "project: demo" in profile_path.read_text(encoding="utf-8")

    result_no_force = runner.invoke(app, ["-C", str(tmp_path), "init"])
    assert result_no_force.exit_code == 2

    result_force = runner.invoke(
        app, ["-C", str(tmp_path), "init", "--force", "--project", "demo2"]
    )
    assert result_force.exit_code == 0
    assert "project: demo2" in profile_path.read_text(encoding="utf-8")


def test_config_show_and_explain(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")

    show = runner.invoke(app, ["-C", str(tmp_path), "config", "show"])
    assert show.exit_code == 0
    assert "project: demo" in show.output

    explain = runner.invoke(app, ["-C", str(tmp_path), "config", "show", "--explain"])
    assert explain.exit_code == 0
    assert "project = 'demo'" in explain.output


def test_config_check_exit_codes(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ok = runner.invoke(app, ["-C", str(tmp_path), "config", "check"])
    assert ok.exit_code == 0
    assert "no issues" in ok.output

    write_profile(
        tmp_path,
        "project: demo\n"
        "layout:\n"
        "  rtl: 'design/{block}/rtl/m.sv'\n"
        "  tb: 'design/{block}/rtl/m.sv'\n",
    )
    bad = runner.invoke(app, ["-C", str(tmp_path), "config", "check"])
    assert bad.exit_code == 1
    assert "layout.tb" in bad.output


def test_check_pass_fail_and_json(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(
        tmp_path,
        "project: demo\n"
        "adapters:\n"
        "  ok:\n"
        "    use: cmd\n"
        "    cmd: [python3, -c, pass]\n"
        "  bad:\n"
        "    use: cmd\n"
        "    cmd: [python3, -c, 'import sys; sys.exit(1)']\n",
    )

    ok = runner.invoke(app, ["-C", str(tmp_path), "check", "--only", "ok"])
    assert ok.exit_code == 0
    assert "PASS" in ok.output

    bad = runner.invoke(app, ["-C", str(tmp_path), "check", "--only", "bad"])
    assert bad.exit_code == 1
    assert "FAIL" in bad.output

    as_json = runner.invoke(app, ["--json", "-C", str(tmp_path), "check", "--only", "ok"])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert payload[0]["check_id"] == "ok"
    assert payload[0]["status"] == "pass"


def test_build_status_rewind_build(tmp_path: Path) -> None:
    init_git(tmp_path)
    _write_pack(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")

    build1 = runner.invoke(app, ["-C", str(tmp_path), "build", "demo/gen_out"])
    assert build1.exit_code == 0, build1.output
    assert (tmp_path / "out" / "a.txt").is_file()

    status1 = runner.invoke(app, ["-C", str(tmp_path), "status"])
    assert status1.exit_code == 0
    assert "demo/gen_out[block=a]" in status1.output
    assert "done" in status1.output

    rewound = runner.invoke(app, ["-C", str(tmp_path), "rewind", "demo/gen_out[block=a]"])
    assert rewound.exit_code == 0
    assert "demo/gen_out[block=a]" in rewound.output

    build2 = runner.invoke(app, ["-C", str(tmp_path), "build", "demo/gen_out"])
    assert build2.exit_code == 0
    assert "demo/gen_out[block=a]" in build2.output


def test_approve_makes_a_waiting_gate_pass_on_next_build(tmp_path: Path) -> None:
    init_git(tmp_path)
    _write_pack(tmp_path, gated=True)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "a.md").write_text("spec for a", encoding="utf-8")
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")

    build1 = runner.invoke(app, ["-C", str(tmp_path), "build", "demo/gen_out"])
    assert build1.exit_code == 1
    assert "waiting" in build1.output

    approved = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "approve",
            "spec:a",
            "--instance",
            "demo/gen_out[block=a]",
            "--by",
            "tester",
        ],
    )
    assert approved.exit_code == 0, approved.output

    build2 = runner.invoke(app, ["-C", str(tmp_path), "build", "demo/gen_out"])
    assert build2.exit_code == 0, build2.output
    assert (tmp_path / "out" / "a.txt").is_file()


def test_doctor_reports_python_and_git(tmp_path: Path) -> None:
    init_git(tmp_path)
    result = runner.invoke(app, ["-C", str(tmp_path), "doctor"])
    assert "python" in result.output
    assert "git" in result.output
    # No profile in this repo, so doctor reports it missing and exits 1.
    assert result.exit_code == 1
    assert "profile" in result.output


def test_no_profile_read_only_works_write_commands_exit_2(tmp_path: Path) -> None:
    init_git(tmp_path)
    before = git_status(tmp_path)

    status_result = runner.invoke(app, ["-C", str(tmp_path), "status"])
    assert status_result.exit_code == 0

    doctor_result = runner.invoke(app, ["-C", str(tmp_path), "doctor"])
    assert doctor_result.exit_code == 1  # ran fine; just reports a missing profile

    build_result = runner.invoke(app, ["-C", str(tmp_path), "build", "*"])
    assert build_result.exit_code == 2
    assert "chipgraph init" in build_result.output

    assert git_status(tmp_path) == before == ""
