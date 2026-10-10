"""A worker for the fan-out kill test: starts a fan-out whose branch hangs, then waits.

Usage: python _fanout_kill_worker.py <repo> <tmp_root> <marker>

The branch writes `<marker>` (its workspace path) and then sleeps, so the parent can
SIGKILL this process while the workspace exists.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from chipgraph.adapters.vcs.git import GitVcs
from chipgraph.core.engine.fanout import BranchContext, BranchOutcome, FanoutBranch, run_fanout


async def _main(repo: Path, tmp_root: Path, marker: Path) -> None:
    async def hang(ctx: BranchContext) -> BranchOutcome:
        (ctx.path / "rtl").mkdir(exist_ok=True)
        (ctx.path / "rtl/a.v").write_text("a\n")
        marker.write_text(str(ctx.path))
        await asyncio.sleep(600)
        return BranchOutcome(ok=True)

    async def quick(ctx: BranchContext) -> BranchOutcome:
        await asyncio.sleep(600)
        return BranchOutcome(ok=True)

    branches = [
        FanoutBranch(id="a", run=hang, write_set=("rtl/a.v",)),
        FanoutBranch(id="b", run=quick, write_set=("rtl/b.v",)),
    ]
    await run_fanout(branches, root=repo, vcs=GitVcs(), tmp_root=tmp_root, run_id="killed")


if __name__ == "__main__":
    asyncio.run(_main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
