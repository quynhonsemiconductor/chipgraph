"""Turn one module's elaborated facts into clock-domain crossings.

Conservative, structural analysis (no timing, no value analysis), per DESIGN.md 4.8
layer 1:

1. Each `always_ff` has a clock *root* -- the module input clock its event samples. Every
   register (a variable it assigns) belongs to that root's domain.
2. A register's next-state expression reads other signals; each read is expanded through
   combinational logic (`assign`, `always_comb`) to the set of *source registers* it
   ultimately depends on.
3. A source register in domain A feeding a register in domain B != A is a crossing.
4. The crossing is *synchronised* when the path goes through a synchroniser cell: a sync
   instance's output is a fresh signal that carries no source domain, so expansion stops
   there (its source set is empty). A register reading a sync output is therefore never a
   crossing, while the same value taken raw or through plain combinational logic still is.
5. When the source is a module input with no known domain, or the path leaves the analysed
   level (deeper hierarchy), the result is `info` ("unknown"/"not analysed"), never an
   error.

One level of hierarchy is followed. A non-sync child instance with a resolvable clock
input is modelled as a set of *pseudo-registers*: each of its outputs is a flop in the
domain its clock port maps to (the parent clock root the port connects to), sampling the
child's data inputs. That makes a crossing wired between two sibling instances through the
parent visible, and lets the child's output feed a further crossing. A child with no
resolvable clock is treated as combinational passthrough; a sync child breaks the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from chipgraph.checks._cdc_elaborate import Loc, ModuleFacts, SubInstance


class Kind(Enum):
    """What a resolved crossing is: a real one, or one we could not decide."""

    UNSYNCHRONISED = "unsynchronised"
    UNKNOWN_DOMAIN = "unknown_domain"
    NOT_ANALYSED = "not_analysed"


@dataclass(frozen=True, slots=True)
class Crossing:
    """One resolved clock-domain crossing at a destination register."""

    kind: Kind
    dst_name: str
    dst_domain: str
    loc: Loc
    detail: str


@dataclass(frozen=True, slots=True)
class _Dst:
    """A crossing destination: a real register or a child instance's pseudo-register."""

    name: str
    domain: str | None
    loc: Loc
    reads: frozenset[str]


def analyse(facts: ModuleFacts) -> list[Crossing]:
    """Every crossing at a destination of `facts`, deterministically ordered.

    A destination in domain B whose fan-in reaches a source register in domain A != B is a
    crossing (unsynchronised), unless every A-domain path is broken by a synchroniser
    (which yields an empty source set for its output).
    """
    domains, dsts = _domains_and_destinations(facts)
    resolver = _Resolver(facts, domains)

    crossings: list[Crossing] = []
    for dst in sorted(dsts, key=lambda d: (d.loc.line or 0, d.name)):
        dst_domain = dst.domain
        if dst_domain is None:
            # The destination itself has no known clock: cannot judge crossings into it.
            continue
        sources = _SourceSet()
        for read in dst.reads:
            sources.merge(resolver.sources_of(read))

        bad_domains = {d for d in sources.domains if d is not None and d != dst_domain}
        if bad_domains:
            crossings.append(
                Crossing(
                    kind=Kind.UNSYNCHRONISED,
                    dst_name=dst.name,
                    dst_domain=dst_domain,
                    loc=dst.loc,
                    detail=(
                        f"{dst.name!r} in clock domain {dst_domain!r} samples a register in "
                        f"domain {', '.join(sorted(bad_domains))} without a synchroniser "
                        "between them"
                    ),
                )
            )
            continue
        # No hard crossing, but an unresolved source that *might* be another domain.
        if sources.unknown_input is not None:
            crossings.append(
                Crossing(
                    kind=Kind.UNKNOWN_DOMAIN,
                    dst_name=dst.name,
                    dst_domain=dst_domain,
                    loc=dst.loc,
                    detail=(
                        f"{dst.name!r} in clock domain {dst_domain!r} samples "
                        f"{sources.unknown_input!r}, an input with no known clock domain"
                    ),
                )
            )
        elif sources.unresolved is not None:
            crossings.append(
                Crossing(
                    kind=Kind.NOT_ANALYSED,
                    dst_name=dst.name,
                    dst_domain=dst_domain,
                    loc=dst.loc,
                    detail=(
                        f"{dst.name!r} in clock domain {dst_domain!r} reads "
                        f"{sources.unresolved!r}, which this check does not analyse deeper"
                    ),
                )
            )
    return crossings


