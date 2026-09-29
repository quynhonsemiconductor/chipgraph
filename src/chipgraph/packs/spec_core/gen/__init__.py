"""Deterministic generators for the ``spec-core`` pack.

Currently the diagram generator,
:class:`~chipgraph.packs.spec_core.gen.diagram.DiagramGenerator`, which renders block,
memory-map, interrupt-map and clock/reset-tree figures from the Design Model as SVG and
drawio. Figures are computed from the model, never drawn by an AI (DESIGN.md 4.6).
"""

from chipgraph.packs.spec_core.gen.diagram import DiagramGenerator, render

__all__ = ["DiagramGenerator", "render"]
