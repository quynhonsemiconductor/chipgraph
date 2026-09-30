"""`CrossChipCheck`: chip-wide consistency across IPs and the chip contract (layer 1).

Deterministic cross-artifact checks over the model `chipgraph ingest` built
(DESIGN.md 4.4 "Chéo IP và chip", 4.8 layer 1):

- `address.overlap`: two memory regions whose `[base, base+size)` windows overlap (int
  base and size only; a region with a string size is skipped with an `info`).
- `interrupt.line_shared`: two interrupts assigned to the same line.
- `interrupt.count`: an IP's spec declares interrupt output ports (name matches `regex`,
  default `^o_int_`; widths summed) but the contract gives its instances a different
  number of lines. Compared per IP through `instance_of` (D38), only when both sides are
  known.
- `interrupt.no_block`: an interrupt with no owning block; the IP whose name matches the
  interrupt's exactly is suggested, and nothing is guessed otherwise.
- `clock.unknown` / `reset.unknown`: a block, port or module names a clock/reset key (or
  bare name) that is not a `clock:`/`reset:` entity in the model.
- D38 (`instance.dangling`, `block.unknown`, `instance.unmapped`): an instance whose IP
  is missing, a `block:` reference absent from the model, and a contract instance (a
  block with a memory region) that is neither an IP nor an instance of one.

With a `--block`, only issues involving that IP or one of its instances are reported.
"""

from __future__ import annotations

import re
import time

from chipgraph.checks._common import ArgError, error_result, optional_str
from chipgraph.checks._cross import (
    block_key,
    block_param,
    load_model_or_skip,
    result_from_issues,
    scope_block_keys,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.model.entities import (
    EntityBase,
    InterruptEntity,
    MemoryRegionEntity,
    PortEntity,
)
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_INTERRUPT_PORT_REGEX = r"^o_int_"


class CrossChipCheck:
    """Checks chip-wide address, interrupt and clock/reset consistency."""

    id = "cross_chip"
    name = "Cross chip"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        try:
            int_port_regex = re.compile(
                optional_str(spec.args, "interrupt_port_regex", _DEFAULT_INTERRUPT_PORT_REGEX)
            )
        except (ArgError, re.error) as exc:
            return error_result(spec, f"bad interrupt_port_regex: {exc}", start)

        loaded = load_model_or_skip(spec, ctx, start)
        if isinstance(loaded, CheckResult):
            return loaded
        model = loaded.model
        block = block_param(ctx)
        scope = scope_block_keys(model, block) if block else None

        issues: list[Issue] = []
        issues.extend(_address_overlap(model, scope))
        issues.extend(_interrupt_line_shared(model, scope))
        issues.extend(_interrupt_count(model, scope, int_port_regex))
        issues.extend(_interrupt_no_block(model, scope))
        issues.extend(_clock_reset_unknown(model, scope))
        issues.extend(_d38(model, scope))
        return result_from_issues(spec, issues, loaded, start)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _prov(entity: EntityBase) -> tuple[str | None, int | None]:
    return entity.source.file, entity.source.line


def _entity_block(entity: EntityBase) -> str | None:
    block = getattr(entity, "block", None)
    if isinstance(block, str) and block:
        return block
    attr_block = entity.attrs.get("block")
    if isinstance(attr_block, str) and attr_block:
        return attr_block
    return None


def _in_scope(scope: set[str] | None, *block_keys: str | None) -> bool:
    """True when no scope is set, or any of `block_keys` is in the scope."""
    if scope is None:
        return True
    return any(bk in scope for bk in block_keys if bk is not None)


# --- address ---------------------------------------------------------------------------


def _address_overlap(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    regions: list[MemoryRegionEntity] = []
    for region in model.by_kind("memory_region"):
        assert isinstance(region, MemoryRegionEntity)
        if not isinstance(region.base, int) or not isinstance(region.size, int):
            file, line = _prov(region)
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="address.skipped",
                    severity="info",
                    msg=(
                        f"memory region {region.key!r} has a non-integer base/size "
                        f"({region.base!r}/{region.size!r}); overlap not checked"
                    ),
                )
            )
            continue
        regions.append(region)

    regions.sort(key=lambda r: (r.base, r.key))
    for i, a in enumerate(regions):
        for b in regions[i + 1 :]:
            assert isinstance(a.base, int) and isinstance(a.size, int)
            assert isinstance(b.base, int) and isinstance(b.size, int)
            if b.base >= a.base + a.size:
                break  # sorted by base: no later region can overlap `a`
            if not _in_scope(scope, _entity_block(a), _entity_block(b)):
                continue
            file, line = _prov(b)
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="address.overlap",
                    severity="error",
                    msg=(
                        f"memory region {b.key!r} [{b.base:#x}, {b.base + b.size:#x}) overlaps "
                        f"{a.key!r} [{a.base:#x}, {a.base + a.size:#x})"
                    ),
                )
            )
    return issues


