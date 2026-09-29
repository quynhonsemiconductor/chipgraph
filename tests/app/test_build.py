"""Tests for `chipgraph.app.build`: loading rules from packs, and `make_scheduler`."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml
from conftest import init_git, write_profile

import chipgraph.packs
from chipgraph.app.build import builtin_packs_dir, load_rules, make_scheduler, pack_search_paths
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError

_PACK_MANIFEST = {"name": "demo", "version": "0.1.0", "provides": {"rules": ["rules"]}}


def _write_pack(pack_dir: Path, rule_body: dict) -> None:
    (pack_dir / "rules").mkdir(parents=True)
    (pack_dir / "pack.yml").write_text(yaml.safe_dump(_PACK_MANIFEST))
    (pack_dir / "rules" / "gen_out.yml").write_text(yaml.safe_dump(rule_body))


_SIMPLE_RULE = {
    "rule": "gen_out",
    "kind": "gen",
    "foreach": "blocks",
    "inputs": [{"path": "spec/in.txt"}],
    "outputs": ["out/{block}.txt"],
    "run": {
        "use": "cmd",
        "args": {
            "cmd": [
                "python3",
                "-c",
                "import pathlib; pathlib.Path('out').mkdir(exist_ok=True); "
                "pathlib.Path('out/{block}.txt').write_text('hi')",
            ]
        },
    },
}


def test_load_rules_from_chipgraph_pack_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs_root = tmp_path / "external-packs"
    _write_pack(packs_root / "demo", _SIMPLE_RULE)
    monkeypatch.setenv("CHIPGRAPH_PACK_PATH", str(packs_root))

    repo = tmp_path / "repo"
    repo.mkdir()
    init_git(repo)
    write_profile(repo, "project: demo\npacks: [demo]\n")

    ctx = AppContext.load(repo)
    rules = load_rules(ctx)

    assert [rule.id for rule in rules] == ["demo/gen_out"]


def test_load_rules_missing_pack_raises_app_error(tmp_path: Path) -> None:
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [does-not-exist]\n")
    ctx = AppContext.load(tmp_path)

    with pytest.raises(AppError, match="does-not-exist"):
        load_rules(ctx)


def test_make_scheduler_and_run_builds_one_instance_per_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pack(tmp_path / ".chipgraph" / "packs" / "demo", _SIMPLE_RULE)
    init_git(tmp_path)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "in.txt").write_text("hello")
    write_profile(
        tmp_path,
        "project: demo\npacks: [demo]\nblocks:\n  a: {}\n  b: {}\n",
    )
    monkeypatch.delenv("CHIPGRAPH_PACK_PATH", raising=False)

    ctx = AppContext.load(tmp_path)
    scheduler = make_scheduler(ctx, "*")
    summary = asyncio.run(scheduler.run("*"))

    assert set(summary.done) == {"demo/gen_out[block=a]", "demo/gen_out[block=b]"}
    assert summary.ok
    assert (tmp_path / "out" / "a.txt").read_text() == "hi"
    assert (tmp_path / "out" / "b.txt").read_text() == "hi"

    scheduler2 = make_scheduler(ctx, "*")
    summary2 = asyncio.run(scheduler2.run("*"))
    assert set(summary2.skipped_fresh) == {"demo/gen_out[block=a]", "demo/gen_out[block=b]"}
    assert not summary2.done


def test_make_scheduler_foreach_other_than_blocks_needs_design_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = dict(_SIMPLE_RULE)
    rule["foreach"] = "model.plan(block).modules"
    _write_pack(tmp_path / ".chipgraph" / "packs" / "demo", rule)
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\npacks: [demo]\nblocks:\n  a: {}\n")
    monkeypatch.delenv("CHIPGRAPH_PACK_PATH", raising=False)

    ctx = AppContext.load(tmp_path)
    with pytest.raises(AppError, match="Design Model"):
        make_scheduler(ctx, "*")


def test_builtin_packs_dir_is_the_chipgraph_packs_package() -> None:
    assert builtin_packs_dir() == Path(chipgraph.packs.__file__).parent


def test_pack_search_paths_order_project_builtin_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra_a, extra_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("CHIPGRAPH_PACK_PATH", f"{extra_a}{os.pathsep}{extra_b}")
    init_git(tmp_path)
    write_profile(tmp_path, "project: demo\n")
    ctx = AppContext.load(tmp_path)

    assert pack_search_paths(ctx) == [
        tmp_path / ".chipgraph" / "packs",
        builtin_packs_dir(),
        extra_a,
        extra_b,
    ]


def test_load_rules_finds_a_builtin_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = tmp_path / "builtin"
    _write_pack(builtin / "demo", _SIMPLE_RULE)
    monkeypatch.setattr("chipgraph.app.build.builtin_packs_dir", lambda: builtin)
    monkeypatch.delenv("CHIPGRAPH_PACK_PATH", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git(repo)
    write_profile(repo, "project: demo\npacks: [demo]\n")

    assert [rule.id for rule in load_rules(AppContext.load(repo))] == ["demo/gen_out"]


def test_project_pack_with_builtin_name_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin = tmp_path / "builtin"
    _write_pack(builtin / "demo", _SIMPLE_RULE)
    monkeypatch.setattr("chipgraph.app.build.builtin_packs_dir", lambda: builtin)
    monkeypatch.delenv("CHIPGRAPH_PACK_PATH", raising=False)
    repo = tmp_path / "repo"
    _write_pack(repo / ".chipgraph" / "packs" / "demo", _SIMPLE_RULE)
    init_git(repo)
    write_profile(repo, "project: demo\npacks: [demo]\n")

    with pytest.raises(AppError, match="duplicate pack name"):
        load_rules(AppContext.load(repo))
