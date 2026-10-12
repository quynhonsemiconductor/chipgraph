"""The differential simulation: a module against the oracle, on the same random stimulus.

`differential(problem, candidates, work)` grades each candidate (its SystemVerilog text,
by name) against the problem's oracle. A candidate whose declared ports (read with
pyslang, header only) are not the interface's names, directions and widths fails at once
(`interface`): a port of the wrong width could otherwise be padded or cut and look right.
The others are compiled together with the oracle into one Verilator `--binary` harness:
every module is renamed (`cg_ref_<module>`, `cg_c<k>_<module>`), all of them get the same
inputs, and every output of every candidate is compared with the oracle's (`!==`) once
per cycle, after the inputs of the cycle settle and before the clock's rising edge. The
harness runs once per seed of the problem's stimulus (`stimulus.py`, from the interface
only) and prints, per candidate, the cycles compared, the cycles with a mismatch, and the
first mismatching cycle and port.

A candidate passes (`match`) when every output equals the oracle's on every cycle of
every seed. `match_fraction` (cycles) and `seed_fraction` (seeds) show near misses. When
the build of several candidates fails, each is built on its own, so one that does not
compile is `compile_error` and the others are still graded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import stimulus
from .problems import Port, Problem

DiffStatus = Literal["match", "mismatch", "interface", "compile_error", "error"]

HARNESS_TOP = "cg_diff"
VERILATOR_FLAGS = (
    "--binary",
    "--timing",
    "-Wno-fatal",
    "-Wno-lint",
    "-Wno-style",
    "--x-assign",
    "0",
    "--x-initial",
    "0",
)
_RESULT_RE = re.compile(
    r"^CG_DIFF c(?P<k>\d+) cycles=(?P<cycles>\d+) mismatched=(?P<bad>\d+) "
    r"first=(?P<first>-?\d+) port=(?P<port>\S+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SeedResult:
    seed: int
    cycles: int
    mismatched: int
    first_cycle: int | None = None
    first_port: str | None = None

    @property
    def matched(self) -> bool:
        return self.mismatched == 0


@dataclass(frozen=True)
class DiffResult:
    status: DiffStatus
    seeds: tuple[SeedResult, ...] = ()
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "match"

    @property
    def match_fraction(self) -> float:
        """Cycles whose every output matched, over all cycles of all seeds (0 if none ran)."""
        total = sum(s.cycles for s in self.seeds)
        return round(sum(s.cycles - s.mismatched for s in self.seeds) / total, 4) if total else 0.0

    @property
    def seed_fraction(self) -> float:
        """Seeds with no mismatch, over the seeds."""
        if not self.seeds:
            return 0.0
        return round(sum(s.matched for s in self.seeds) / len(self.seeds), 4)

    def to_json(self) -> dict[str, object]:
        first = next((s for s in self.seeds if not s.matched), None)
        return {
            "status": self.status,
            "passed": self.passed,
            "match_fraction": self.match_fraction,
            "seed_fraction": self.seed_fraction,
            "seeds": len(self.seeds),
            "cycles": sum(s.cycles for s in self.seeds),
            "first_mismatch": (
                {"seed": first.seed, "cycle": first.first_cycle, "port": first.first_port}
                if first is not None
                else None
            ),
            "note": self.note,
        }


@dataclass
class Toolchain:
    """The simulator: `verilator` on PATH (or a path) and a time limit per step."""

    verilator: str = "verilator"
    timeout_s: float = 600
    log: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return shutil.which(self.verilator) is not None and shutil.which("make") is not None


def rename_module(text: str, old: str, new: str) -> str:
    """`text` with the declaration (and `endmodule : old` label) of `old` renamed `new`."""
    out = re.sub(rf"\bmodule(\s+){re.escape(old)}\b", rf"module\g<1>{new}", text, count=1)
    return re.sub(rf"\bendmodule(\s*:\s*){re.escape(old)}\b", rf"endmodule\g<1>{new}", out)


def interface_problems(problem: Problem, text: str) -> list[str]:
    """How the module declared in `text` differs from the problem's interface ([] if not)."""
    from chipgraph.packs.dv.declaration import declaration_in_text

    try:
        decl = declaration_in_text(text, problem.module, name=f"{problem.module}.sv")
    except Exception as exc:  # pyslang raises on pathological input only
        return [f"cannot read the declaration: {exc}"]
    if decl is None:
        return [f"no module {problem.module} is declared"]
    declared = {p.name: p for p in decl.ports}
    out: list[str] = []
    for port in problem.interface:
        got = declared.get(port.name)
        if got is None:
            out.append(f"port {port.name} is missing")
            continue
        if got.direction != port.direction:
            out.append(f"port {port.name} is {got.direction}, not {port.direction}")
        if got.width != port.width:
            out.append(f"port {port.name} is {got.width} bits wide, not {port.width}")
    extra = sorted(set(declared) - {p.name for p in problem.interface})
    if extra:
        out.append(f"ports not in the interface: {', '.join(extra)}")
    return out


