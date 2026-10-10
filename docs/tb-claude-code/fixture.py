"""The M2-06 acceptance fixture: a tinysoc copy whose `dv/tb_module` tasks a testbench
Author does without ever seeing the RTL.

`make_tb_project(dest)` copies `examples/tinysoc` to `dest` (a new git repo; the example
itself is not changed), turns on the built-in pack `dv` (rule `dv/tb_module`, skill
`dv/cocotb`) and configures its check:

    adapters:
      tb_static: { use: tb_static, top: "tiny_{block}" }

With `sim=True`, `tb_static` also runs the written test on the RTL once its static
rules pass (`adapters.tb_static.sim`, the `edalize` adapter on Verilator): the Author
then gets the simulation's failures as feedback, filtered of anything from the RTL.
With `ingest=True` (the default) the Design Model is built (`chipgraph ingest`), so the
interface comes from the spec's ports; without it, from the module declarations.
`tiers` (e.g. `{"medium": "haiku", "large": "sonnet"}`) maps the role's model tiers in
the copy's profile.

The RTL files get one comment line each, `// chipgraph-tb-fingerprint: <block>`, after
their header comment: a line found in no other file, which `report.py` looks for in
everything the subagent saw (with every other long RTL line).

Command line, used by `run.sh`::

    python docs/tb-claude-code/fixture.py DEST [--medium-model M] [--large-model M]
        [--sim] [--no-ingest]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "tinysoc"

RULE = "dv/tb_module"
BLOCKS = ("gpio", "timer")
TASKS = {block: f"{RULE}[block={block}]" for block in BLOCKS}
OUTPUTS = {block: f"dv/{block}/test_{block}.py" for block in BLOCKS}
TOPS = {block: f"tiny_{block}" for block in BLOCKS}
FINGERPRINT = "chipgraph-tb-fingerprint"
"""The comment planted in each RTL body (see the module docstring)."""

SIM = {
    "use": "edalize",
    "simulator": "verilator",
    "top": "tiny_{block}",
    "filelist": "filelists/{block}.f",
    "test_module": "dv/{block}/test_{block}.py",
    "timeout_s": 600,
}
"""The `tb_static.sim` config: the written test, run on the block's RTL."""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def plant_fingerprints(root: Path) -> None:
    """Add a fingerprint comment inside each RTL module's body (after `);`)."""
    for path in sorted((root / "rtl").glob("*.sv")):
        text = path.read_text(encoding="utf-8")
        head, sep, body = text.partition(");\n")
        if not sep:
            continue
        line = f"  // {FINGERPRINT}: {path.stem} body\n"
        path.write_text(head + sep + line + body, encoding="utf-8")


def make_tb_project(
    dest: Path,
    *,
    tiers: dict[str, str] | None = None,
    sim: bool = False,
    ingest: bool = True,
    blocks: tuple[str, ...] | None = None,
) -> Path:
    """Copy tinysoc to `dest`, enable `dv`, configure `tb_static`, commit (and ingest)."""
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    plant_fingerprints(dest)
    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = [*profile.get("packs", []), "dv"]
    adapters = profile.setdefault("adapters", {})
    tb_static: dict[str, object] = {"use": "tb_static", "top": "tiny_{block}"}
    if sim:
        tb_static["sim"] = dict(SIM)
    adapters["tb_static"] = tb_static
    if blocks is not None:
        profile["blocks"] = {name: profile["blocks"].get(name) or {} for name in blocks}
    if tiers:
        profile.setdefault("models", {})["tiers"] = dict(tiers)
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m2-06@example.invalid")
    _git(dest, "config", "user.name", "m2-06")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with the dv pack")
    if ingest:
        from chipgraph.app.context import AppContext
        from chipgraph.app.ingest import run_ingest

        run_ingest(AppContext.load(dest))
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    parser.add_argument("--sim", action="store_true", help="tb_static also runs the test")
    parser.add_argument("--no-ingest", action="store_true", help="do not build the model")
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_tb_project(
        args.dest,
        tiers=tiers or None,
        sim=args.sim,
        ingest=not args.no_ingest,
        blocks=BLOCKS,
    )
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
