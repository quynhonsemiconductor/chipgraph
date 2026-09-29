"""Build the four Design Model figures as deterministic :class:`Scene` objects.

Each builder reads only from the typed Design Model, sorts everything by model key (or by
address, for the memory map), and lays elements out on a fixed integer grid so the SVG and
drawio renderers produce byte-identical output for a given model. Nothing here draws a
figure by hand or calls an AI: the picture is a function of the model (DESIGN.md 4.6).
"""

from __future__ import annotations

from chipgraph.core.model import DesignModel, EntityBase
from chipgraph.packs.spec_core.gen.diagram._scene import Box, Edge, Scene, escape_id

_MARGIN = 20
_COL_W = 180
_ROW_H = 44
_ROW_GAP = 12
_COL_GAP = 60
_EMPTY_W = 360


def _empty_scene(title: str, kind_desc: str) -> Scene:
    """A valid figure that says there is no data, for an empty or partial model."""
    box = Box(id="no_data", x=_MARGIN, y=_MARGIN, w=_EMPTY_W, h=_ROW_H, text="no data")
    return Scene(
        title=title,
        desc=f"{kind_desc}: no data in the model.",
        width=_EMPTY_W + 2 * _MARGIN,
        height=_ROW_H + 2 * _MARGIN,
        boxes=(box,),
    )


def _name_of(entity: EntityBase) -> str:
    return entity.name


def _attr_str(entity: EntityBase, key: str) -> str | None:
    value = entity.attrs.get(key)
    return value if isinstance(value, str) else None


def _attr_names(entity: EntityBase, key: str) -> tuple[str, ...]:
    value = entity.attrs.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _block_name(model: DesignModel, key: object) -> str:
    """The name of the block at `key`, or ``-`` when it is missing/unset."""
    if not isinstance(key, str):
        return "-"
    entity = model.get(key)
    return entity.name if entity is not None else "-"


def block_diagram(model: DesignModel) -> Scene:
    """Buses (interfaces) with their masters and slave blocks; lone blocks still shown."""
    title = "Block diagram"
    interfaces = sorted(model.by_kind("interface"), key=lambda e: e.key)
    blocks = {e.key: e for e in model.by_kind("block")}
    if not interfaces and not blocks:
        return _empty_scene(title, "Block diagram")

    # Which block keys are reached by a bus, so lone blocks can be listed separately.
    connected: set[str] = set()
    boxes: list[Box] = []
    edges: list[Edge] = []
    y = _MARGIN
    bus_x = _MARGIN
    slave_x = _MARGIN + _COL_W + _COL_GAP
    max_x = slave_x + _COL_W

    for interface in interfaces:
        masters = _attr_names(interface, "masters")
        bus_id = escape_id(interface.key)
        proto_value = getattr(interface, "protocol", None)
        proto = proto_value if isinstance(proto_value, str) else ""
        header = f"bus: {interface.name}"
        if proto:
            header += f"\n{proto}"
        if masters:
            header += "\nmasters: " + ", ".join(sorted(masters))
        bus_y = y
        boxes.append(Box(id=bus_id, x=bus_x, y=bus_y, w=_COL_W, h=_ROW_H, text=header))
        slaves = sorted(
            model.get_relations(src=interface.key, kind="connects"), key=lambda r: r.dst
        )
        row_y = y
        for relation in slaves:
            block = blocks.get(relation.dst)
            if block is None:
                continue
            connected.add(block.key)
            block_id = escape_id(block.key)
            boxes.append(Box(id=block_id, x=slave_x, y=row_y, w=_COL_W, h=_ROW_H, text=block.name))
            edges.append(Edge(id=f"{bus_id}__{block_id}", src=bus_id, dst=block_id))
            row_y += _ROW_H + _ROW_GAP
        y = max(row_y, bus_y + _ROW_H + _ROW_GAP)

    lone = sorted((b for k, b in blocks.items() if k not in connected), key=lambda e: e.key)
    if lone:
        boxes.append(Box(id="lone_header", x=_MARGIN, y=y, w=_COL_W, h=_ROW_H, text="not on a bus"))
        row_y = y
        for block in lone:
            boxes.append(
                Box(
                    id=escape_id(block.key),
                    x=slave_x,
                    y=row_y,
                    w=_COL_W,
                    h=_ROW_H,
                    text=block.name,
                )
            )
            row_y += _ROW_H + _ROW_GAP
        y = max(row_y, y + _ROW_H + _ROW_GAP)

    height = max(y, _MARGIN + _ROW_H) + _MARGIN
    return Scene(
        title=title,
        desc=(
            f"Block diagram: {len(interfaces)} bus(es), {len(blocks)} block(s), "
            f"{len(lone)} block(s) not on a bus."
        ),
        width=max_x + _MARGIN,
        height=height,
        boxes=tuple(boxes),
        edges=tuple(edges),
    )


