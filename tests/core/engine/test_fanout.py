"""M2-07: fan-out in separate workspaces, merged in id order (DESIGN 5.3, F2-F7)."""

from __future__ import annotations

import asyncio
import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fanout_helpers import git, init_repo, linked_worktrees, snapshot, tree_listing, writer

from chipgraph.adapters.vcs.git import GitVcs
from chipgraph.core.contracts import CheckResult, Issue
from chipgraph.core.engine.fanout import (
    BranchContext,
    BranchOutcome,
    FanoutBranch,
    FanoutError,
    FanoutResult,
    open_fanout,
    run_fanout,
)
from chipgraph.core.engine.scheduler import ExecContext

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

BASE_FILES = {"README.md": "hello\n", "rtl/top.v": "module top; endmodule\n"}


def _run(root: Path, branches: list[FanoutBranch], tmp: Path, **kw: Any) -> FanoutResult:
    kw.setdefault("tmp_root", tmp / "fanout-tmp")
    return asyncio.run(run_fanout(branches, root=root, vcs=GitVcs(), **kw))


def _two(delay_a: float = 0.0, delay_b: float = 0.0) -> list[FanoutBranch]:
    return [
        FanoutBranch(
            id="b",
            run=writer({"rtl/b.v": "module b; endmodule\n"}, delay=delay_b),
            write_set=("rtl/b.v",),
        ),
        FanoutBranch(
            id="a",
            run=writer({"rtl/a.v": "module a; endmodule\n"}, delay=delay_a),
            write_set=("rtl/a.v",),
        ),
    ]


def _passing(check_id: str = "join.lint") -> CheckResult:
    return CheckResult(check_id=check_id, status="pass", duration_s=0.0, idempotency_key="k")


# --- accept: two independent branches merge cleanly ------------------------------------


def test_two_independent_branches_merge_into_one_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = init_repo(root, BASE_FILES)
    result = _run(root, _two(), tmp_path, run_id="r1", name="blocks")

    assert result.ok, result.message
    assert result.base == base
    assert [b.id for b in result.branches] == ["a", "b"]  # ascending id order
    assert result.files == ("rtl/a.v", "rtl/b.v")
    assert result.branch == "chipgraph/r1/blocks"
    assert git(root, "rev-parse", f"{result.branch}^").strip() == base
    assert git(root, "rev-parse", f"{result.branch}^{{tree}}").strip() == result.tree
    listing = tree_listing(root, result.branch)
    assert {path: text for path, (_, text) in listing.items()} == {
        **BASE_FILES,
        "rtl/a.v": "module a; endmodule\n",
        "rtl/b.v": "module b; endmodule\n",
    }
    assert git(root, "rev-parse", "HEAD").strip() == base  # the user's HEAD did not move
    assert git(root, "branch", "--show-current").strip() == "main"
    assert linked_worktrees(root) == []
    assert not any((tmp_path / "fanout-tmp").iterdir())


def test_same_inputs_give_the_same_tree_hash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    first = _run(root, _two(delay_a=0.05), tmp_path, run_id="r1")
    second = _run(root, _two(delay_b=0.05), tmp_path, run_id="r2")
    assert first.ok and second.ok
    assert first.tree == second.tree
    assert first.files == second.files
    dumped = first.model_dump_json()
    assert str(tmp_path) not in dumped  # no machine-dependent paths in the result
    assert FanoutResult.model_validate_json(dumped) == first


def test_file_modes_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branch = FanoutBranch(
        id="a",
        run=writer({"scripts/run.sh": "#!/bin/sh\n"}, executable=("scripts/run.sh",)),
        write_set=("scripts/run.sh",),
    )
    result = _run(root, [branch], tmp_path)
    assert result.ok
    assert tree_listing(root, result.branch or "")["scripts/run.sh"][0] == "100755"
    assert result.branches[0].files[0].new_mode == "100755"


def test_an_ignored_write_set_file_is_still_merged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, {**BASE_FILES, ".gitignore": "build/\n"})
    branch = FanoutBranch(
        id="a", run=writer({"build/out.txt": "generated\n"}), write_set=("build/out.txt",)
    )
    result = _run(root, [branch], tmp_path)
    assert result.ok
    assert result.files == ("build/out.txt",)


