"""``mas-markdown``: a deterministic extractor for QNSC-style MAS Markdown.

Reads a Micro-Architecture Specification written in the QNSC template into the Design
Model. It never calls a model or a network; every fact comes from parsing the text, and
every problem is returned as a :class:`MasDiagnostic`, never raised (an unreadable row
still yields a model with what could be read).

What it extracts (see DESIGN.md 4.1-4.5, 4.8 and DECISIONS D37):

* **Requirements** -- declared REQ-IDs (matched by :meth:`RequirementsCfg.id_regex`) and,
  when ``requirements.infer == "verification"``, one inferred requirement per top-level
  numbered item of the ``infer_heading`` section. Declared wins over inferred. An
  inferred key is a content hash, so renumbering the list does not change it.
* **Interface ports** -- one :class:`PortEntity` per signal of the interface table(s),
  keyed ``port:spec.<block>.<signal>`` (the ``spec`` part keeps them apart from RTL
  ports for the M1-07 ``ports_diff``).
* **Registers and fields** -- from the register-map table, with a ``contains`` relation
  from each register to its fields.
* **Open items** -- dependency rows of the "Requirements on other owners" table and
  prose ``Open:`` lines / ``Open items:`` bullet lists.

The block name is *not* emitted as an entity here: the contract format adapter owns the
``block:<b>`` entity, and emitting one would conflict on merge.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from chipgraph.core.config.models import RequirementsCfg
from chipgraph.core.model import (
    DesignModel,
    EntityBase,
    FieldEntity,
    JSONValue,
    OpenItemEntity,
    PortEntity,
    RegisterEntity,
    Relation,
    RequirementEntity,
    make_key,
)
from chipgraph.core.model.provenance import Provenance
from chipgraph.packs.spec_core.extract import _markdown as md
from chipgraph.packs.spec_core.extract import _text as tx

_EXTRACTOR = "mas-markdown"

Severity = Literal["error", "warning", "info"]

_DIRECTIONS: dict[str, Literal["input", "output", "inout"]] = {
    "in": "input",
    "input": "input",
    "out": "output",
    "output": "output",
    "inout": "inout",
    "io": "inout",
}
_ACCESS_KINDS = ("RW", "RO", "WO", "W1C", "RSVD")


@dataclass(frozen=True, slots=True)
class MasDiagnostic:
    """A problem found while extracting, with file:line context. Never raised."""

    file: str | None
    line: int | None
    severity: Severity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MasTemplate:
    """Section titles the extractor looks for, defaulting to the QNSC MAS template.

    Titles are matched against a heading's number-stripped title, case-insensitive, at
    any ``#`` level.
    """

    interface: str = "Interface"
    registers: str = "Register map"
    open_items: str = "Requirements on others, and open items"


# Frozen, immutable defaults for the public API (avoids a call in a default argument).
_DEFAULT_REQUIREMENTS = RequirementsCfg()
_DEFAULT_TEMPLATE = MasTemplate()


@dataclass(slots=True)
class _Builder:
    """Accumulates entities, relations and diagnostics during one extraction."""

    file: str | None
    artifact: str | None
    entities: list[EntityBase] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    diagnostics: list[MasDiagnostic] = field(default_factory=list)

    def prov(self, line: int | None) -> Provenance:
        return Provenance(file=self.file, line=line, extractor=_EXTRACTOR, artifact=self.artifact)

    def diag(self, line: int | None, severity: Severity, code: str, message: str) -> None:
        self.diagnostics.append(MasDiagnostic(self.file, line, severity, code, message))


class MasExtractor:
    """Extracts Design Model facts from a QNSC-style MAS Markdown file."""

    name = "mas-markdown"

    def extract(self, path: Path) -> Iterable[Mapping[str, object]]:
        """The ``Extractor`` protocol: every entity and relation as a plain mapping.

        The block is guessed from the file name (``QNSC_<BLOCK>_MAS.md`` -> ``<BLOCK>``),
        falling back to ``"unknown"``. Callers that know the block and its config should
        use :meth:`extract_model` instead.
        """
        block = _block_from_filename(path) or "unknown"
        model, _ = self.extract_model(path, block=block)
        facts: list[Mapping[str, object]] = [
            model.entities[key].model_dump(mode="json") for key in sorted(model.entities)
        ]
        facts.extend(r.model_dump(mode="json") for r in model.relations)
        return facts

    @classmethod
    def extract_model(
        cls,
        path: Path,
        *,
        block: str,
        requirements: RequirementsCfg = _DEFAULT_REQUIREMENTS,
        root: Path | None = None,
        template: MasTemplate = _DEFAULT_TEMPLATE,
    ) -> tuple[DesignModel, tuple[MasDiagnostic, ...]]:
        """Extract a :class:`DesignModel` from the MAS at ``path`` for ``block``.

        Args:
            path: The MAS Markdown file.
            block: The block name (lower/upper filled into ``id_pattern``, and used in keys).
            requirements: How REQ-IDs are found (declared always, inferred if configured).
            root: Repo root; provenance ``file`` is written relative to it, POSIX.
            template: Section titles to look for.

        Returns:
            ``(model, diagnostics)``. Diagnostics are never raised; the model always holds
            whatever could be read.
        """
        text = path.read_text(encoding="utf-8")
        artifact = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rel_file = _display_path(path, root)
        builder = _Builder(file=rel_file, artifact=artifact)
        doc = md.scan(text)

        block_lower = block.lower()
        block_key = make_key("block", block_lower)

        _extract_requirements(builder, doc, block_lower, block_key, requirements)
        _extract_ports(builder, doc, block_lower, block_key, template)
        _extract_registers(builder, doc, block_lower, block_key, template)
        _extract_open_items(builder, doc, block_lower, template)

        model = DesignModel.build(builder.entities, builder.relations)
        return model, tuple(builder.diagnostics)


# --- requirements -----------------------------------------------------------------


def _extract_requirements(
    builder: _Builder,
    doc: md.Document,
    block: str,
    block_key: str,
    cfg: RequirementsCfg,
) -> None:
    id_regex = cfg.id_regex(block)
    declared: dict[str, int] = {}  # ID -> first source line

    # Declared REQ-IDs: first token of a list item, a paragraph, a heading, or the first
    # cell of a table row.
    for item in doc.all_list_items:
        _try_declare(
            builder,
            block_key,
            id_regex,
            declared,
            tx.strip_leading_number(item.text),
            item.text,
            item.line,
        )
    for section in doc.sections:
        if section.heading is not None:
            _try_declare(
                builder,
                block_key,
                id_regex,
                declared,
                section.heading.raw_title,
                section.heading.raw_title,
                section.heading.line,
            )
        for paragraph in section.paragraphs:
            _try_declare(
                builder,
                block_key,
                id_regex,
                declared,
                paragraph.text,
                paragraph.text,
                paragraph.line,
            )
    for table in doc.all_tables:
        for row in table.rows:
            if row.cells:
                _try_declare(
                    builder, block_key, id_regex, declared, row.cells[0], row.cells[0], row.line
                )

    if cfg.infer != "verification":
        return

    # Inferred requirements: each top-level numbered item of the infer_heading section.
    for section in md.iter_sections_by_title(doc, cfg.infer_heading):
        top_indent = _min_ordered_indent(section.list_items)
        for item in section.list_items:
            if not item.ordered or item.indent != top_indent:
                continue
            token = tx.strip_markup_token(tx.first_token(tx.strip_leading_number(item.text)))
            if token and id_regex.fullmatch(token):
                continue  # declared wins; already emitted above
            _emit_inferred(builder, block, block_key, item)


def _try_declare(
    builder: _Builder,
    block_key: str,
    id_regex: re.Pattern[str],
    declared: dict[str, int],
    head: str,
    full_text: str,
    line: int,
) -> None:
    token = tx.strip_markup_token(tx.first_token(head))
    if not token or not id_regex.fullmatch(token):
        return
    if token in declared:
        builder.diag(
            line,
            "error",
            "req.duplicate",
            f"requirement {token!r} declared again (first at line {declared[token]})",
        )
        return
    declared[token] = line
    text = _requirement_text(full_text, token)
    builder.entities.append(
        RequirementEntity(
            key=make_key("requirement", token),
            name=token,
            text=text or None,
            source=builder.prov(line),
            attrs={"id_source": "declared", "block": block_key},
        )
    )


def _emit_inferred(builder: _Builder, block: str, block_key: str, item: md.ListItem) -> None:
    normalized = tx.normalize_item_text(item.text)
    digest = tx.content_hash8(normalized)
    key = make_key("requirement", f"{block}.h{digest}")
    builder.entities.append(
        RequirementEntity(
            key=key,
            name=_short_name(normalized),
            text=normalized or None,
            source=builder.prov(item.line),
            attrs={"id_source": "inferred", "block": block_key},
        )
    )


def _requirement_text(full_text: str, token: str) -> str:
    """The requirement body: the item minus a leading number and the ID token."""
    body = tx.strip_leading_number(full_text).strip()
    # Drop the leading ID token (possibly wrapped in `` ` `` / ``**``).
    first = tx.first_token(body)
    if tx.strip_markup_token(first) == token:
        body = body[len(first) :].strip()
    return tx.collapse_whitespace(body)


def _min_ordered_indent(items: Iterable[md.ListItem]) -> int:
    indents = [i.indent for i in items if i.ordered]
    return min(indents) if indents else 0


# --- interface ports --------------------------------------------------------------


def _extract_ports(
    builder: _Builder,
    doc: md.Document,
    block: str,
    block_key: str,
    template: MasTemplate,
) -> None:
    seen: set[str] = set()
    for section in md.iter_sections_by_title(doc, template.interface):
        for table in section.tables:
            cols = _column_index(table.headers)
            if not {"signal", "dir", "width"} <= set(cols):
                continue  # not an interface table (memory map, etc.)
            _ports_from_table(builder, table, cols, block, block_key, seen)


def _ports_from_table(
    builder: _Builder,
    table: md.Table,
    cols: dict[str, int],
    block: str,
    block_key: str,
    seen: set[str],
) -> None:
    ncols = len(table.headers)
    for row in table.rows:
        if len(row.cells) != ncols:
            builder.diag(
                row.line,
                "error",
                "table.row_width",
                f"row has {len(row.cells)} cells, header has {ncols}",
            )
            continue
        signal_cell = row.cells[cols["signal"]]
        dir_cell = row.cells[cols["dir"]]
        width_cell = row.cells[cols["width"]]
        desc = row.cells[cols["description"]] if "description" in cols else ""
        signals = _split_signals(signal_cell)
        if not signals:
            continue
        directions = _split_multi(dir_cell, len(signals))
        widths = _split_multi(width_cell, len(signals))
        for idx, signal in enumerate(signals):
            direction = _parse_direction(builder, directions[idx], row.line)
            width = _parse_width(widths[idx])
            key = make_key("port", "spec", block, signal)
            if key in seen:
                continue
            seen.add(key)
            builder.entities.append(
                PortEntity(
                    key=key,
                    name=signal,
                    direction=direction,
                    width=width,
                    module=None,
                    source=builder.prov(row.line),
                    attrs={
                        "block": block_key,
                        "origin": "spec",
                        **({"description": desc} if desc else {}),
                    },
                )
            )


def _split_signals(cell: str) -> list[str]:
    """Split a signal cell into individual signal names.

    Handles ``` `a`, `b`, `c` ``` (several signals in one cell) and a single signal.
    Non-identifier fragments are dropped (e.g. a stray note).
    """
    cleaned = tx.strip_cell_markup(cell)
    if not cleaned:
        return []
    parts = [p.strip() for p in cleaned.split(",")]
    return [p for p in parts if tx.is_identifier(p)]


def _split_multi(cell: str, count: int) -> list[str]:
    """Distribute a Dir/Width cell across ``count`` signals.

    ``1 each`` and a single value apply to every signal; a comma-separated list maps one
    value per signal (padded with the last value if short).
    """
    value = tx.strip_cell_markup(cell)
    lowered = value.lower()
    if lowered.endswith(" each"):
        base = value[: -len(" each")].strip()
        return [base] * count
    if "," in value:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) < count:
            parts += [parts[-1]] * (count - len(parts))
        return parts[:count]
    return [value] * count


def _parse_direction(
    builder: _Builder, cell: str, line: int
) -> Literal["input", "output", "inout"] | None:
    value = tx.strip_cell_markup(cell).lower()
    if value in _DIRECTIONS:
        return _DIRECTIONS[value]
    builder.diag(line, "error", "port.direction", f"unknown port direction {cell.strip()!r}")
    return None


def _parse_width(cell: str) -> int | str | None:
    value = tx.strip_cell_markup(cell)
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return value


# --- registers and fields ---------------------------------------------------------


def _extract_registers(
    builder: _Builder,
    doc: md.Document,
    block: str,
    block_key: str,
    template: MasTemplate,
) -> None:
    reg_names: dict[str, int] = {}  # register name -> first line, per block
    for section in md.iter_sections_by_title(doc, template.registers):
        for table in section.tables:
            cols = _column_index(table.headers)
            colset = set(cols)
            has_full = {"register", "field", "bits", "access"} <= colset
            has_short = (
                {"offset", "register", "access", "reset"} <= colset
                and "field" not in cols
                and "bits" not in cols
            )
            if has_full:
                _registers_from_table(
                    builder, table, cols, block, block_key, reg_names, with_fields=True
                )
            elif has_short:
                _registers_from_table(
                    builder, table, cols, block, block_key, reg_names, with_fields=False
                )
            else:
                builder.diag(
                    table.header_line,
                    "warning",
                    "register.table_form",
                    "register table not in template form; no registers read from it",
                )


def _registers_from_table(
    builder: _Builder,
    table: md.Table,
    cols: dict[str, int],
    block: str,
    block_key: str,
    reg_names: dict[str, int],
    *,
    with_fields: bool,
) -> None:
    ncols = len(table.headers)
    current_reg_key: str | None = None
    for row in table.rows:
        if len(row.cells) != ncols:
            builder.diag(
                row.line,
                "error",
                "table.row_width",
                f"row has {len(row.cells)} cells, header has {ncols}",
            )
            continue
        offset_cell = row.cells[cols["offset"]] if "offset" in cols else ""
        register_cell = row.cells[cols["register"]]
        reg_name = tx.strip_cell_markup(register_cell)

        is_continuation = with_fields and not tx.strip_cell_markup(offset_cell) and not reg_name
        if is_continuation:
            if current_reg_key is not None:
                _emit_field(builder, table, cols, row, current_reg_key, block, block_key)
            continue

        # A reserved / array / range row: no register entity.
        if _is_reserved_offset(offset_cell) or not tx.is_identifier(register_cell):
            builder.diag(
                row.line,
                "info",
                "register.reserved",
                f"reserved or non-register row {reg_name or '--'!r}; no register entity",
            )
            current_reg_key = None
            continue

        if reg_name in reg_names:
            builder.diag(
                row.line,
                "error",
                "register.duplicate",
                f"register {reg_name!r} defined again (first at line {reg_names[reg_name]})",
            )
            continue
        reg_names[reg_name] = row.line
        offset = tx.parse_int_maybe(offset_cell)
        access, access_note = _parse_access(builder, row.cells, cols, row.line)
        reset = _parse_reset(row.cells, cols)
        reg_key = make_key("register", block, reg_name)
        attrs: dict[str, JSONValue] = {}
        if access_note:
            attrs["access_note"] = access_note
        builder.entities.append(
            RegisterEntity(
                key=reg_key,
                name=reg_name,
                block=block_key,
                offset=offset,
                reset_value=reset,
                access=access,
                source=builder.prov(row.line),
                attrs=attrs,
            )
        )
        current_reg_key = reg_key
        if with_fields:
            _emit_field(builder, table, cols, row, reg_key, block, block_key)


def _emit_field(
    builder: _Builder,
    table: md.Table,
    cols: dict[str, int],
    row: md.TableRow,
    reg_key: str,
    block: str,
    block_key: str,
) -> None:
    if "field" not in cols:
        return
    field_cell = row.cells[cols["field"]]
    field_name = tx.strip_cell_markup(field_cell)
    if not tx.is_identifier(field_cell):
        builder.diag(
            row.line,
            "info",
            "field.reserved",
            f"reserved or non-field cell {field_name or '--'!r}; no field entity",
        )
        return
    bits = tx.parse_bits(row.cells[cols["bits"]]) if "bits" in cols else None
    msb, lsb = (bits[0], bits[1]) if bits is not None else (None, None)
    access, access_note = _parse_access(builder, row.cells, cols, row.line)
    reset = _parse_reset(row.cells, cols)
    _, reg_name = reg_key.split(":", 1)
    reg_name = reg_name.split(".", 1)[1]
    field_key = make_key("field", block, reg_name, field_name)
    attrs: dict[str, JSONValue] = {}
    if access_note:
        attrs["access_note"] = access_note
    builder.entities.append(
        FieldEntity(
            key=field_key,
            name=field_name,
            register=reg_key,
            msb=msb,
            lsb=lsb,
            access=access,
            reset_value=reset,
            source=builder.prov(row.line),
            attrs=attrs,
        )
    )
    builder.relations.append(
        Relation(kind="contains", src=reg_key, dst=field_key, source=builder.prov(row.line))
    )


def _is_reserved_offset(cell: str) -> bool:
    """True for an offset range (``0x28``-``0x3C``) or an ``--`` placeholder."""
    value = tx.strip_cell_markup(cell)
    if value in ("", "--", "—"):
        return True
    # Ranges: two hex offsets joined by - or -- (already markup-stripped).
    return bool(re.fullmatch(r"0x[0-9a-fA-F_]+\s*-{1,2}\s*0x[0-9a-fA-F_]+", value))


def _parse_access(
    builder: _Builder, cells: tuple[str, ...], cols: dict[str, int], line: int
) -> tuple[str | None, str | None]:
    if "access" not in cols:
        return None, None
    raw = tx.strip_cell_markup(cells[cols["access"]])
    if not raw or raw in ("--", "—"):
        return None, None
    head, _, rest = raw.partition(",")
    kind = head.strip().upper()
    if kind not in _ACCESS_KINDS:
        builder.diag(line, "error", "register.access", f"unknown access mode {head.strip()!r}")
        return None, None
    note = rest.strip() or None
    return kind.lower(), note


def _parse_reset(cells: tuple[str, ...], cols: dict[str, int]) -> int | str | None:
    if "reset" not in cols:
        return None
    return tx.parse_int_maybe(cells[cols["reset"]])


# --- open items -------------------------------------------------------------------

_DEP_HEADER = ("item", "owner", "what it blocks")
_OPEN_PROSE = re.compile(r"^\s*(?:\*\*\s*)?open\s*:?\s*", re.IGNORECASE)
_OPEN_LIST_LEAD = re.compile(r"^\s*open items\b", re.IGNORECASE)
_OPEN_NONE = re.compile(r"^\s*open items\s*:?\s*none\.?\s*$", re.IGNORECASE)


def _extract_open_items(
    builder: _Builder, doc: md.Document, block: str, template: MasTemplate
) -> None:
    seen: set[str] = set()
    for section in md.iter_sections_by_title(doc, template.open_items):
        _dependency_rows(builder, section, block, seen)
        _prose_open_items(builder, section, block, seen)


def _dependency_rows(builder: _Builder, section: md.Section, block: str, seen: set[str]) -> None:
    for table in section.tables:
        cols = _column_index(table.headers)
        if not {"item", "owner", "what it blocks"} <= set(cols):
            continue
        ncols = len(table.headers)
        for row in table.rows:
            if len(row.cells) != ncols:
                builder.diag(
                    row.line,
                    "error",
                    "table.row_width",
                    f"row has {len(row.cells)} cells, header has {ncols}",
                )
                continue
            item_text = tx.collapse_whitespace(row.cells[cols["item"]])
            if not item_text:
                continue
            owner = tx.collapse_whitespace(row.cells[cols["owner"]])
            blocks = tx.collapse_whitespace(row.cells[cols["what it blocks"]])
            _emit_open_item(
                builder,
                block,
                item_text,
                row.line,
                seen,
                item_type="dependency",
                extra={"owner": owner, "blocks": blocks},
            )


def _prose_open_items(builder: _Builder, section: md.Section, block: str, seen: set[str]) -> None:
    # "Open: ..." / "**Open:** ..." single-line items (from prose).
    for paragraph in section.paragraphs:
        text = paragraph.text
        if _OPEN_NONE.match(text):
            continue
        if _OPEN_PROSE.match(text) and not _OPEN_LIST_LEAD.match(text):
            body = _OPEN_PROSE.sub("", text).strip().rstrip("*").strip()
            if body:
                _emit_open_item(builder, block, body, paragraph.line, seen, item_type="open")

    # "Open items[, <owner>]:" followed by a bullet list.
    bullet_context = _open_bullet_context(section)
    for item in section.list_items:
        if id(item) in bullet_context:
            body = tx.collapse_whitespace(item.text).rstrip("*").strip()
            if body:
                _emit_open_item(builder, block, body, item.line, seen, item_type="open")


def _open_bullet_context(section: md.Section) -> set[int]:
    """Return ids of the list items that follow an ``Open items:`` prose lead.

    A bullet list belongs to the open items only when the closest preceding paragraph is
    an ``Open items[, owner]:`` lead (and not ``Accepted limits`` or ``none``).
    """
    leads = [
        p.line
        for p in section.paragraphs
        if _OPEN_LIST_LEAD.match(p.text) and not _OPEN_NONE.match(p.text)
    ]
    if not leads:
        return set()
    stops = sorted(p.line for p in section.paragraphs if not _OPEN_LIST_LEAD.match(p.text))
    result: set[int] = set()
    for lead_line in leads:
        # The bullets governed by this lead run until the next stopping paragraph.
        next_stop = min((s for s in stops if s > lead_line), default=None)
        for item in section.list_items:
            if item.line > lead_line and (next_stop is None or item.line < next_stop):
                result.add(id(item))
    return result


def _emit_open_item(
    builder: _Builder,
    block: str,
    text: str,
    line: int,
    seen: set[str],
    *,
    item_type: str,
    extra: Mapping[str, JSONValue] | None = None,
) -> None:
    normalized = tx.collapse_whitespace(text)
    digest = tx.content_hash8(normalized)
    key = make_key("open_item", f"{block}.h{digest}")
    if key in seen:
        return
    seen.add(key)
    attrs: dict[str, JSONValue] = {"type": item_type}
    if extra:
        attrs.update(extra)
    builder.entities.append(
        OpenItemEntity(
            key=key,
            name=_short_name(normalized),
            status="open",
            source=builder.prov(line),
            attrs=attrs,
        )
    )


# --- shared helpers ---------------------------------------------------------------


def _column_index(headers: Iterable[str]) -> dict[str, int]:
    """Map a lower-cased, markup-stripped header name to its column index."""
    index: dict[str, int] = {}
    for i, header in enumerate(headers):
        name = tx.strip_cell_markup(header).lower()
        if name and name not in index:
            index[name] = i
    return index


def _short_name(text: str, limit: int = 60) -> str:
    """A short human name: the first ~``limit`` chars of ``text``."""
    collapsed = tx.collapse_whitespace(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip()


def _block_from_filename(path: Path) -> str | None:
    """Guess the block from ``QNSC_<BLOCK>_MAS.md`` / ``<BLOCK>_MAS.md`` file names."""
    stem = path.stem
    match = re.fullmatch(r"(?:QNSC_)?(.+?)_MAS", stem)
    if match:
        return match.group(1).lower()
    return None


def _display_path(path: Path, root: Path | None) -> str:
    """``path`` relative to ``root`` (POSIX) when under it, else its POSIX form."""
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


__all__ = ["MasDiagnostic", "MasExtractor", "MasTemplate"]