def _region_base(entity: EntityBase) -> int | None:
    base = getattr(entity, "base", None)
    return base if isinstance(base, int) else None


def _region_size(entity: EntityBase) -> int | None:
    size = getattr(entity, "size", None)
    return size if isinstance(size, int) else None


def _hex_or(value: int | None, fallback: object) -> str:
    if value is not None:
        return f"{value:#010x}"
    return "auto" if fallback == "auto" else str(fallback) if fallback is not None else "?"


def _overlapping_keys(regions: list[EntityBase]) -> set[str]:
    """Model keys of regions that overlap another region (both endpoints marked)."""
    placed = [
        (e.key, base, size)
        for e in regions
        if (base := _region_base(e)) is not None and (size := _region_size(e)) is not None
    ]
    placed.sort(key=lambda t: (t[1], t[0]))
    bad: set[str] = set()
    for i, (key_a, base_a, size_a) in enumerate(placed):
        for key_b, base_b, _size_b in placed[i + 1 :]:
            if base_b >= base_a + size_a:
                break
            bad.add(key_a)
            bad.add(key_b)
    return bad


def memory_map(model: DesignModel) -> Scene:
    """Regions sorted by base; base/end/size in hex; overlaps outlined in the marker colour."""
    title = "Memory map"
    regions = model.by_kind("memory_region")
    if not regions:
        return _empty_scene(title, "Memory map")

    # Sort by base (numeric first, in address order), then by key for stability.
    def sort_key(entity: EntityBase) -> tuple[int, int, str]:
        base = _region_base(entity)
        if base is None:
            return (1, 0, entity.key)
        return (0, base, entity.key)

    ordered = sorted(regions, key=sort_key)
    bad = _overlapping_keys(list(regions))

    boxes: list[Box] = []
    width = 520
    y = _MARGIN
    header = Box(
        id="mm_header",
        x=_MARGIN,
        y=y,
        w=width,
        h=_ROW_H,
        text="Memory map (base / end / size / block)",
    )
    boxes.append(header)
    y += _ROW_H + _ROW_GAP
    for entity in ordered:
        base = _region_base(entity)
        size = _region_size(entity)
        end = base + size if base is not None and size is not None else None
        base_s = _hex_or(base, getattr(entity, "base", None))
        end_s = _hex_or(end, None) if end is not None else "?"
        size_s = _hex_or(size, getattr(entity, "size", None))
        block = getattr(entity, "block", None)
        block_name = _block_name(model, block)
        text = f"{entity.name}\n{base_s} .. {end_s}  size {size_s}  block {block_name}"
        overlap = entity.key in bad
        if overlap:
            text += "\n[OVERLAP]"
        boxes.append(
            Box(
                id=escape_id(entity.key),
                x=_MARGIN,
                y=y,
                w=width,
                h=_ROW_H + (16 if overlap else 0),
                text=text,
                overlap=overlap,
            )
        )
        y += _ROW_H + (16 if overlap else 0) + _ROW_GAP

    desc = f"Memory map: {len(regions)} region(s)"
    if bad:
        desc += f", {len(bad)} in an overlap (marked)"
    desc += "."
    return Scene(
        title=title,
        desc=desc,
        width=width + 2 * _MARGIN,
        height=y + _MARGIN,
        boxes=tuple(boxes),
    )


