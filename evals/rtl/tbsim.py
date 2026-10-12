"""A cocotb test run against the oracle and against each mutant of it (Verilator).

`run_tb(problem, test_text, work)` grades a testbench Author's test with the oracle: the
oracle and its mutants (`mutants.oracle_mutants`) are compiled once, behind a wrapper
module with the oracle's own name and ports that passes through the outputs of one of
them, chosen at run time (the file `cg_variant.txt` in the work directory: 0 the oracle,
k the k-th mutant). So the model is built once and the test runs once per variant, each
time through the `edalize` adapter (cocotb 2.1, Verilator) as `tb_static`'s `sim` does,
with cocotb's random seed fixed (0).

- soundness: the share of the test's cocotb tests that pass on the oracle (a correct
  test passes on correct RTL; a failing one is the testbench author's mistake);
- mutation score: the share of mutants killed, a mutant being killed when a test that
  passes on the oracle fails (or errors) on it. A test that fails on the oracle kills
  nothing.

Needs the `sim` extra (edalize, cocotb), Verilator, `make` and a C++ compiler.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mutants import Mutant, oracle_mutants
from .problems import Port, Problem

VARIANT_FILE = "cg_variant.txt"
RUN_TIMEOUT_S = 300


@dataclass(frozen=True)
class VariantRun:
    """One run of the test on one variant: the check status and each test's outcome."""

    variant: str
    status: str
    tests: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class TbResult:
    oracle: VariantRun
    mutants: tuple[VariantRun, ...] = ()
    error: str = ""

    @property
    def total_tests(self) -> int:
        return len(self.oracle.tests)

    @property
    def passing_on_oracle(self) -> tuple[str, ...]:
        return tuple(n for n, outcome in self.oracle.tests.items() if outcome == "pass")

    @property
    def soundness(self) -> float:
        """Tests that pass on the oracle over all tests (0 when none ran)."""
        if not self.oracle.tests:
            return 0.0
        return round(len(self.passing_on_oracle) / len(self.oracle.tests), 4)

    def killed(self, run: VariantRun) -> bool:
        good = set(self.passing_on_oracle)
        if not good:
            return False
        if run.status == "error" and not run.tests:
            return True  # the model did not finish (a hang, a crash) where the oracle passed
        return any(run.tests.get(name) != "pass" for name in good)

    @property
    def mutation_score(self) -> float:
        if not self.mutants:
            return 0.0
        return round(sum(self.killed(m) for m in self.mutants) / len(self.mutants), 4)

    def to_json(self) -> dict[str, Any]:
        return {
            "tests": self.total_tests,
            "passing_on_oracle": list(self.passing_on_oracle),
            "failing_on_oracle": [
                n for n, outcome in self.oracle.tests.items() if outcome != "pass"
            ],
            "oracle_status": self.oracle.status,
            "soundness": self.soundness,
            "mutants": len(self.mutants),
            "killed": [m.variant for m in self.mutants if self.killed(m)],
            "survived": [m.variant for m in self.mutants if not self.killed(m)],
            "mutation_score": self.mutation_score,
            "note": self.error or self.oracle.note,
        }


def _decl(port: Port, name: str, direction: str | None = None) -> str:
    width = f"[{port.width - 1}:0] " if port.width > 1 else ""
    head = f"{direction} " if direction else ""
    return f"{head}logic {width}{name}"