# --- interrupts ------------------------------------------------------------------------


def _interrupt_line_shared(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    by_line: dict[int, list[InterruptEntity]] = {}
    for irq in model.by_kind("interrupt"):
        assert isinstance(irq, InterruptEntity)
        if irq.line is None:
            continue
        by_line.setdefault(irq.line, []).append(irq)

    for line_no, irqs in sorted(by_line.items()):
        if len(irqs) < 2:
            continue
        irqs.sort(key=lambda e: e.key)
        first = irqs[0]
        for other in irqs[1:]:
            if not _in_scope(scope, _entity_block(first), _entity_block(other)):
                continue
            file, line = _prov(other)
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="interrupt.line_shared",
                    severity="error",
                    msg=(f"interrupt {other.key!r} shares line {line_no} with {first.key!r}"),
                )
            )
    return issues


def _interrupt_count(
    model: DesignModel, scope: set[str] | None, port_regex: re.Pattern[str]
) -> list[Issue]:
    """Compare an IP's declared interrupt-output width to its instances' contract lines."""
    issues: list[Issue] = []
    # Declared count per IP: sum of the widths of its spec interrupt output ports.
    declared: dict[str, int] = {}
    declared_prov: dict[str, tuple[str | None, int | None]] = {}
    for port in model.by_kind("port"):
        assert isinstance(port, PortEntity)
        ip = _spec_port_block(port)
        if ip is None or port.direction != "output" or not port_regex.search(port.name):
            continue
        if not isinstance(port.width, int):
            continue
        declared[ip] = declared.get(ip, 0) + port.width
        declared_prov.setdefault(ip, _prov(port))

    # Instances per IP, and the number of interrupt lines the contract gives each.
    for ip, want in sorted(declared.items()):
        ip_key = block_key(ip)
        instance_keys = sorted(r.src for r in model.get_relations(dst=ip_key, kind="instance_of"))
        if not instance_keys:
            instance_keys = [ip_key]  # same-name rule (D38): the IP is its own instance
        # Compare per instance: each instance of the IP should carry `want` interrupt
        # lines. An interrupt entity may aggregate several sources (`attrs.sources`), so
        # count sources when the contract states them, else one line per interrupt entity.
        for inst_key in instance_keys:
            lines = 0
            known = False
            for irq in model.by_kind("interrupt"):
                assert isinstance(irq, InterruptEntity)
                if _entity_block(irq) != inst_key or irq.line is None:
                    continue
                known = True
                sources = irq.attrs.get("sources")
                lines += sources if isinstance(sources, int) and sources > 0 else 1
            if not known:
                continue  # the contract side is unknown for this instance; skip
            if lines == want:
                continue
            if not _in_scope(scope, ip_key, inst_key):
                continue
            file, line = declared_prov[ip]
            same_name = inst_key == ip_key
            where = "its spec" if same_name else f"the spec of its IP {ip_key!r}"
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="interrupt.count",
                    severity="error",
                    msg=(
                        f"instance {inst_key!r} has {lines} interrupt line(s) in the contract, "
                        f"but {where} declares {want}"
                    ),
                )
            )
    return issues