# --- F2 ---------------------------------------------------------------------------------


def test_overlapping_write_sets_are_an_error_before_anything_starts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(id="x", run=writer({}), write_set=("rtl/a.v", "rtl/x.v")),
        FanoutBranch(id="y", run=writer({}), write_set=("rtl/y.v", "rtl/a.v")),
    ]
    with pytest.raises(FanoutError, match=r"'x' and 'y' both write 'rtl/a\.v'"):
        _run(root, branches, tmp_path)
    assert not (tmp_path / "fanout-tmp").exists()  # no workspace was made


def test_writing_a_shared_file_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [FanoutBranch(id="x", run=writer({}), write_set=("rtl/top.v",))]
    with pytest.raises(FanoutError, match=r"'x' writes 'rtl/top\.v', a shared file"):
        _run(root, branches, tmp_path, shared=("rtl/top.v",))


def test_a_branch_changing_a_file_outside_its_write_set_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(
            id="a",
            run=writer({"rtl/a.v": "a\n", "rtl/top.v": "hacked\n"}),
            write_set=("rtl/a.v",),
        ),
        FanoutBranch(id="b", run=writer({"rtl/b.v": "b\n"}), write_set=("rtl/b.v",)),
    ]
    result = _run(root, branches, tmp_path, run_id="r1")
    a = result.branch_result("a")
    assert a is not None and a.status == "rejected"
    assert a.outside_writes == ("rtl/top.v",)
    assert "rtl/top.v" in a.message
    assert result.status == "failed" and result.branch is None
    assert "chipgraph/r1/fanout" not in git(root, "branch", "--list")


# --- F3 conflicts -----------------------------------------------------------------------


def test_a_patch_on_a_different_base_file_is_a_conflict(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, {**BASE_FILES, "spec/regs.md": "v1\n"})
    (root / "spec/regs.md").write_text("user edit, not committed\n")
    branches = [
        # `a` rewrites the committed regs.md; `b` reads the user's newer copy.
        FanoutBranch(
            id="a", run=writer({"spec/regs.md": "v2 by a\n"}), write_set=("spec/regs.md",)
        ),
        FanoutBranch(
            id="b", run=writer({"rtl/b.v": "b\n"}), write_set=("rtl/b.v",), inputs=("spec/regs.md",)
        ),
    ]
    result = _run(root, branches, tmp_path)
    assert result.status == "failed"
    assert result.branch is None
    assert len(result.conflicts) == 1
    assert result.conflicts[0].branches == ("a",)
    assert "does not apply" in result.conflicts[0].message
    assert (root / "spec/regs.md").read_text() == "user edit, not committed\n"


# --- F4 assumptions ---------------------------------------------------------------------


def test_a_mismatch_of_assumptions_stops_and_asks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(
            id="rtl",
            run=writer({"rtl/a.v": "a\n"}, assumptions=("interface:uart_if.width=8",)),
            write_set=("rtl/a.v",),
        ),
        FanoutBranch(
            id="tb",
            run=writer({"tb/a_tb.v": "tb\n"}, assumptions=("interface:uart_if.width=16",)),
            write_set=("tb/a_tb.v",),
        ),
    ]
    result = _run(root, branches, tmp_path, run_id="r1")
    assert result.status == "needs_human"
    assert result.branch is None and result.commit is None
    (mismatch,) = result.mismatches
    assert mismatch.key == "interface:uart_if.width"
    assert [(s.branch, s.value) for s in mismatch.statements] == [("rtl", "8"), ("tb", "16")]
    (question,) = result.open_questions
    assert "'8'" in question and "'16'" in question
    assert "chipgraph/r1" not in git(root, "branch", "--list")