def _decl(port: Port, name: str) -> str:
    width = f"[{port.width - 1}:0] " if port.width > 1 else ""
    return f"  logic {width}{name};"


def harness(problem: Problem, count: int, cycles: int) -> str:
    """The SystemVerilog harness for the oracle and `count` candidates."""
    ports = problem.interface
    outputs = problem.outputs
    packed = stimulus.packed_ports(problem)
    width = stimulus.packed_width(problem)
    names = ["ref", *(f"c{k}" for k in range(count))]
    lines = [
        "`timescale 1ns/1ps",
        f"module {HARNESS_TOP};",
        f"  localparam int W = {width};",
        f"  localparam int N = {cycles};",
        f"  localparam int K = {count};",
        "  logic [W-1:0] stim [0:N-1];",
    ]
    lines += [_decl(p, p.name) for p in ports if p.direction == "input"]
    for name in names:
        lines += [_decl(p, f"{p.name}__{name}") for p in outputs]
    lines += ["  int mism [K];", "  int first [K];", "  string firstp [K];"]
    for name in names:
        conns = ",\n".join(
            f"    .{p.name}({p.name if p.direction == 'input' else f'{p.name}__{name}'})"
            for p in ports
        )
        lines += [f"  cg_{name}_{problem.module} u_{name} (", conns, "  );"]
    lines += [
        "  initial begin",
        "    string path;",
        '    if (!$value$plusargs("stim=%s", path)) $fatal(1, "cg_diff: no +stim=<file>");',
        "    $readmemh(path, stim);",
        f"    {problem.clock} = 1'b0;",
        '    for (int k = 0; k < K; k++) begin mism[k] = 0; first[k] = -1; firstp[k] = "-"; end',
        "    for (int c = 0; c < N; c++) begin",
        f"      {{{', '.join(p.name for p in packed)}}} = stim[c];",
        "      #4;",
    ]
    for k in range(count):
        lines.append("      begin")
        lines.append("        bit bad;")
        lines.append("        bad = 1'b0;")
        for p in outputs:
            lines += [
                f"        if ({p.name}__c{k} !== {p.name}__ref) begin",
                "          bad = 1'b1;",
                f"          if (first[{k}] < 0) begin",
                f'            first[{k}] = c; firstp[{k}] = "{p.name}";',
                "          end",
                "        end",
            ]
        lines += [f"        if (bad) mism[{k}]++;", "      end"]
    lines += [
        f"      #1 {problem.clock} = 1'b1;",
        f"      #5 {problem.clock} = 1'b0;",
        "    end",
        "    for (int k = 0; k < K; k++)",
        '      $display("CG_DIFF c%0d cycles=%0d mismatched=%0d first=%0d port=%s",',
        "               k, N, mism[k], first[k], firstp[k]);",
        "    $finish;",
        "  end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def _run(cmd: Sequence[str], cwd: Path, tools: Toolchain) -> subprocess.CompletedProcess[str]:
    tools.log.append(" ".join(cmd))
    return subprocess.run(
        list(cmd), cwd=cwd, capture_output=True, text=True, timeout=tools.timeout_s, check=False
    )


def _build(
    problem: Problem, oracle: str, sources: Sequence[str], work: Path, tools: Toolchain
) -> tuple[Path | None, str]:
    """Write and compile the harness; (the binary, or None, and the build log)."""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    files = [work / "harness.sv", work / "ref.sv"]
    files[0].write_text(harness(problem, len(sources), problem.stimulus.cycles), encoding="utf-8")
    files[1].write_text(
        rename_module(oracle, problem.module, f"cg_ref_{problem.module}"), encoding="utf-8"
    )
    for k, text in enumerate(sources):
        path = work / f"c{k}.sv"
        path.write_text(
            rename_module(text, problem.module, f"cg_c{k}_{problem.module}"), encoding="utf-8"
        )
        files.append(path)
    cmd = [
        tools.verilator,
        *VERILATOR_FLAGS,
        "--top-module",
        HARNESS_TOP,
        "-Mdir",
        "obj",
        "-o",
        HARNESS_TOP,
        *(f.name for f in files),
    ]
    try:
        proc = _run(cmd, work, tools)
    except subprocess.TimeoutExpired:
        return None, "the Verilator build timed out"
    log = proc.stdout + proc.stderr
    binary = work / "obj" / HARNESS_TOP
    return (binary if proc.returncode == 0 and binary.is_file() else None), log


def _simulate(
    problem: Problem, binary: Path, count: int, work: Path, tools: Toolchain
) -> list[list[SeedResult]] | str:
    """Per candidate, a `SeedResult` per seed; or the reason the harness failed."""
    out: list[list[SeedResult]] = [[] for _ in range(count)]
    for seed in problem.stimulus.seeds:
        stim = work / f"stim-{seed}.hex"
        stim.write_text(stimulus.pack(problem, stimulus.generate(problem, seed)), encoding="utf-8")
        try:
            proc = _run([str(binary), f"+stim={stim.name}"], work, tools)
        except subprocess.TimeoutExpired:
            return f"the simulation of seed {seed} timed out"
        found = {int(m["k"]): m for m in _RESULT_RE.finditer(proc.stdout)}
        if proc.returncode != 0 or set(found) != set(range(count)):
            tail = (proc.stdout + proc.stderr)[-800:]
            return f"the harness failed on seed {seed} (exit {proc.returncode}): {tail}"
        for k, match in found.items():
            first = int(match["first"])
            out[k].append(
                SeedResult(
                    seed=seed,
                    cycles=int(match["cycles"]),
                    mismatched=int(match["bad"]),
                    first_cycle=first if first >= 0 else None,
                    first_port=match["port"] if first >= 0 else None,
                )
            )
    return out


def _tail(log: str) -> str:
    errors = [line for line in log.splitlines() if "%Error" in line]
    return "; ".join(errors[:5]) or log[-600:]


def differential(
    problem: Problem,
    candidates: Mapping[str, str],
    work: Path,
    *,
    oracle: str | None = None,
    tools: Toolchain | None = None,
) -> dict[str, DiffResult]:
    """Each candidate (name -> SystemVerilog text) against the oracle (default: the
    problem's oracle fixture), in `work` (a scratch directory)."""
    from .fixtures import strip_header

    tools = tools or Toolchain()
    reference = (
        oracle if oracle is not None else strip_header(problem.fixture(problem.files.oracle))
    )
    results: dict[str, DiffResult] = {}
    graded: list[str] = []
    for name, text in candidates.items():
        wrong = interface_problems(problem, text)
        if wrong:
            results[name] = DiffResult("interface", note="; ".join(wrong))
        else:
            graded.append(name)
    if not graded:
        return results

    def run_group(group: list[str], where: Path) -> bool:
        binary, log = _build(problem, reference, [candidates[n] for n in group], where, tools)
        if binary is None:
            for name in group:
                results[name] = DiffResult("compile_error", note=_tail(log))
            return False
        run = _simulate(problem, binary, len(group), where, tools)
        if isinstance(run, str):
            for name in group:
                results[name] = DiffResult("error", note=run)
            return True
        for name, seeds in zip(group, run, strict=True):
            ok = all(s.matched for s in seeds)
            results[name] = DiffResult("match" if ok else "mismatch", tuple(seeds))
        return True

    if not run_group(graded, work) and len(graded) > 1:
        for index, name in enumerate(graded):
            run_group([name], work.parent / f"{work.name}-{index}")
    return {name: results[name] for name in candidates}


__all__ = [
    "DiffResult",
    "DiffStatus",
    "SeedResult",
    "Toolchain",
    "differential",
    "harness",
    "interface_problems",
    "rename_module",
]
