"""Deterministically merge per-source models into one Design Model (DESIGN.md 4.1-4.5).

`ingest` is the pure core of `chipgraph ingest`: it takes the models that the format
adapters and extractors already built, each tagged with where it came from
(`SourcePart`), and merges them into a single `DesignModel`, a `build_inputs_hash` and a
JSON-dumpable `IngestStats`. It never touches disk, never calls a tool, and names no
chip, bus, PDK or tool: the app layer (`chipgraph.app.ingest`) does all of that and hands
the results here.

The merge is deterministic (independent of the order the parts arrive) and never raises on
content: two parts that disagree on an entity's content produce one kept entity plus an
`IngestIssue(code="conflict")`, rather than a `ModelConflict`. Ownership of a module that
several blocks elaborate (vendor cells, a `top.f` that pulls in every block) is decided by
a fixed rule, so the same inputs always give the same owner.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.model.entities import EntityBase, ModuleEntity
from chipgraph.core.model.json_value import JSONValue
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.relations import Relation

Severity = Literal["error", "warning", "info"]

# Source priority for tie-breaking a content conflict: a chip-level spec beats a MAS,
# which beats extracted RTL. Anything unlisted sorts last. This is a role, not a concrete
# format or tool name, so core stays chip/tool-agnostic.
_SOURCE_PRIORITY: dict[str, int] = {"chip": 0, "mas": 1, "rtl": 2}


@dataclass(frozen=True, slots=True)
class SourcePart:
    """One already-built model, tagged with where it came from.

    `source` is a coarse role used only for conflict tie-breaking: `"chip"` (the
    chip-level spec), `"mas"` (a per-block spec) or `"rtl"` (extracted RTL). `block` is
    the block this part belongs to, or None for a part that spans blocks (a chip-level
    spec, or RTL elaborated with no single owning block). `diagnostics` are issues the
    adapter/extractor already found; ingest passes them through unchanged.
    """

    source: str
    block: str | None
    model: DesignModel
    diagnostics: tuple[IngestIssue, ...] = ()


class IngestIssue(BaseModel):
    """One problem found while ingesting, JSON-dumpable and never raised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity = Field(description="How serious the issue is.")
    code: str = Field(description="A short machine code, e.g. 'conflict', 'dangling'.")
    message: str = Field(description="A human-readable description.")
    key: str | None = Field(default=None, description="Model key the issue is about, if any.")
    block: str | None = Field(default=None, description="Block the issue is about, if any.")
    file: str | None = Field(default=None, description="Source file, repo-relative POSIX.")
    line: int | None = Field(default=None, description="1-based source line, if any.")
    sources: tuple[str, ...] = Field(
        default=(), description="The source roles involved (e.g. for a conflict)."
    )


class IngestStats(BaseModel):
    """Deterministic, JSON-dumpable counts describing an ingested model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: int = Field(default=0, description="Number of input files read.")
    entities: int = Field(default=0, description="Total entities in the merged model.")
    relations: int = Field(default=0, description="Total relations in the merged model.")
    entities_by_kind: dict[str, int] = Field(
        default_factory=dict, description="Entity count per kind."
    )
    entities_by_source: dict[str, int] = Field(
        default_factory=dict, description="Entity count per source role."
    )
    entities_by_block: dict[str, int] = Field(
        default_factory=dict, description="Entity count per owning block key ('' = none)."
    )
    relations_by_kind: dict[str, int] = Field(
        default_factory=dict, description="Relation count per kind."
    )
    diagnostics_by_severity: dict[str, int] = Field(
        default_factory=dict, description="Issue count per severity."
    )
    conflicts: int = Field(default=0, description="Number of content conflicts recorded.")


class IngestResult(BaseModel):
    """The outcome of `ingest`: the model is returned separately (it is not a pydantic model)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stats: IngestStats
    issues: tuple[IngestIssue, ...]
    build_inputs_hash: str


@dataclass(frozen=True, slots=True)
class InputFile:
    """One input file that was read, for the build-inputs hash."""

    rel_path: str
    sha256: str


@dataclass(slots=True)
class _MergeState:
    entities: dict[str, EntityBase] = field(default_factory=dict)
    entity_source: dict[str, str] = field(default_factory=dict)
    entity_owner_first: dict[str, bool] = field(default_factory=dict)
    entity_origin: dict[str, str] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    issues: list[IngestIssue] = field(default_factory=list)
    conflicts: int = 0


