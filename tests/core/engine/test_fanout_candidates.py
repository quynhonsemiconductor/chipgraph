"""M2-08: `run_candidates`, the fan-out for N candidates of one piece of work."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
from fanout_helpers import git, init_repo, linked_worktrees, snapshot, writer

from chipgraph.adapters.vcs.git import GitVcs
from chipgraph.core.contracts import CheckResult
from chipgraph.core.engine.fanout import (
    BranchResult,
    FanoutBranch,
    FanoutError,
    run_candidates,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OUT = "rtl/a.v"


def _candidates(ok: Mapping[str, bool] | None = None) -> list[FanoutBranch]:
    ok = ok or {}
    return [
        FanoutBranch(
            id=cid,
            run=writer({OUT: f"{cid}\n"}, ok=ok.get(cid, True)),
            write_set=(OUT,),
            inputs=("spec.md",),
        )
        for cid in ("a", "b", "c")
    ]


def _pick(cid: str | None, seen: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    async def choose(
        results: tuple[BranchResult, ...], workspaces: Mapping[str, Path]
    ) -> str | None:
        assert [r.id for r in results] == ["a", "b", "c"]
        for rid, path in workspaces.items():
            if seen is not None:
                seen[rid] = (path / OUT).read_text()
        return cid

    return choose


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    init_repo(root, {"spec.md": "spec\n", OUT: "old\n"})
    return root


def _clean(root: Path, tmp_root: Path) -> None:
    assert linked_worktrees(root) == []
    assert not tmp_root.exists() or not any(tmp_root.iterdir())


def test_only_the_chosen_candidate_is_merged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = snapshot(root)
    seen: dict[str, str] = {}
    result = asyncio.run(
        run_candidates(
            _candidates(),
            choose=_pick("b", seen),
            root=root,
            vcs=GitVcs(),
            tmp_root=tmp_path / "t",
            run_id="r1",
        )
    )
    assert result.ok, result.message
    assert (result.chosen, result.files, result.branch) == ("b", (OUT,), "chipgraph/r1/candidates")
    assert git(root, "show", f"{result.branch}:{OUT}") == "b\n"
    # Every workspace still existed for the chooser, with its own content.
    assert seen == {"a": "a\n", "b": "b\n", "c": "c\n"}
    assert snapshot(root) == before
    _clean(root, tmp_path / "t")


def test_choosing_none_merges_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = asyncio.run(
        run_candidates(
            _candidates(), choose=_pick(None), root=root, vcs=GitVcs(), tmp_root=tmp_path / "t"
        )
    )
    assert result.status == "failed" and result.branch is None and result.chosen is None
    assert git(root, "branch", "--list", "chipgraph/*") == ""
    _clean(root, tmp_path / "t")


def test_a_chosen_candidate_that_failed_is_not_merged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = asyncio.run(
        run_candidates(
            _candidates({"a": False}),
            choose=_pick("a"),
            root=root,
            vcs=GitVcs(),
            tmp_root=tmp_path / "t",
        )
    )
    assert result.status == "failed" and "did not finish" in result.message
    assert result.branch is None
    _clean(root, tmp_path / "t")


def test_a_chooser_naming_no_candidate_is_an_error_and_cleans_up(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(FanoutError, match="not a candidate"):
        asyncio.run(
            run_candidates(
                _candidates(), choose=_pick("z"), root=root, vcs=GitVcs(), tmp_root=tmp_path / "t"
            )
        )
    _clean(root, tmp_path / "t")


def test_join_checks_still_gate_the_result_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def failing(path: Path) -> CheckResult:
        return CheckResult(check_id="join.lint", status="fail", duration_s=0.0, idempotency_key="k")

    result = asyncio.run(
        run_candidates(
            _candidates(),
            choose=_pick("c"),
            root=root,
            vcs=GitVcs(),
            tmp_root=tmp_path / "t",
            join_checks=(failing,),
        )
    )
    assert result.status == "failed" and "F5" in result.message and result.branch is None
    _clean(root, tmp_path / "t")


def test_candidates_must_be_the_same_work(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    mixed = [
        FanoutBranch(id="a", run=writer({OUT: "a\n"}), write_set=(OUT,)),
        FanoutBranch(id="b", run=writer({"rtl/b.v": "b\n"}), write_set=("rtl/b.v",)),
    ]
    with pytest.raises(FanoutError, match="differ in write set or inputs"):
        asyncio.run(
            run_candidates(
                mixed, choose=_pick("a"), root=root, vcs=GitVcs(), tmp_root=tmp_path / "t"
            )
        )
    with pytest.raises(FanoutError, match="shared file"):
        asyncio.run(
            run_candidates(
                _candidates(),
                choose=_pick("a"),
                root=root,
                vcs=GitVcs(),
                shared=(OUT,),
                tmp_root=tmp_path / "t",
            )
        )
    with pytest.raises(FanoutError, match="at least one"):
        asyncio.run(
            run_candidates([], choose=_pick("a"), root=root, vcs=GitVcs(), tmp_root=tmp_path / "t")
        )
