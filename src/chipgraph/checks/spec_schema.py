"""`SpecSchemaCheck`: per-entity sanity of the spec side of the Design Model (layer 1).

Deterministic schema checks on what a spec declared, run over the model `chipgraph
ingest` built (DESIGN.md 4.4 "Schema", 4.8 layer 1):

- a register with no access, or no reset value;
- a field whose bits fall outside its register's width (32 when the width is unknown),
  or overlap another field of the same register, or have `msb < lsb`;
- a spec port with no direction, or no width;
- a requirement with empty text;
- `requirement.missing_id`: in a MAS file that declares at least one requirement ID, a
  top-level numbered item of its Verification section (`requirements.infer_heading`) that
  carries no ID (the extractor records it with `attrs.id_source = "missing"`). The issue
  points at the item's line and, when one can be proposed, names the next free ID of the
  file (`attrs.suggested_id`). See docs/REQUIREMENT_IDS.md.

Scope: with a `--block` (D38 IP block), only entities of that block (or its instances)
are checked; without one, the whole model is checked.
"""

from __future__ import annotations

import time

from chipgraph.checks._cross import (
    block_param,
    load_model_or_skip,
    result_from_issues,
    scope_block_keys,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.model.entities import (
    EntityBase,
    FieldEntity,
    PortEntity,
    RegisterEntity,
    RequirementEntity,
)
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_REGISTER_WIDTH = 32
"""The register width assumed for field-bounds checks when the register does not state
one (a common case: MAS register tables give field bits, not a register width)."""


class SpecSchemaCheck:
    """Checks the spec side of the Design Model for missing or inconsistent fields."""

    id = "spec_schema"
    name = "Spec schema"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        loaded = load_model_or_skip(spec, ctx, start)
        if isinstance(loaded, CheckResult):
            return loaded
        model = loaded.model
        scope = _scope(model, block_param(ctx))

        issues: list[Issue] = []
        issues.extend(_register_issues(model, scope))
        issues.extend(_field_issues(model, scope))
        issues.extend(_port_issues(model, scope))
        issues.extend(_requirement_issues(model, scope))
        return result_from_issues(spec, issues, loaded, start)


def _scope(model: DesignModel, block: str | None) -> set[str] | None:
    """The set of block keys to restrict to, or None for the whole model."""
    if block is None:
        return None
    return scope_block_keys(model, block)


def _entity_block(entity: EntityBase) -> str | None:
    """The block key an entity belongs to, from its typed field or `attrs.block`."""
    block = getattr(entity, "block", None)
    if isinstance(block, str) and block:
        return block
    attr_block = entity.attrs.get("block")
    if isinstance(attr_block, str) and attr_block:
        return attr_block
    return None


def _prov(entity: EntityBase) -> tuple[str | None, int | None]:
    return entity.source.file, entity.source.line


def _register_issues(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    for reg in model.by_kind("register"):
        assert isinstance(reg, RegisterEntity)
        if scope is not None and _entity_block(reg) not in scope:
            continue
        file, line = _prov(reg)
        if not reg.access:
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="register.no_access",
                    severity="error",
                    msg=f"register {reg.key!r} has no access mode",
                )
            )
        if reg.reset_value is None:
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="register.no_reset",
                    severity="error",
                    msg=f"register {reg.key!r} has no reset value",
                )
            )
    return issues


