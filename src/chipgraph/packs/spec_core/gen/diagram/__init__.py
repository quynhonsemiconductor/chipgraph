"""The ``diagram`` generator (pack ``spec-core``): figures rendered from the Design Model.

Public API:

- :func:`render` — pure: a :class:`~chipgraph.core.model.DesignModel` in, a
  ``{file name: bytes}`` mapping out. The same model always gives byte-identical bytes.
- :class:`DiagramGenerator` — the ``chipgraph.adapters.generator`` plugin: rebuilds a model
  from ``facts`` (entity/relation JSON dumps), writes the files under ``out_dir`` and
  returns their paths, sorted.

Four figures, each written as an SVG and an uncompressed drawio ``mxfile``:

======================  =========================================
Kind                     Files
======================  =========================================
``blocks``               ``block_diagram.svg`` / ``.drawio``
``memory_map``           ``memory_map.svg`` / ``.drawio``
``interrupts``           ``interrupt_map.svg`` / ``.drawio``
``clocks``               ``clock_reset_tree.svg`` / ``.drawio``
======================  =========================================

Determinism: everything is sorted by model key (or address, for the memory map), element
ids come from escaped model keys, coordinates are integers, XML is escaped, line endings
are ``\\n`` and the drawio ``mxfile`` carries no timestamp/etag/agent attribute. Figures
are computed from the model, never drawn by an AI (DESIGN.md 4.6).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from chipgraph.core.model import DEFAULT_ENTITY_KINDS, DesignModel, EntityBase, Relation
from chipgraph.packs.spec_core.gen.diagram._diagrams import (
    block_diagram,
    clock_reset_tree,
    interrupt_map,
    memory_map,
)
from chipgraph.packs.spec_core.gen.diagram._scene import Scene, scene_to_drawio, scene_to_svg

DEFAULT_KINDS: tuple[str, ...] = ("blocks", "memory_map", "interrupts", "clocks")
"""The diagram kinds :func:`render` produces by default, in output order."""

_BUILDERS = {
    "blocks": ("block_diagram", block_diagram),
    "memory_map": ("memory_map", memory_map),
    "interrupts": ("interrupt_map", interrupt_map),
    "clocks": ("clock_reset_tree", clock_reset_tree),
}


def render(model: DesignModel, kinds: Iterable[str] = DEFAULT_KINDS) -> dict[str, bytes]:
    """Render `model` into a ``{file name: bytes}`` mapping (SVG and drawio per kind).

    Raises ``ValueError`` for an unknown kind. The result is deterministic: the same model
    (regardless of entity insertion order) yields byte-identical output.
    """
    files: dict[str, bytes] = {}
    for kind in kinds:
        try:
            stem, builder = _BUILDERS[kind]
        except KeyError:
            raise ValueError(
                f"unknown diagram kind {kind!r}; known kinds: {sorted(_BUILDERS)}"
            ) from None
        scene: Scene = builder(model)
        files[f"{stem}.svg"] = scene_to_svg(scene)
        files[f"{stem}.drawio"] = scene_to_drawio(scene)
    return files


def facts_to_model(facts: Iterable[Mapping[str, object]]) -> DesignModel:
    """Rebuild a `DesignModel` from `facts` (entity and relation JSON dumps, intermixed).

    A fact with ``src`` and ``dst`` keys is a relation; every other mapping is an entity,
    parsed through the default entity-kind registry (so pack kinds round-trip as ext).
    """
    entities: list[EntityBase] = []
    relations: list[Relation] = []
    for fact in facts:
        if "src" in fact and "dst" in fact:
            relations.append(Relation.model_validate(dict(fact)))
        else:
            entities.append(DEFAULT_ENTITY_KINDS.parse(dict(fact)))
    return DesignModel.build(entities, relations)


class DiagramGenerator:
    """`Generator` plugin ``diagram``: render the Design Model figures to `out_dir`."""

    name = "diagram"

    def generate(self, out_dir: Path, facts: Iterable[Mapping[str, object]]) -> tuple[Path, ...]:
        """Rebuild the model from `facts`, write every figure under `out_dir`, return paths.

        Paths are returned sorted, and files are written with ``\\n`` line endings.
        """
        model = facts_to_model(facts)
        files = render(model)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name in sorted(files):
            path = out_dir / name
            path.write_bytes(files[name])
            written.append(path)
        return tuple(sorted(written))


__all__ = ["DEFAULT_KINDS", "DiagramGenerator", "facts_to_model", "render"]
