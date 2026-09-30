"""Tests for `chipgraph.app.ingest`: wiring the profile to the extractors on tinysoc."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.core.model.query import ModelQuery
from chipgraph.core.model.store import ModelStore, default_model_db_path

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _copy_tinysoc(dest: Path) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest


def _git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def tinysoc(tmp_path: Path) -> Path:
    return _copy_tinysoc(tmp_path / "tinysoc")


def test_ingest_builds_owned_modules_and_specs(tinysoc: Path) -> None:
    ctx = AppContext.load(tinysoc)
    report = run_ingest(ctx)
    model = report.model

    # Blocks and their owned modules.
    assert model.get("module:tiny_timer").block == "block:timer"
    assert model.get("module:tiny_gpio").block == "block:gpio"
    assert model.get("module:tiny_top").block == "block:top"

    # RTL ports and spec ports live under different keys (M1-07 ports_diff relies on it).
    assert model.get("port:tiny_timer.clk") is not None
    assert model.get("port:spec.timer.clk") is not None

    # A clock and an active-low reset.
    assert model.get("clock:clk") is not None
    assert model.get("reset:rst_n") is not None

    # MAS requirements from both blocks.
    req_names = {e.name for e in model.by_kind("requirement")}
    assert {f"REQ-TIM-00{i}" for i in range(1, 6)} <= req_names
    assert {f"REQ-GPIO-00{i}" for i in range(1, 4)} <= req_names


def test_ingest_query_block_returns_its_module_and_ports(tinysoc: Path) -> None:
    ctx = AppContext.load(tinysoc)
    run_ingest(ctx)
    store = ModelStore(default_model_db_path(tinysoc))
    query = ModelQuery(store.read(), store)
    info = query.block("timer")
    assert "module:tiny_timer" in info.modules
    assert any(p.startswith("port:tiny_timer.") for p in info.ports)


def test_ingest_writes_only_under_state_cache_and_leaves_tree_clean(tinysoc: Path) -> None:
    ctx = AppContext.load(tinysoc)
    run_ingest(ctx)
    # The model db is under .chipgraph/state/cache (already git-ignored by the backend).
    assert default_model_db_path(tinysoc).is_file()
    assert _git_status(tinysoc) == ""


def test_ingest_is_deterministic_after_deleting_the_cache(tinysoc: Path) -> None:
    ctx = AppContext.load(tinysoc)
    run_ingest(ctx)
    first = _snapshot(tinysoc)

    shutil.rmtree(tinysoc / ".chipgraph" / "state" / "cache")
    run_ingest(AppContext.load(tinysoc))
    second = _snapshot(tinysoc)

    assert first == second


def _snapshot(root: Path) -> tuple[dict[str, str], list[str], str | None]:
    import sqlite3

    conn = sqlite3.connect(default_model_db_path(root))
    try:
        entities = {key: payload for key, payload in conn.execute("SELECT key, json FROM entities")}
        relations = sorted(payload for (payload,) in conn.execute("SELECT json FROM relations"))
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    return entities, relations, meta.get("build_inputs_hash")


def _set_profile(repo: Path, text: str) -> None:
    (repo / ".chipgraph.yml").write_text(text, encoding="utf-8")


def test_block_with_spec_opt_out_reports_nothing(tinysoc: Path) -> None:
    # tinysoc's `top` says `layout.spec: []`: no spec expected, nothing reported.
    report = run_ingest(AppContext.load(tinysoc))
    assert not [i for i in report.issues if i.block == "block:top" and "spec" in i.code]
    assert not report.has_error


def test_missing_spec_and_filelist_are_warnings(tinysoc: Path) -> None:
    profile = (tinysoc / ".chipgraph.yml").read_text(encoding="utf-8")
    _set_profile(tinysoc, profile + "  uart: {}\n")
    report = run_ingest(AppContext.load(tinysoc))
    by_code = {(i.code, i.block): i for i in report.issues}
    spec = by_code[("missing_spec", "block:uart")]
    assert spec.severity == "warning"
    assert spec.file == "doc/specs/TINY_UART_MAS.md"
    assert by_code[("missing_filelist", "block:uart")].severity == "warning"
    assert not report.has_error


def test_per_block_spec_list_reads_every_file(tinysoc: Path) -> None:
    extra = tinysoc / "doc" / "specs" / "TIMER_EXTRA.md"
    extra.write_text("# 7. Behaviour\n\n`REQ-TIM-900` The timer also does this.\n")
    profile = (tinysoc / ".chipgraph.yml").read_text(encoding="utf-8")
    profile = profile.replace(
        "  timer: {}\n",
        "  timer:\n    layout:\n      spec: [doc/specs/TINY_TIMER_MAS.md, doc/specs/TIMER_EXTRA.md]\n",
    )
    _set_profile(tinysoc, profile)
    report = run_ingest(AppContext.load(tinysoc))
    reqs = {e.key for e in report.model.by_kind("requirement")}
    assert {"requirement:REQ-TIM-001", "requirement:REQ-TIM-900"} <= reqs


def test_spec_matching_the_template_but_unread_is_a_warning(tinysoc: Path) -> None:
    orphan = tinysoc / "doc" / "specs" / "TINY_UART_MAS.md"
    orphan.write_text("# 1. Overview\n")
    template = tinysoc / "doc" / "specs" / "TINY_TEMPLATE_MAS.md"
    template.write_text("# 1. Overview\n")
    profile = (tinysoc / ".chipgraph.yml").read_text(encoding="utf-8")
    profile += (
        "paths:\n"
        '  "doc/specs/TINY_TEMPLATE_MAS.md":\n'
        "    checks: { ingest: off }\n"
        "    reason: the template, not a spec\n"
    )
    _set_profile(tinysoc, profile)
    report = run_ingest(AppContext.load(tinysoc))
    unread = [i.file for i in report.issues if i.code == "unread_spec"]
    assert unread == ["doc/specs/TINY_UART_MAS.md"]