def wrapper(problem: Problem, variants: int, variant_file: Path) -> str:
    """The top the test sees: the oracle's name and ports, outputs from variant `sel`."""
    ports = ",\n".join(f"  {_decl(p, p.name, p.direction)}" for p in problem.interface)
    lines = [
        "`timescale 1ns/1ps",
        f"module {problem.module} (",
        ports,
        ");",
        "  int cg_sel = 0;",
        "  initial begin",
        "    int fd;",
        f'    fd = $fopen("{variant_file.as_posix()}", "r");',
        '    if (fd != 0) begin void\'($fscanf(fd, "%d", cg_sel)); $fclose(fd); end',
        "  end",
    ]
    for k in range(variants):
        lines += [f"  {_decl(p, f'{p.name}__v{k}')};" for p in problem.outputs]
        conns = ",\n".join(
            f"    .{p.name}({p.name if p.direction == 'input' else f'{p.name}__v{k}'})"
            for p in problem.interface
        )
        lines += [f"  cg_v{k}_{problem.module} u_v{k} (", conns, "  );"]
    for p in problem.outputs:
        choice = " : ".join(f"(cg_sel == {k}) ? {p.name}__v{k}" for k in range(1, variants))
        expr = f"{choice} : {p.name}__v0" if choice else f"{p.name}__v0"
        lines.append(f"  assign {p.name} = {expr};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run `coro` to completion, also from inside a running event loop (Inspect's scorer
    is async): then in a thread of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _tests(work: Path) -> dict[str, str]:
    from chipgraph.adapters.parser.cocotb import CocotbParser

    path = work / "results.xml"
    if not path.is_file():
        return {}
    outcome = CocotbParser().parse_results(path.read_text(encoding="utf-8"))
    return {t.name.rsplit(".", 1)[-1]: t.outcome for t in outcome.tests}


def run_tb(
    problem: Problem,
    test_text: str,
    work: Path,
    *,
    mutants: list[Mutant] | None = None,
    oracle: str | None = None,
) -> TbResult:
    """Run the test `test_text` on the oracle and on each mutant, in `work` (scratch)."""
    from chipgraph.adapters.runner.local import LocalRunner
    from chipgraph.adapters.tool.edalize import EdalizeTool
    from chipgraph.core.contracts import CheckSpec
    from chipgraph.core.plugin_api.types import ToolContext

    from .diffsim import rename_module
    from .fixtures import strip_header

    reference = (
        oracle if oracle is not None else strip_header(problem.fixture(problem.files.oracle))
    )
    found = oracle_mutants(problem) if mutants is None else mutants
    variants = [("oracle", reference), *((m.id, m.text) for m in found)]
    shutil.rmtree(work, ignore_errors=True)
    src = work / "src"  # the adapter's repo root; its work root must be outside it
    (src / "rtl").mkdir(parents=True)
    (src / "tb").mkdir()
    sim = work / "sim"
    sim.mkdir()
    test = src / "tb" / Path(problem.tb_output).name
    test.write_text(test_text, encoding="utf-8")
    files = []
    for k, (_, text) in enumerate(variants):
        path = src / "rtl" / f"v{k}.sv"
        path.write_text(
            rename_module(text, problem.module, f"cg_v{k}_{problem.module}"), encoding="utf-8"
        )
        files.append(path.relative_to(src).as_posix())
    select = sim / VARIANT_FILE
    top = src / "rtl" / "top.sv"
    top.write_text(wrapper(problem, len(variants), select), encoding="utf-8")
    files.append(top.relative_to(src).as_posix())
    spec = CheckSpec(
        id="tb_vs_oracle",
        capability="sim",
        adapter="edalize",
        args={
            "simulator": "verilator",
            "top": problem.module,
            "files": files,
            "test_module": test.relative_to(src).as_posix(),
            "work_root": str(sim),
            "timeout_s": RUN_TIMEOUT_S,
            "seed": 0,
            # The variants are the oracle's text: warnings never stop their build, and a
            # file without a `timescale` next to the wrapper's is not an error.
            "tool_options": {"verilator_options": ["-Wno-fatal", "-Wno-TIMESCALEMOD"]},
        },
    )
    ctx = ToolContext(repo_root=src, runner=LocalRunner(), params={})
    tool = EdalizeTool()
    runs: list[VariantRun] = []
    for k, (name, _) in enumerate(variants):
        select.write_text(f"{k}\n", encoding="utf-8")
        result = _run_sync(tool.run(spec, ctx))
        note = "; ".join(i.msg for i in result.issues[:3]) if result.status == "error" else ""
        runs.append(VariantRun(name, result.status, _tests(sim), note[:400]))
        if k == 0 and result.status == "error" and not runs[0].tests:
            return TbResult(runs[0], error=f"the test did not run on the oracle: {note[:400]}")
    return TbResult(runs[0], tuple(runs[1:]))


__all__ = ["TbResult", "VariantRun", "run_tb", "wrapper"]
