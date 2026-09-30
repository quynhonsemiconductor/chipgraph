"""CLI tests for `chipgraph baseline` (M1-24).

Most tests run the real CLI against a git copy of `examples/tinysoc` in `tmp_path`,
with the example's shipped `baseline` decisions removed so the gates start out waiting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app
from chipgraph.core.contracts import ArtifactRef
from chipgraph.core.contracts.finding import Evidence, Finding
from chipgraph.core.state.findings import FindingStore
from chipgraph.core.state.layout import StateLayout

runner = CliRunner()

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    shutil.rmtree(dest / ".chipgraph" / "decisions", ignore_errors=True)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    _git(dest, "config", "user.email", "t@example.invalid")
    _git(dest, "config", "user.name", "t")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "initial import")
    return dest


def _status(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout


def _decisions(root: Path) -> list[Path]:
    d = root / ".chipgraph" / "decisions"
    return sorted(d.glob("*.yml")) if d.is_dir() else []


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _copy_tinysoc(tmp_path / "tinysoc")


def test_dry_run_lists_gates_and_writes_nothing(repo: Path) -> None:
    before_status = _status(repo)
    result = runner.invoke(app, ["-C", str(repo), "baseline"])
    assert result.exit_code == 0, result.output
    assert "spec:timer" in result.output
    assert "doc/specs/TINY_TIMER_MAS.md" in result.output
    assert "would baseline 3 gate(s)" in result.output

    # Dry run is read-only: git status and the decisions dir are unchanged.
    assert _status(repo) == before_status
    assert _decisions(repo) == []


def test_dry_run_json_is_deterministic(repo: Path) -> None:
    result = runner.invoke(app, ["--json", "-C", str(repo), "baseline"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    gate_ids = [g["gate_id"] for g in payload["gates"]]
    assert gate_ids == sorted(gate_ids)
    assert set(payload["gates_to_baseline"]) == {"spec:timer", "spec:gpio", "spec:top"}
    assert payload["open_findings"] == 0


def test_confirm_writes_decisions_and_build_passes_the_gate(repo: Path) -> None:
    confirm = runner.invoke(app, ["-C", str(repo), "baseline", "--confirm", "--by", "lead"])
    assert confirm.exit_code == 0, confirm.output
    assert _decisions(repo)  # decisions were written

    # Building the timer RTL now passes its spec gate.
    build = runner.invoke(app, ["--json", "-C", str(repo), "build", "tinysoc/rtl[block=timer]"])
    assert build.exit_code == 0, build.output
    summary = json.loads(build.output)
    assert summary["waiting_gate"] == []
    assert "tinysoc/rtl[block=timer]" in summary["done"]


def test_confirm_is_idempotent(repo: Path) -> None:
    first = runner.invoke(app, ["-C", str(repo), "baseline", "--confirm"])
    assert first.exit_code == 0, first.output
    n_first = len(_decisions(repo))
    assert n_first == 3

    second = runner.invoke(app, ["-C", str(repo), "baseline", "--confirm"])
    assert second.exit_code == 0, second.output
    assert "0 gates" in second.output
    assert len(_decisions(repo)) == n_first  # nothing new written


def test_editing_the_mas_returns_the_gate_to_waiting(repo: Path) -> None:
    runner.invoke(app, ["-C", str(repo), "baseline", "--confirm"])
    build = runner.invoke(app, ["--json", "-C", str(repo), "build", "tinysoc/rtl[block=timer]"])
    assert json.loads(build.output)["waiting_gate"] == []

    mas = repo / "doc" / "specs" / "TINY_TIMER_MAS.md"
    mas.write_text(mas.read_text() + "\n<!-- edited -->\n")
    build2 = runner.invoke(app, ["--json", "-C", str(repo), "build", "tinysoc/rtl[block=timer]"])
    assert json.loads(build2.output)["waiting_gate"] == ["tinysoc/rtl[block=timer]"]


def test_dirty_artifact_is_listed_and_not_baselined(repo: Path) -> None:
    mas = repo / "doc" / "specs" / "TINY_TIMER_MAS.md"
    mas.write_text(mas.read_text() + "\n<!-- uncommitted -->\n")

    result = runner.invoke(app, ["--json", "-C", str(repo), "baseline"])
    payload = json.loads(result.output)
    dirty = [a["path"] for a in payload["dirty_artifacts"]]
    assert "doc/specs/TINY_TIMER_MAS.md" in dirty


def test_refuses_on_a_non_main_branch(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feature/x")
    result = runner.invoke(app, ["-C", str(repo), "baseline"])
    assert result.exit_code == 1
    assert "refusing to baseline" in result.output
    # --allow-branch overrides.
    ok = runner.invoke(app, ["-C", str(repo), "baseline", "--allow-branch"])
    assert ok.exit_code == 0, ok.output


def test_no_profile_exits_2(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = runner.invoke(app, ["-C", str(tmp_path), "baseline"])
    assert result.exit_code == 2
    assert ".chipgraph.yml" in result.output


def test_open_findings_are_baselined_and_still_listed(repo: Path) -> None:
    # Seed one open finding in the store, bound to a real tracked artifact.
    now = datetime.now(UTC)
    finding = Finding(
        layer=4,
        severity="warning",
        source="check:lint",
        evidence=(Evidence(file="rtl/tiny_timer.sv", line=1, note="demo"),),
        claim="a pre-existing lint smell",
        artifacts=(ArtifactRef(kind="rtl", path="rtl/tiny_timer.sv"),),
        first_seen=now,
        last_seen=now,
    )
    FindingStore(StateLayout(repo)).upsert((finding,))

    dry = runner.invoke(app, ["--json", "-C", str(repo), "baseline"])
    assert json.loads(dry.output)["open_findings"] == 1

    confirm = runner.invoke(app, ["--json", "-C", str(repo), "baseline", "--confirm", "--by", "l"])
    assert confirm.exit_code == 0, confirm.output
    assert finding.id in json.loads(confirm.output)["findings_baselined"]

    # It is recorded (a baseline decision exists) but still listed as open.
    listed = runner.invoke(app, ["--json", "-C", str(repo), "findings", "--status", "open"])
    ids = [row["id"] for row in json.loads(listed.output)]
    assert finding.id in ids