def _field_issues(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    fields_by_register: dict[str, list[FieldEntity]] = {}
    for field in model.by_kind("field"):
        assert isinstance(field, FieldEntity)
        if field.register is None:
            continue
        fields_by_register.setdefault(field.register, []).append(field)

    for reg_key, fields in fields_by_register.items():
        reg = model.get(reg_key)
        if scope is not None and (reg is None or _entity_block(reg) not in scope):
            continue
        width = reg.width if isinstance(reg, RegisterEntity) and reg.width else None
        limit = width if width is not None else _DEFAULT_REGISTER_WIDTH
        placed: list[tuple[int, int, str]] = []  # (lsb, msb, key), for overlap
        for field in sorted(fields, key=lambda f: f.key):
            file, line = _prov(field)
            if field.msb is None or field.lsb is None:
                # A field with no bit range cannot be range-checked; skip silently: a
                # missing width is a different concern from an out-of-range field.
                continue
            if field.msb < field.lsb:
                issues.append(
                    Issue(
                        file=file,
                        line=line,
                        rule="field.msb_lt_lsb",
                        severity="error",
                        msg=f"field {field.key!r} has msb {field.msb} < lsb {field.lsb}",
                    )
                )
                continue
            if field.msb >= limit:
                issues.append(
                    Issue(
                        file=file,
                        line=line,
                        rule="field.out_of_range",
                        severity="error",
                        msg=(
                            f"field {field.key!r} bits [{field.msb}:{field.lsb}] fall outside "
                            f"register {reg_key!r} width {limit}"
                        ),
                    )
                )
                continue
            for lsb2, msb2, key2 in placed:
                if field.lsb <= msb2 and lsb2 <= field.msb:
                    issues.append(
                        Issue(
                            file=file,
                            line=line,
                            rule="field.overlap",
                            severity="error",
                            msg=(
                                f"field {field.key!r} bits [{field.msb}:{field.lsb}] overlap "
                                f"field {key2!r} bits [{msb2}:{lsb2}]"
                            ),
                        )
                    )
                    break
            placed.append((field.lsb, field.msb, field.key))
    return issues


def _port_issues(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    for port in model.by_kind("port"):
        assert isinstance(port, PortEntity)
        block = _spec_port_block(port)
        if block is None:
            continue  # not a spec port (`port:spec.<block>.*`)
        if scope is not None and f"block:{block}" not in scope:
            continue
        file, line = _prov(port)
        if port.direction is None:
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="port.no_direction",
                    severity="error",
                    msg=f"spec port {port.key!r} has no direction",
                )
            )
        if port.width is None:
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="port.no_width",
                    severity="error",
                    msg=f"spec port {port.key!r} has no width",
                )
            )
    return issues


def _spec_port_block(port: PortEntity) -> str | None:
    """The block name of a spec port key (`port:spec.<block>.<name>`), else None."""
    _, _, rest = port.key.partition(":")
    parts = rest.split(".")
    if len(parts) >= 3 and parts[0] == "spec":
        return parts[1]
    return None


def _requirement_issues(model: DesignModel, scope: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    for req in model.by_kind("requirement"):
        assert isinstance(req, RequirementEntity)
        if scope is not None and not _requirement_in_scope(req, scope):
            continue
        if req.attrs.get("id_source") == "missing":
            file, line = _prov(req)
            suggested = req.attrs.get("suggested_id")
            hint = f" Suggested next ID: {suggested}" if isinstance(suggested, str) else ""
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="requirement.missing_id",
                    severity="error",
                    msg=(
                        f"Verification item has no ID; the other items of this file have one.{hint}"
                    ),
                )
            )
        if req.text is None or not req.text.strip():
            file, line = _prov(req)
            issues.append(
                Issue(
                    file=file,
                    line=line,
                    rule="requirement.empty_text",
                    severity="error",
                    msg=f"requirement {req.key!r} has empty text",
                )
            )
    return issues


def _requirement_in_scope(req: RequirementEntity, scope: set[str]) -> bool:
    """Whether a requirement belongs to a scoped block, by its `block`/key prefix."""
    block = _entity_block(req)
    if block is not None:
        return block in scope
    # A requirement key is `requirement:<block>.h<hash>` (inferred or missing, D37) or
    # `requirement:<ID>` (declared). Only the hashed form carries a block; a declared
    # ID that names no block is left to a whole-model run.
    _, _, rest = req.key.partition(":")
    head = rest.split(".", 1)[0]
    return f"block:{head}" in scope


__all__ = ["SpecSchemaCheck"]
