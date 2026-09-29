"""A tiny deterministic scene model and its SVG / drawio renderers.

A :class:`Scene` is a title, a description and an ordered list of :class:`Box`,
:class:`Edge` and :class:`Row` elements laid out on a fixed integer grid. Both renderers
(:func:`scene_to_svg`, :func:`scene_to_drawio`) walk the same scene, so the two files
always agree, and both are byte-deterministic: elements keep the order the diagram code
put them in, ids come from escaped model keys, coordinates are integers, text is XML
escaped, and there are no timestamps, random ids or attributes that change between runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FONT_SIZE = 12
"""The single readable font size used for all element text."""

_OVERLAP_STROKE = "#cc0000"
"""The one non-monochrome colour, reserved for the memory-map overlap marker."""


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle with text, placed by its top-left corner on the grid."""

    id: str
    x: int
    y: int
    w: int
    h: int
    text: str
    overlap: bool = False
    """When true, the box is outlined in the overlap colour (bad-data marker)."""


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed connection between two boxes, drawn as an arrow."""

    id: str
    src: str
    dst: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class Scene:
    """A whole figure: metadata plus its boxes and edges, in draw order."""

    title: str
    desc: str
    width: int
    height: int
    boxes: tuple[Box, ...] = field(default_factory=tuple)
    edges: tuple[Edge, ...] = field(default_factory=tuple)


def xml_escape(text: str) -> str:
    """Escape a string for use in XML text and double-quoted attribute values."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def escape_id(key: str) -> str:
    """Turn a model key into a stable id token (letters, digits, ``_`` only)."""
    return "".join(c if c.isalnum() else "_" for c in key)


def _box_center(box: Box) -> tuple[int, int]:
    return box.x + box.w // 2, box.y + box.h // 2


def scene_to_svg(scene: Scene) -> bytes:
    """Render `scene` to a self-contained, accessible SVG document (UTF-8, ``\\n``)."""
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{scene.width}" height="{scene.height}" '
        f'viewBox="0 0 {scene.width} {scene.height}" '
        f'font-family="sans-serif" font-size="{FONT_SIZE}">'
    )
    lines.append(f"  <title>{xml_escape(scene.title)}</title>")
    lines.append(f"  <desc>{xml_escape(scene.desc)}</desc>")
    lines.append(
        '  <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        'orient="auto"><path d="M0,0 L7,3 L0,6 z" fill="#000000"/></marker></defs>'
    )
    by_id = {box.id: box for box in scene.boxes}
    for edge in scene.edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        x1, y1 = _box_center(src)
        x2, y2 = _box_center(dst)
        lines.append(
            f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#000000" marker-end="url(#arrow)"/>'
        )
        if edge.label:
            lx, ly = (x1 + x2) // 2, (y1 + y2) // 2
            lines.append(
                f'  <text x="{lx}" y="{ly}" text-anchor="middle">{xml_escape(edge.label)}</text>'
            )
    for box in scene.boxes:
        stroke = _OVERLAP_STROKE if box.overlap else "#000000"
        lines.append(
            f'  <rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" '
            f'fill="#ffffff" stroke="{stroke}"/>'
        )
        cx = box.x + box.w // 2
        for i, part in enumerate(box.text.split("\n")):
            ty = box.y + FONT_SIZE + 4 + i * (FONT_SIZE + 4)
            lines.append(
                f'  <text x="{cx}" y="{ty}" text-anchor="middle">{xml_escape(part)}</text>'
            )
    lines.append("</svg>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def scene_to_drawio(scene: Scene) -> bytes:
    """Render `scene` to an uncompressed drawio ``<mxfile>`` document (UTF-8, ``\\n``).

    The ``<mxfile>`` has no ``modified``/``etag``/``agent``/``host`` attributes, so two
    runs of the same scene produce byte-identical files.
    """
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(f'<mxfile><diagram id="0" name="{xml_escape(scene.title)}">')
    lines.append(
        f'<mxGraphModel dx="{scene.width}" dy="{scene.height}" grid="0" '
        f'pageWidth="{scene.width}" pageHeight="{scene.height}" math="0" shadow="0">'
    )
    lines.append("<root>")
    lines.append('<mxCell id="0"/>')
    lines.append('<mxCell id="1" parent="0"/>')
    for box in scene.boxes:
        stroke = _OVERLAP_STROKE if box.overlap else "#000000"
        style = (
            f"rounded=0;whiteSpace=wrap;html=0;fillColor=#ffffff;"
            f"strokeColor={stroke};fontColor=#000000;fontSize={FONT_SIZE}"
        )
        lines.append(
            f'<mxCell id="{box.id}" value="{xml_escape(box.text)}" '
            f'style="{style}" vertex="1" parent="1">'
        )
        lines.append(
            f'<mxGeometry x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" as="geometry"/>'
        )
        lines.append("</mxCell>")
    for edge in scene.edges:
        style = "endArrow=classic;html=0;strokeColor=#000000;fontColor=#000000"
        lines.append(
            f'<mxCell id="{edge.id}" value="{xml_escape(edge.label)}" style="{style}" '
            f'edge="1" parent="1" source="{edge.src}" target="{edge.dst}">'
        )
        lines.append('<mxGeometry relative="1" as="geometry"/>')
        lines.append("</mxCell>")
    lines.append("</root>")
    lines.append("</mxGraphModel>")
    lines.append("</diagram></mxfile>")
    return ("\n".join(lines) + "\n").encode("utf-8")