def ingest(
    parts: Iterable[SourcePart],
    *,
    input_files: Sequence[InputFile] = (),
    profile_digest: str | None = None,
) -> tuple[DesignModel, IngestResult]:
    """Merge `parts` into one model, plus stats, issues and a build-inputs hash.

    Deterministic: the returned model, stats and issues do not depend on the order of
    `parts`, and the module-ownership rule breaks every tie the same way each time. Never
    raises on conflicting content: a content conflict is kept-one-and-recorded.
    """
    ordered = _order_parts(parts)
    owners = _assign_ownership(ordered)

    state = _MergeState()
    for part in ordered:
        _merge_part(state, part, owners)

    _add_ownership_relations(state, owners)
    model = DesignModel(entities=dict(state.entities), relations=tuple(state.relations))

    for message in model.validate_relations():
        state.issues.append(IngestIssue(severity="warning", code="dangling", message=message))

    issues = _sorted_issues(state.issues + [d for part in ordered for d in part.diagnostics])
    stats = _compute_stats(model, state, owners, len(input_files), issues)
    build_inputs_hash = _build_inputs_hash(input_files, profile_digest)
    return model, IngestResult(stats=stats, issues=issues, build_inputs_hash=build_inputs_hash)


def _order_parts(parts: Iterable[SourcePart]) -> list[SourcePart]:
    """A deterministic, order-independent processing order for the parts.

    Sorted by (source priority, block key, a stable content digest), so shuffling the
    input never changes the merged result.
    """
    return sorted(parts, key=_part_sort_key)


def _part_sort_key(part: SourcePart) -> tuple[int, str, str, str]:
    priority = _SOURCE_PRIORITY.get(part.source, len(_SOURCE_PRIORITY))
    return (priority, part.source, part.block or "", _part_digest(part))


