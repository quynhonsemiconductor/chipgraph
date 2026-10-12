"""The random stimulus of the differential simulation, from the interface only.

`generate(problem, seed)` reads nothing of a module but its port list, its clock and its
reset (the problem's `interface`, `clock`, `reset` and `stimulus`): every cycle, a value
for every input but the clock. Reset is held active for `reset_cycles` at the start, and
later pulsed for one or two cycles with probability `reset_rate` per cycle; every other
input gets uniform random bits each cycle (`random.Random(seed).getrandbits`, the same
values on every platform and Python version). The same generator serves every problem.

`pack(problem, vectors)` writes the cycles as one hex word per line, the inputs
concatenated MSB-first in interface order (`$readmemh` in the harness unpacks them in the
same order).
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from .problems import Port, Problem

Vector = dict[str, int]


def _reset_value(problem: Problem, active: bool) -> int:
    return int(active != problem.reset.active_low)


def generate(problem: Problem, seed: int, cycles: int | None = None) -> list[Vector]:
    """`cycles` (default: the problem's) input vectors for `seed`, deterministic."""
    settings = problem.stimulus
    total = settings.cycles if cycles is None else cycles
    rng = random.Random(seed)
    threshold = round(settings.reset_rate * (1 << 16))
    out: list[Vector] = []
    pulse = 0
    for cycle in range(total):
        if cycle < settings.reset_cycles:
            active = True
        elif pulse > 0:
            active, pulse = True, pulse - 1
        else:
            active = rng.getrandbits(16) < threshold
            if active:
                pulse = rng.getrandbits(1)  # one or two cycles
        vector: Vector = {}
        for port in problem.inputs:
            if port.name == problem.reset.name:
                vector[port.name] = _reset_value(problem, active)
            else:
                vector[port.name] = rng.getrandbits(port.width)
        out.append(vector)
    return out


def packed_ports(problem: Problem) -> tuple[Port, ...]:
    """The inputs in packing order (MSB first)."""
    return problem.inputs


def packed_width(problem: Problem) -> int:
    return sum(p.width for p in packed_ports(problem))


def pack(problem: Problem, vectors: Sequence[Vector]) -> str:
    """The vectors as `$readmemh` text: one hex word per cycle."""
    ports = packed_ports(problem)
    digits = (packed_width(problem) + 3) // 4
    lines = []
    for vector in vectors:
        word = 0
        for port in ports:
            word = (word << port.width) | (vector[port.name] & ((1 << port.width) - 1))
        lines.append(f"{word:0{digits}x}")
    return "\n".join(lines) + "\n"


__all__ = ["Vector", "generate", "pack", "packed_ports", "packed_width"]