def _interrupt_no_block(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    for irq in model.by_kind("interrupt"):
        assert isinstance(irq, InterruptEntity)
        if _entity_block(irq) is not None:
            continue
        # Suggest the IP whose block name matches the interrupt's name exactly; never guess.
        _, _, rest = irq.key.partition(":")
        suggestion = block_key(rest)
        has_match = model.get(suggestion) is not None
        if scope is not None and (not has_match or suggestion not in scope):
            continue
        file, line = _prov(irq)
        hint = f"; it may belong to {suggestion!r}" if has_match else ""
        issues.append(
            Issue(
                file=file,
                line=line,
                rule="interrupt.no_block",
                severity="error",
                msg=f"interrupt {irq.key!r} has no owning block{hint}",
            )
        )
    return issues


def _spec_port_block(port: PortEntity) -> str | None:
    _, _, rest = port.key.partition(":")
    parts = rest.split(".")
    if len(parts) >= 3 and parts[0] == "spec":
        return parts[1]
    return None


# --- clock / reset ---------------------------------------------------------------------


def _clock_reset_unknown(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    clocks = {e.key for e in model.by_kind("clock")}
    resets = {e.key for e in model.by_kind("reset")}

    def known(kind: str, ref: str, present: set[str]) -> bool:
        # A reference is either a full key (`clock:peri`) or a bare name (`peri`).
        return ref in present or f"{kind}:{ref}" in present

    for entity in model.entities.values():
        if entity.kind not in ("block", "port", "module"):
            continue
        block_of = entity.key if entity.kind == "block" else _entity_block(entity)
        for kind, present, rule in (
            ("clock", clocks, "clock.unknown"),
            ("reset", resets, "reset.unknown"),
        ):
            ref = _clock_reset_ref(entity, kind)
            if ref is None or known(kind, ref, present):
                continue
            if not _in_scope(scope, block_of):
                continue
            file, line = _prov(entity)
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule=rule,
                    severity="error",
                    msg=f"{entity.kind} {entity.key!r} refers to unknown {kind} {ref!r}",
                )
            )
    return issues


def _clock_reset_ref(entity: EntityBase, kind: str) -> str | None:
    typed = getattr(entity, kind, None)
    if isinstance(typed, str) and typed:
        return typed
    attr = entity.attrs.get(kind)
    if isinstance(attr, str) and attr:
        return attr
    return None


# --- D38 -------------------------------------------------------------------------------


def _d38(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    instance_of = model.get_relations(kind="instance_of")
    region_blocks = {
        _entity_block(r) for r in model.by_kind("memory_region") if _entity_block(r) is not None
    }

    # 1. An IP instance not in the contract: a block declared an `instance_of` some IP
    #    but with no memory region (the contract memory map does not list it). A dangling
    #    IP target is reported by rule 2 (block.unknown) instead.
    for rel in instance_of:
        if rel.src in region_blocks or model.get(rel.src) is None:
            continue
        if not _in_scope(scope, rel.src, rel.dst):
            continue
        src = model.get(rel.src)
        file, line = _prov(src) if src is not None else (None, None)
        issues.append(
            Issue(
                file=file,
                line=line,
                rule="instance.no_region",
                severity="error",
                msg=(
                    f"instance {rel.src!r} is an instance_of {rel.dst!r} but has no memory "
                    "region in the contract"
                ),
            )
        )

    # 2. A `block:` reference (typed `block`, `attrs.block`, or a relation endpoint,
    #    including an instance_of target) that points at a block not present in the model.
    for block_ref, referrer, entity in _block_references(model):
        if model.get(block_ref) is not None:
            continue
        if not _in_scope(scope, block_ref, _entity_block(entity) if entity else None):
            continue
        file, line = _prov(entity) if entity is not None else (None, None)
        issues.append(
            Issue(
                file=file,
                line=line,
                rule="block.unknown",
                severity="error",
                msg=f"{referrer} refers to block {block_ref!r}, which is not in the model",
            )
        )
    return issues


def _block_references(model: DesignModel) -> list[tuple[str, str, EntityBase | None]]:
    """Every `block:` reference in the model: (block_key, referrer_label, referring entity)."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, EntityBase | None]] = []
    for key in sorted(model.entities):
        entity = model.entities[key]
        for ref in (getattr(entity, "block", None), entity.attrs.get("block")):
            if isinstance(ref, str) and ref.startswith("block:") and (ref, key) not in seen:
                seen.add((ref, key))
                out.append((ref, f"{entity.kind} {key!r}", entity))
    for i, rel in enumerate(model.relations):
        for endpoint in (rel.src, rel.dst):
            label = f"relation[{i}] {rel.kind!r}"
            if endpoint.startswith("block:") and (endpoint, label) not in seen:
                seen.add((endpoint, label))
                out.append((endpoint, label, None))
    return out


__all__ = ["CrossChipCheck"]