def _domains_and_destinations(
    facts: ModuleFacts,
) -> tuple[dict[str, str | None], list[_Dst]]:
    """The domain of every register/pseudo-register, and the list of crossing destinations.

    A real register maps its hierarchical path to its clock root. A non-sync child with a
    resolvable clock also contributes: each output is a pseudo-register in the child's
    mapped domain, reading the child's data inputs -- so the child's output can be both a
    crossing destination and a further source.
    """
    domains: dict[str, str | None] = {}
    dsts: list[_Dst] = []
    for reg in facts.registers.values():
        domains[reg.path] = reg.clock_root
        dsts.append(_Dst(name=reg.name, domain=reg.clock_root, loc=reg.loc, reads=reg.reads))

    for sub in facts.sub_instances:
        if sub.is_sync:
            continue
        child_domain = _child_domain(sub)
        if child_domain is None:
            continue  # combinational passthrough; handled by the resolver, not a flop here
        data_reads = _child_data_reads(sub)
        for port, out_path in sorted(sub.outputs.items()):
            domains[out_path] = child_domain
            dsts.append(
                _Dst(
                    name=f"{sub.name}.{port}",
                    domain=child_domain,
                    loc=sub.loc,
                    reads=data_reads,
                )
            )
    return domains, dsts


def _child_domain(sub: SubInstance) -> str | None:
    """The parent clock root a child's single clock input connects to, or None.

    The connected parent expression is a path like `top.clk_b`; its domain root is the
    last segment (`clk_b`). More than one clock input, or an unresolvable connection, is
    left to the resolver as passthrough rather than guessed at.
    """
    roots = {parent for parent in sub.clock_inputs.values() if parent is not None}
    if len(roots) != 1:
        return None
    (parent,) = roots
    return parent.rsplit(".", 1)[-1]


def _child_data_reads(sub: SubInstance) -> frozenset[str]:
    """The parent paths a child's non-clock inputs read (its data inputs)."""
    reads: set[str] = set()
    for port, in_reads in sub.inputs.items():
        if port in sub.clock_inputs:
            continue
        reads |= in_reads
    return frozenset(reads)


@dataclass(slots=True)
class _SourceSet:
    """The domains a signal's fan-in reaches, plus any unresolved source it hit."""

    domains: set[str | None] = field(default_factory=set)
    unknown_input: str | None = None
    unresolved: str | None = None

    def merge(self, other: _SourceSet) -> None:
        self.domains |= other.domains
        self.unknown_input = self.unknown_input or other.unknown_input
        self.unresolved = self.unresolved or other.unresolved


class _Resolver:
    """Expands a read to the source-register domains it depends on, with memoisation."""

    def __init__(self, facts: ModuleFacts, domains: dict[str, str | None]) -> None:
        self._domains = domains
        self._comb = facts.comb
        self._cache: dict[str, _SourceSet] = {}
        self._sync_outputs: set[str] = set()
        self._sub_output_reads: dict[str, frozenset[str]] = {}
        for sub in facts.sub_instances:
            for out_path in sub.outputs.values():
                if sub.is_sync:
                    self._sync_outputs.add(out_path)
                elif out_path not in domains:
                    # A combinational (no-clock) child: its output passes its inputs
                    # through. A clocked child's output is already a domain source above.
                    reads: set[str] = set()
                    for in_reads in sub.inputs.values():
                        reads |= in_reads
                    self._sub_output_reads[out_path] = frozenset(reads)

    def sources_of(self, path: str) -> _SourceSet:
        return self._resolve(path, frozenset())

    def _resolve(self, path: str, stack: frozenset[str]) -> _SourceSet:
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        result = _SourceSet()
        if path in stack:
            return result  # break a combinational loop conservatively (do not cache)
        stack = stack | {path}

        if path in self._sync_outputs:
            # A synchroniser output is a fresh signal in the reader's domain: it carries
            # no source domain, so the source set is empty and the crossing is clean.
            self._cache[path] = result
            return result

        if path in self._domains:
            result.domains.add(self._domains[path])
            self._cache[path] = result
            return result

        if path in self._comb:
            for read in self._comb[path].reads:
                result.merge(self._resolve(read, stack))
            self._cache[path] = result
            return result

        sub_reads = self._sub_output_reads.get(path)
        if sub_reads is not None:
            for read in sub_reads:
                result.merge(self._resolve(read, stack))
            self._cache[path] = result
            return result

        # Neither register, comb node, nor child output. A plain `top.name` is a module
        # input with no known domain (info); a deeper path is something not analysed.
        if path.count(".") == 1:
            result.unknown_input = path
        else:
            result.unresolved = path
        self._cache[path] = result
        return result


__all__ = ["Crossing", "Kind", "analyse"]