def interrupt_map(model: DesignModel) -> Scene:
    """Line number -> source (name, block); interrupts without a line/block are unassigned."""
    title = "Interrupt map"
    interrupts = model.by_kind("interrupt")
    if not interrupts:
        return _empty_scene(title, "Interrupt map")

    def line_of(entity: EntityBase) -> int | None:
        line = getattr(entity, "line", None)
        return line if isinstance(line, int) else None

    assigned = sorted(
        (e for e in interrupts if line_of(e) is not None and getattr(e, "block", None)),
        key=lambda e: (line_of(e), e.key),
    )
    unassigned = sorted(
        (e for e in interrupts if line_of(e) is None or not getattr(e, "block", None)),
        key=lambda e: e.key,
    )

    boxes: list[Box] = []
    width = 460
    y = _MARGIN
    boxes.append(
        Box(
            id="irq_header",
            x=_MARGIN,
            y=y,
            w=width,
            h=_ROW_H,
            text="Interrupt map (line -> source)",
        )
    )
    y += _ROW_H + _ROW_GAP
    # Two sources on one line are bad data: mark them like overlapping memory regions.
    line_counts: dict[int, int] = {}
    for entity in interrupts:
        number = line_of(entity)
        if number is not None:
            line_counts[number] = line_counts.get(number, 0) + 1
    for entity in assigned:
        line = line_of(entity)
        block = getattr(entity, "block", None)
        block_name = _block_name(model, block)
        shared = line is not None and line_counts.get(line, 0) > 1
        text = f"line {line}: {entity.name}  (block {block_name})"
        if shared:
            text += "  [line shared]"
        boxes.append(
            Box(
                id=escape_id(entity.key),
                x=_MARGIN,
                y=y,
                w=width,
                h=_ROW_H,
                text=text,
                overlap=shared,
            )
        )
        y += _ROW_H + _ROW_GAP
    if unassigned:
        boxes.append(
            Box(
                id="irq_unassigned",
                x=_MARGIN,
                y=y,
                w=width,
                h=_ROW_H,
                text="unassigned (no line, or no block in the model)",
            )
        )
        y += _ROW_H + _ROW_GAP
        for entity in unassigned:
            block = getattr(entity, "block", None)
            block_name = _block_name(model, block)
            line = line_of(entity)
            line_s = str(line) if line is not None else "-"
            shared = line is not None and line_counts.get(line, 0) > 1
            text = f"{entity.name}  (line {line_s}, block {block_name})"
            if shared:
                text += "  [line shared]"
            boxes.append(
                Box(
                    id=escape_id(entity.key),
                    x=_MARGIN,
                    y=y,
                    w=width,
                    h=_ROW_H,
                    text=text,
                    overlap=shared,
                )
            )
            y += _ROW_H + _ROW_GAP

    return Scene(
        title=title,
        desc=(f"Interrupt map: {len(assigned)} assigned line(s), {len(unassigned)} unassigned."),
        width=width + 2 * _MARGIN,
        height=y + _MARGIN,
        boxes=tuple(boxes),
    )


def _blocks_using(model: DesignModel, node: EntityBase, block_attr: str) -> list[EntityBase]:
    """Blocks that use a clock/reset `node`, via block.attrs[attr] or node.attrs['blocks']."""
    blocks = {e.key: e for e in model.by_kind("block")}
    found: dict[str, EntityBase] = {}
    for block in blocks.values():
        if _attr_str(block, block_attr) == node.name:
            found[block.key] = block
    for name in _attr_names(node, "blocks"):
        for block in blocks.values():
            if block.name == name:
                found[block.key] = block
    return sorted(found.values(), key=lambda e: e.key)


def clock_reset_tree(model: DesignModel) -> Scene:
    """Each clock and reset -> the blocks that use it."""
    title = "Clock/reset tree"
    clocks = sorted(model.by_kind("clock"), key=lambda e: e.key)
    resets = sorted(model.by_kind("reset"), key=lambda e: e.key)
    if not clocks and not resets:
        return _empty_scene(title, "Clock/reset tree")

    boxes: list[Box] = []
    edges: list[Edge] = []
    node_x = _MARGIN
    block_x = _MARGIN + _COL_W + _COL_GAP
    max_x = block_x + _COL_W
    y = _MARGIN
    n_edges = 0

    for node, kind_label, attr in (
        *((c, "clock", "clock") for c in clocks),
        *((r, "reset", "reset") for r in resets),
    ):
        node_id = escape_id(node.key)
        node_y = y
        boxes.append(
            Box(
                id=node_id,
                x=node_x,
                y=node_y,
                w=_COL_W,
                h=_ROW_H,
                text=f"{kind_label}: {node.name}",
            )
        )
        users = _blocks_using(model, node, attr)
        row_y = y
        for block in users:
            block_id = f"{node_id}__use__{escape_id(block.key)}"
            boxes.append(Box(id=block_id, x=block_x, y=row_y, w=_COL_W, h=_ROW_H, text=block.name))
            edges.append(Edge(id=f"e{n_edges}", src=node_id, dst=block_id))
            n_edges += 1
            row_y += _ROW_H + _ROW_GAP
        y = max(row_y, node_y + _ROW_H + _ROW_GAP)

    return Scene(
        title=title,
        desc=f"Clock/reset tree: {len(clocks)} clock(s), {len(resets)} reset(s).",
        width=max_x + _MARGIN,
        height=max(y, _MARGIN + _ROW_H) + _MARGIN,
        boxes=tuple(boxes),
        edges=tuple(edges),
    )