def test_equal_assumptions_pass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(
            id="rtl",
            run=writer({"rtl/a.v": "a\n"}, keys={"clock.freq": "50 MHz"}),
            write_set=("rtl/a.v",),
        ),
        FanoutBranch(
            id="tb",
            run=writer({"tb/a_tb.v": "tb\n"}, keys={"Clock.Freq": " 50  mhz. "}),
            write_set=("tb/a_tb.v",),
        ),
        FanoutBranch(
            id="doc",
            run=writer({"doc/a.md": "doc\n"}, keys={"only.here": "x"}),
            write_set=("doc/a.md",),
        ),
    ]
    result = _run(root, branches, tmp_path)
    assert result.ok, result.message
    assert result.mismatches == ()


# --- F5 join checks ---------------------------------------------------------------------


def test_a_failing_join_check_fails_the_fanout_and_creates_no_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    seen: list[set[str]] = []

    async def lint(integration: Path) -> CheckResult:
        seen.append({p.name for p in (integration / "rtl").iterdir()})
        return CheckResult(
            check_id="join.lint",
            status="fail",
            issues=(Issue(file="rtl/a.v", line=1, rule="lint.syntax", msg="bad"),),
            duration_s=0.0,
            idempotency_key="k",
        )

    result = _run(root, _two(), tmp_path, run_id="r1", join_checks=(lint,))
    assert seen == [{"top.v", "a.v", "b.v"}]  # it ran on the merged tree
    assert result.status == "failed"
    assert "join.lint" in result.message
    assert result.branch is None and result.commit is None
    assert result.join_checks[0].issues[0].rule == "lint.syntax"
    assert "chipgraph/r1" not in git(root, "branch", "--list")


def test_a_join_check_that_raises_is_an_error_result(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)

    async def broken(_: Path) -> CheckResult:
        raise RuntimeError("tool missing")

    result = _run(root, _two(), tmp_path, join_checks=(broken,))
    assert result.status == "failed"
    assert result.join_checks[0].status == "error"
    assert "tool missing" in result.join_checks[0].issues[0].msg


def test_passing_join_checks_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)

    async def lint(_: Path) -> CheckResult:
        return _passing()

    result = _run(root, _two(), tmp_path, join_checks=(lint,))
    assert result.ok
    assert [c.check_id for c in result.join_checks] == ["join.lint"]


def test_open_fanout_yields_the_integrated_tree_while_the_context_lives(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)

    async def main() -> Path:
        async with open_fanout(
            _two(), root=root, vcs=GitVcs(), tmp_root=tmp_path / "t", make_branch=False
        ) as session:
            assert session.result.ok
            assert session.result.branch is None and session.result.commit is not None
            assert session.integration is not None
            assert (session.integration / "rtl/a.v").read_text() == "module a; endmodule\n"
            return session.integration

    integration = asyncio.run(main())
    assert not integration.exists()
    assert linked_worktrees(root) == []


# --- parallelism, F7 re-run, timeouts, journal ------------------------------------------


def test_max_parallel_is_never_exceeded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    running = 0
    peak = 0

    def counted(i: int) -> Any:
        async def run(ctx: BranchContext) -> BranchOutcome:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            (ctx.path / f"out/{i}.txt").parent.mkdir(exist_ok=True)
            (ctx.path / f"out/{i}.txt").write_text(f"{i}\n")
            running -= 1
            return BranchOutcome(ok=True)

        return run

    branches = [
        FanoutBranch(id=f"b{i}", run=counted(i), write_set=(f"out/{i}.txt",)) for i in range(6)
    ]
    result = _run(root, branches, tmp_path, max_parallel=2)
    assert result.ok
    assert peak == 2
    assert len(result.files) == 6


