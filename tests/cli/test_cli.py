"""End-to-end tests for the chipgraph CLI (task M0-12), via `typer.testing.CliRunner`."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
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


def test_build_prints_handoff_path(tmp_path: Path) -> None:
    init_git(tmp_path)
    _write_pack(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")

    build1 = runner.invoke(app, ["-C", str(tmp_path), "build", "demo/gen_out"])
    assert build1.exit_code == 0, build1.output
    runs_dir = tmp_path / ".chipgraph" / "state" / "runs"
    run_ids = [p.name for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_ids) == 1
    expected = runs_dir / run_ids[0] / "HANDOFF.md"
    assert expected.is_file()
    assert str(expected) in build1.output

    as_json = runner.invoke(app, ["--json", "-C", str(tmp_path), "build", "demo/gen_out"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["handoff"].endswith("HANDOFF.md")
    assert Path(payload["handoff"]).is_file()


def _invoke_subprocess(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the real `chipgraph` CLI in a subprocess.

    `ConsoleSpanExporter` defaults its `out` param to `sys.stdout` bound at import
    time (opentelemetry's own code, not chipgraph's), which is the real process
    stdout, not `typer.testing.CliRunner`'s in-memory buffer; a real subprocess (a
    real OS pipe for fd 1) is what actually observes it.
    """
    return subprocess.run(
        [sys.executable, "-c", "from chipgraph.cli import app; app()", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_trace_console_prints_spans_unless_offline(tmp_path: Path) -> None:
    init_git(tmp_path)
    _write_pack(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")

    traced = _invoke_subprocess(tmp_path, ["--trace", "console", "build", "demo/gen_out"])
    assert traced.returncode == 0, traced.stdout + traced.stderr
    assert '"name": "chipgraph.run"' in traced.stdout

    plain = _invoke_subprocess(tmp_path, ["build", "demo/gen_out"])
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert '"name": "chipgraph.run"' not in plain.stdout


def test_offline_profile_disables_tracing_even_with_trace_console(tmp_path: Path) -> None:
    init_git(tmp_path)
    _write_pack(tmp_path)
    write_profile(tmp_path, "project: demo\noffline: true\npacks: [demo]\nblocks:\n  a: {}\n")

    result = _invoke_subprocess(tmp_path, ["--trace", "console", "build", "demo/gen_out"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"name": "chipgraph.run"' not in result.stdout


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


def test_doctor_checks_python3_for_the_write_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")  # runtime defaults to claude-code
    ok = runner.invoke(app, ["--json", "-C", str(tmp_path), "doctor"])
    rows = {r["check"]: r for r in json.loads(ok.output)}
    assert rows["python3 for the write guard"]["ok"] is True

    real_which = shutil.which
    monkeypatch.setattr(
        "chipgraph.cli.shutil.which", lambda n: None if n == "python3" else real_which(n)
    )
    missing = runner.invoke(app, ["--json", "-C", str(tmp_path), "doctor"])
    assert missing.exit_code == 1
    rows = {r["check"]: r for r in json.loads(missing.output)}
    assert rows["python3 for the write guard"]["ok"] is False
    assert "write guard cannot run" in rows["python3 for the write guard"]["detail"]


def test_doctor_skips_python3_for_an_api_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\nruntime: generic\n")
    monkeypatch.setattr("chipgraph.cli.shutil.which", lambda n: None if n == "python3" else "/x")
    result = runner.invoke(app, ["--json", "-C", str(tmp_path), "doctor"])
    assert "python3 for the write guard" not in {r["check"] for r in json.loads(result.output)}