def _part_digest(part: SourcePart) -> str:
    hasher = hashlib.sha256()
    hasher.update(part.source.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update((part.block or "").encode("utf-8"))
    hasher.update(b"\x00")
    for key in sorted(part.model.entities):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


# --- module ownership --------------------------------------------------------------


def _assign_ownership(parts: Sequence[SourcePart]) -> dict[str, _Ownership]:
    """Decide which block owns each module key that appears in a block-tagged part.

    A module is a candidate for every block whose part contains it. With one candidate,
    that block owns it. With several, ownership goes to (in order): the block whose
    filelist directory contains the module's own file; else the block whose name matches
    the module (a filelist top); else the block whose part has the fewest modules, if that
    block is unique; else no owner. `attrs.used_by` records all candidates when more than
    one.
    """
    module_files: dict[str, str | None] = {}
    candidates: dict[str, list[str]] = {}
    module_counts: dict[str, int] = {}

    for part in parts:
        if part.block is None:
            continue
        block_modules = [e for e in part.model.entities.values() if isinstance(e, ModuleEntity)]
        module_counts[part.block] = len(block_modules)
        for module in block_modules:
            candidates.setdefault(module.key, [])
            if part.block not in candidates[module.key]:
                candidates[module.key].append(part.block)
            if module.key not in module_files or module_files[module.key] is None:
                module_files[module.key] = module.file

    owners: dict[str, _Ownership] = {}
    for module_key, cand in candidates.items():
        cand_sorted = sorted(cand)
        owner = _pick_owner(module_key, cand_sorted, module_files.get(module_key), module_counts)
        used_by = tuple(cand_sorted) if len(cand_sorted) > 1 else ()
        owners[module_key] = _Ownership(owner=owner, used_by=used_by)
    return owners


def _pick_owner(
    module_key: str,
    candidates: Sequence[str],
    module_file: str | None,
    module_counts: Mapping[str, int],
) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None

    # 1. The block whose filelist directory contains the module's file. A block key is
    #    "block:<name>"; the directory heuristic matches "/<name>/" in the file path.
    if module_file is not None:
        dir_matches = [b for b in candidates if _block_dir_matches(b, module_file)]
        if len(dir_matches) == 1:
            return dir_matches[0]

    # 2. The block whose name matches the module (its filelist's own top).
    name_matches = [b for b in candidates if _block_name_matches(b, module_key)]
    if len(name_matches) == 1:
        return name_matches[0]

    # 3. The block with the fewest modules, if that block is unique.
    fewest = min(module_counts.get(b, 0) for b in candidates)
    smallest = [b for b in candidates if module_counts.get(b, 0) == fewest]
    if len(smallest) == 1:
        return smallest[0]

    return None


def _block_name(block_key: str) -> str:
    _, _, rest = block_key.partition(":")
    return rest


def _block_dir_matches(block_key: str, module_file: str) -> bool:
    name = _block_name(block_key)
    if not name:
        return False
    return f"/{name}/" in f"/{module_file}"


def _block_name_matches(block_key: str, module_key: str) -> bool:
    _, _, module_name = module_key.partition(":")
    name = _block_name(block_key)
    if not name or not module_name:
        return False
    # The module is the block's own top when its name is or ends with the block name.
    return module_name == name or module_name.endswith(f"_{name}") or name in module_name.split("_")


@dataclass(frozen=True, slots=True)
class _Ownership:
    owner: str | None
    used_by: tuple[str, ...]


# --- merging -----------------------------------------------------------------------


def _merge_part(state: _MergeState, part: SourcePart, owners: Mapping[str, _Ownership]) -> None:
    is_owner_of = _owner_predicate(part, owners)
    for key, entity in part.model.entities.items():
        entity = _apply_ownership(entity, owners)
        origin = f"{part.source}:{part.block}" if part.block else part.source
        owner = is_owner_of(_owning_module(key, entity))
        _merge_entity(state, key, entity, part.source, owner, origin)
    state.relations.extend(part.model.relations)


def _owning_module(key: str, entity: EntityBase) -> str:
    """The module key whose ownership decides a conflict on `entity`.

    A module decides for itself; a port or parameter decides by the module it belongs to,
    so the owning block's elaboration of a shared module wins for its ports and parameters
    too. Anything else decides by its own key.
    """
    module = getattr(entity, "module", None)
    if isinstance(module, str) and module:
        return module
    return key


def _owner_predicate(part: SourcePart, owners: Mapping[str, _Ownership]) -> Callable[[str], bool]:
    def predicate(key: str) -> bool:
        ownership = owners.get(key)
        if ownership is None or ownership.owner is None:
            return part.block is None
        return ownership.owner == part.block

    return predicate


def _apply_ownership(entity: EntityBase, owners: Mapping[str, _Ownership]) -> EntityBase:
    if not isinstance(entity, ModuleEntity):
        return entity
    ownership = owners.get(entity.key)
    if ownership is None:
        return entity
    updates: dict[str, object] = {}
    if ownership.owner is not None:
        updates["block"] = ownership.owner
    if ownership.used_by:
        attrs: dict[str, JSONValue] = dict(entity.attrs)
        attrs["used_by"] = list(ownership.used_by)
        updates["attrs"] = attrs
    if not updates:
        return entity
    return entity.model_copy(update=updates)


def _merge_entity(
    state: _MergeState,
    key: str,
    entity: EntityBase,
    source: str,
    is_owner: bool,
    origin: str,
) -> None:
    existing = state.entities.get(key)
    if existing is None:
        state.entities[key] = entity
        state.entity_source[key] = source
        state.entity_owner_first[key] = is_owner
        state.entity_origin[key] = origin
        return
    if existing == entity:
        return

    # A content conflict: keep one by a fixed rule and record it.
    keep_existing = _keeps_existing(
        state.entity_owner_first[key], state.entity_source[key], is_owner, source, existing, entity
    )
    kept = existing if keep_existing else entity
    old_origin = state.entity_origin[key]
    kept_origin, dropped_origin = (old_origin, origin) if keep_existing else (origin, old_origin)
    if not keep_existing:
        state.entities[key] = entity
        state.entity_source[key] = source
        state.entity_owner_first[key] = is_owner
        state.entity_origin[key] = origin
    state.conflicts += 1
    state.issues.append(
        IngestIssue(
            severity="warning",
            code="conflict",
            key=key,
            message=(
                f"conflicting content for {key!r}; kept {kept_origin} "
                f"({kept.source.file or '?'}:{kept.source.line or '-'}), dropped {dropped_origin}"
            ),
            file=kept.source.file,
            line=kept.source.line,
            sources=tuple(sorted({state.entity_source[key], source})),
        )
    )


def _keeps_existing(
    existing_owner: bool,
    existing_source: str,
    new_owner: bool,
    new_source: str,
    existing: EntityBase,
    new: EntityBase,
) -> bool:
    """The fixed conflict rule: owner's part first, then source priority, then key order.

    Returns True to keep the already-stored entity, False to replace it with `new`.
    """
    if existing_owner != new_owner:
        return existing_owner
    existing_priority = _SOURCE_PRIORITY.get(existing_source, len(_SOURCE_PRIORITY))
    new_priority = _SOURCE_PRIORITY.get(new_source, len(_SOURCE_PRIORITY))
    if existing_priority != new_priority:
        return existing_priority <= new_priority
    # A final deterministic tie-break on the entity's own dumped content.
    return _content_key(existing) <= _content_key(new)


def _content_key(entity: EntityBase) -> str:
    return repr(entity.model_dump(mode="json"))


def _add_ownership_relations(state: _MergeState, owners: Mapping[str, _Ownership]) -> None:
    """Add a `contains` relation from an owner block to each module it owns.

    Only added when both the block and the module are present in the merged model, and
    only once per (block, module) pair.
    """
    seen: set[tuple[str, str]] = {(r.src, r.dst) for r in state.relations if r.kind == "contains"}
    for module_key, ownership in sorted(owners.items()):
        if ownership.owner is None:
            continue
        if ownership.owner not in state.entities or module_key not in state.entities:
            continue
        pair = (ownership.owner, module_key)
        if pair in seen:
            continue
        seen.add(pair)
        state.relations.append(Relation(kind="contains", src=ownership.owner, dst=module_key))


# --- stats and hashing -------------------------------------------------------------


def _compute_stats(
    model: DesignModel,
    state: _MergeState,
    owners: Mapping[str, _Ownership],
    input_count: int,
    issues: Sequence[IngestIssue],
) -> IngestStats:
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_block: dict[str, int] = {}
    for key, entity in model.entities.items():
        by_kind[entity.kind] = by_kind.get(entity.kind, 0) + 1
        source = state.entity_source.get(key, "")
        by_source[source] = by_source.get(source, 0) + 1
        block = _entity_block(entity)
        by_block[block] = by_block.get(block, 0) + 1

    by_relation_kind: dict[str, int] = {}
    for relation in model.relations:
        by_relation_kind[relation.kind] = by_relation_kind.get(relation.kind, 0) + 1

    by_severity: dict[str, int] = {}
    for issue in issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1

    return IngestStats(
        inputs=input_count,
        entities=len(model.entities),
        relations=len(model.relations),
        entities_by_kind=dict(sorted(by_kind.items())),
        entities_by_source=dict(sorted(by_source.items())),
        entities_by_block=dict(sorted(by_block.items())),
        relations_by_kind=dict(sorted(by_relation_kind.items())),
        diagnostics_by_severity=dict(sorted(by_severity.items())),
        conflicts=state.conflicts,
    )


def _entity_block(entity: EntityBase) -> str:
    block = getattr(entity, "block", None)
    if isinstance(block, str):
        return block
    attr_block = entity.attrs.get("block")
    if isinstance(attr_block, str):
        return attr_block
    return ""


def _sorted_issues(issues: Iterable[IngestIssue]) -> tuple[IngestIssue, ...]:
    _severity_order = {"error": 0, "warning": 1, "info": 2}
    return tuple(
        sorted(
            issues,
            key=lambda i: (
                _severity_order.get(i.severity, 3),
                i.code,
                i.key or "",
                i.file or "",
                i.line or 0,
                i.message,
            ),
        )
    )


def _build_inputs_hash(input_files: Sequence[InputFile], profile_digest: str | None) -> str:
    """sha256 over sorted (relative path, file sha256) plus the profile digest.

    Changing any listed input's content, or an ingest-relevant profile section, changes
    the hash; reordering the inputs does not.
    """
    hasher = hashlib.sha256()
    for input_file in sorted(input_files, key=lambda f: f.rel_path):
        hasher.update(input_file.rel_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(input_file.sha256.encode("utf-8"))
        hasher.update(b"\x00")
    if profile_digest is not None:
        hasher.update(b"profile\x00")
        hasher.update(profile_digest.encode("utf-8"))
    return hasher.hexdigest()


__all__ = [
    "IngestIssue",
    "IngestResult",
    "IngestStats",
    "InputFile",
    "SourcePart",
    "ingest",
]