def test_a_failed_branch_is_rerun_alone_and_the_others_are_kept(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    calls: dict[str, int] = {"a": 0, "b": 0}

    def tracked(branch_id: str, fail_first: bool) -> Any:
        inner_ok = writer({f"rtl/{branch_id}.v": f"{branch_id}\n"})
        inner_bad = writer({f"rtl/{branch_id}.v": "half\n"}, ok=False)

        async def run(ctx: BranchContext) -> BranchOutcome:
            calls[branch_id] += 1
            if fail_first and calls[branch_id] == 1:
                return await inner_bad(ctx)
            return await inner_ok(ctx)

        return run

    branches = [
        FanoutBranch(id="a", run=tracked("a", False), write_set=("rtl/a.v",)),
        FanoutBranch(id="b", run=tracked("b", True), write_set=("rtl/b.v",)),
    ]
    first = _run(root, branches, tmp_path, run_id="r1")
    assert first.status == "failed"
    assert [(b.id, b.status) for b in first.branches] == [("a", "done"), ("b", "failed")]
    assert first.branch is None

    second = _run(root, branches, tmp_path, run_id="r2", previous=first)
    assert second.ok, second.message
    assert calls == {"a": 1, "b": 2}  # only b ran again
    assert [(b.id, b.reused) for b in second.branches] == [("a", True), ("b", False)]
    listing = tree_listing(root, second.branch or "")
    assert listing["rtl/a.v"][1] == "a\n" and listing["rtl/b.v"][1] == "b\n"


def test_a_kept_result_is_not_reused_when_its_inputs_changed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, {**BASE_FILES, "spec/a.md": "v1\n"})
    calls = {"a": 0}

    async def run_a(ctx: BranchContext) -> BranchOutcome:
        calls["a"] += 1
        (ctx.path / "rtl/a.v").write_text((ctx.path / "spec/a.md").read_text())
        return BranchOutcome(ok=True)

    branches = [
        FanoutBranch(id="a", run=run_a, write_set=("rtl/a.v",), inputs=("spec/a.md",)),
        FanoutBranch(id="b", run=writer({"rtl/b.v": "b\n"}, ok=False), write_set=("rtl/b.v",)),
    ]
    first = _run(root, branches, tmp_path)
    (root / "spec/a.md").write_text("v2\n")
    _run(root, branches, tmp_path, previous=first)
    assert calls["a"] == 2


def test_a_branch_past_its_timeout_is_stopped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(id="slow", run=writer({"rtl/s.v": "s\n"}, delay=5), write_set=("rtl/s.v",)),
        FanoutBranch(id="fast", run=writer({"rtl/f.v": "f\n"}), write_set=("rtl/f.v",)),
    ]
    result = _run(root, branches, tmp_path, branch_timeout_s=0.2)
    slow = result.branch_result("slow")
    assert slow is not None and slow.status == "timeout"
    assert result.branch_result("fast").status == "done"  # type: ignore[union-attr]
    assert result.status == "failed"
    assert linked_worktrees(root) == []


def test_events_go_to_the_journal_through_the_exec_context(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = ExecContext(run_id="run", run_check=None, emit=lambda t, p: events.append((t, p)))
    result = _run(root, _two(), tmp_path, ctx=ctx)
    assert result.ok
    assert {t for t, _ in events} == {"tool_call"}
    names = [p["event"] for _, p in events]
    assert names[0] == "start" and names[-1] == "done"
    assert names.count("branch_start") == 2 and names.count("branch_done") == 2
    assert "merged" in names
    assert all(p["tool"] == "fanout" for _, p in events)


# --- seeding, dirty trees, detached HEAD, odd paths -------------------------------------


def test_seeding_copies_uncommitted_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    (root / "spec").mkdir()
    (root / "spec/new.md").write_text("untracked spec\n")  # never committed
    (root / "README.md").write_text("edited, not committed\n")

    async def derive(ctx: BranchContext) -> BranchOutcome:
        spec = (ctx.path / "spec/new.md").read_text()
        readme = (ctx.path / "README.md").read_text()
        (ctx.path / "rtl/a.v").write_text(f"// {spec.strip()} / {readme.strip()}\n")
        return BranchOutcome(ok=True)

    branch = FanoutBranch(id="a", run=derive, write_set=("rtl/a.v",), inputs=("spec", "README.md"))
    result = _run(root, [branch], tmp_path)
    assert result.ok, result.message
    assert result.files == ("rtl/a.v",)  # seeded inputs are not part of the branch
    listing = tree_listing(root, result.branch or "")
    assert listing["rtl/a.v"][1] == "// untracked spec / edited, not committed\n"
    assert listing["README.md"][1] == "hello\n"  # the commit is base + the branch
    assert "spec/new.md" not in listing
    assert (root / "spec/new.md").read_text() == "untracked spec\n"  # copied, not moved


def test_works_on_a_dirty_tree_with_a_detached_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = init_repo(root, BASE_FILES)
    (root / "later.txt").write_text("later\n")
    git(root, "add", "later.txt")
    git(root, "commit", "-q", "-m", "later")
    git(root, "checkout", "-q", "--detach", first)
    (root / "README.md").write_text("dirty\n")
    before = snapshot(root)

    result = _run(root, _two(), tmp_path, run_id="r1")
    assert result.ok
    assert result.base == first
    assert "later.txt" not in tree_listing(root, result.branch or "")
    assert snapshot(root) == before


def test_a_repo_under_a_path_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "my projects" / "chip repo"
    init_repo(root, BASE_FILES)
    result = _run(root, _two(), tmp_path / "temp dir")
    assert result.ok
    assert result.files == ("rtl/a.v", "rtl/b.v")


def test_the_default_run_directory_is_outside_the_working_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    seen: list[Path] = []

    async def where(ctx: BranchContext) -> BranchOutcome:
        seen.append(ctx.path)
        (ctx.path / "rtl/a.v").write_text("a\n")
        return BranchOutcome(ok=True)

    branch = FanoutBranch(id="a", run=where, write_set=("rtl/a.v",))
    result = asyncio.run(run_fanout([branch], root=root, vcs=GitVcs()))
    assert result.ok
    (path,) = seen
    assert not path.resolve().is_relative_to(root.resolve())
    assert path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert path.parent.name.startswith("chipgraph-fanout-")
    assert not path.parent.exists()


def test_a_tmp_root_inside_the_working_tree_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    with pytest.raises(FanoutError, match="inside the working tree"):
        _run(root, _two(), tmp_path, tmp_root=root / ".chipgraph" / "state" / "tmp")


@pytest.mark.parametrize(
    "bad", ["../escape.v", "/etc/passwd", ".git/config", "rtl/../x.v", "a//b", "./a", "sub/.git/x"]
)
def test_paths_are_validated(bad: str) -> None:
    with pytest.raises(FanoutError, match="invalid path"):
        FanoutBranch(id="a", run=writer({}), write_set=(bad,))
    with pytest.raises(FanoutError, match="invalid path"):
        FanoutBranch(id="a", run=writer({}), write_set=("ok.v",), inputs=(bad,))


def test_branch_ids_must_be_unique(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [
        FanoutBranch(id="a", run=writer({}), write_set=("x",)),
        FanoutBranch(id="a", run=writer({}), write_set=("y",)),
    ]
    with pytest.raises(FanoutError, match="used twice"):
        _run(root, branches, tmp_path)


# --- property: disjoint write sets always merge to the same tree ------------------------


@pytest.mark.parametrize("seed", range(4))
def test_random_disjoint_write_sets_merge_to_the_same_tree_in_any_order(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    pool = [f"d{d}/f{f}.txt" for d in range(3) for f in range(6)] + ["rtl/top.v"]
    rng.shuffle(pool)
    count = rng.randint(2, 6)
    sets = [pool[i::count][: rng.randint(1, 3)] for i in range(count)]
    contents = {path: f"{path} by {seed}\n" for paths in sets for path in paths}

    def branches(delays: list[float]) -> list[FanoutBranch]:
        return [
            FanoutBranch(
                id=f"br{i:02d}",
                run=writer({p: contents[p] for p in paths}, delay=delays[i]),
                write_set=tuple(paths),
            )
            for i, paths in enumerate(sets)
        ]

    trees = set()
    for run in range(2):
        delays = [rng.uniform(0, 0.05) for _ in sets]
        result = _run(
            root,
            branches(delays),
            tmp_path,
            run_id=f"s{seed}r{run}",
            max_parallel=rng.randint(1, 4),
        )
        assert result.ok, result.message
        listing = tree_listing(root, result.branch or "")
        assert {p: listing[p][1] for p in contents} == contents
        trees.add(result.tree)
    assert len(trees) == 1
