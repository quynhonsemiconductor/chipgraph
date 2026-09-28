"""Block-by-block Jinja2 template overrides (DESIGN 8.6 V2, docs/IMPLEMENTATION_PLAN.md M1-22).

A `TemplateSet` renders named templates (e.g. `rtl_module.sv.j2`) built from an ordered
stack of layers, most specific first. DESIGN.md's lookup order for a template is::

    block -> project -> organisation -> pack

("block" here is the project's own per-IP-block layer, DESIGN 8.6's `blocks:` section;
"pack" is the lowest, always-present fallback.) Each layer may contribute, for a given
template name:

* a **whole-file** template: a file named exactly like the template, in that layer's
  ``directory``, whose top level has real content (not just `{% block %}` tags). The most
  specific layer that has a whole file for a name is the base of the render.
* a **block-only fragment** that overrides individual `{% block %}` regions without
  touching the rest of the file. Two conventions produce a fragment, and both may be used
  together:

  1. **Same file name, in a more specific layer's directory**, containing *only*
     `{% block ... %}...{% endblock %}` definitions (no other top-level output). Detected
     automatically by parsing the file: if every top-level node is a block definition or
     whitespace, it is a fragment, not a whole-file replacement.
  2. **`templates.override` mapping** (`ResolvedProfile.for_block(...).profile.templates
     .override`, or any other `Mapping[str, str]` a caller assembles the same way): a
     block name (not necessarily the template's file name) to a file path containing that
     one block's `{% block <name> %}...{% endblock %}` definition. This is what DESIGN.md's
     example ``templates: override: { rtl_header: templates/header.j2 }`` means: the key is
     the *block name* `rtl_header`, not a template file name, so it applies to any template
     in the chain that defines a block called `rtl_header`.

Both conventions are implemented with real Jinja2 inheritance: each more specific layer's
contribution is compiled as a virtual template `layer:<n>/<name>` that `{% extends %}` the
next less specific layer's virtual template. A block a more specific layer does not touch
is therefore untouched in the rendered output -- it simply falls through Jinja's own block
resolution. This is also why overriding one block never changes any other block's source
or rendering: each block's *content* lives in exactly one layer's fragment/whole file.

Gap vs. `core/config/models.py`: `BlockOverride` (the project's own `blocks:` per-IP-block
section) has no `templates` field, so a project cannot currently express a block-scoped
template override through the resolved `Profile` alone -- only a project-wide
`templates.override`. `TemplateSet` accepts a "block" layer as plain data (an optional
directory and/or override mapping) so a caller can still supply one (e.g. read from
`.chipgraph/blocks/<block>/templates/`, by convention, until `models.py` grows a field);
this module does not itself decide where a per-block layer's directory or overrides come
from.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import BaseLoader, Environment, StrictUndefined, nodes
from jinja2.exceptions import TemplateNotFound as Jinja2TemplateNotFound
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment

__all__ = [
    "BlockSource",
    "TemplateLayer",
    "TemplateNotFoundError",
    "TemplateRenderError",
    "TemplateSet",
    "TemplateSetError",
]

_VIRTUAL_PREFIX = "layer:"


class TemplateSetError(Exception):
    """Base error for `TemplateSet` problems."""


class TemplateNotFoundError(TemplateSetError):
    """No layer contributes anything (whole file or fragment) for a template name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"template {name!r} not found in any layer")
        self.name = name


class TemplateRenderError(TemplateSetError):
    """Rendering failed; names the template and the layer chain that produced it."""

    def __init__(self, name: str, layers: Sequence[str], cause: Exception) -> None:
        chain = " -> ".join(layers) if layers else "(no layer)"
        super().__init__(
            f"error rendering template {name!r} (layer chain, least to most specific: "
            f"{chain}): {cause}"
        )
        self.name = name
        self.layers = tuple(layers)
        self.__cause__ = cause


@dataclass(frozen=True)
class TemplateLayer:
    """One layer in the lookup stack: a directory of whole/fragment files, and/or an
    explicit block-name-to-path override mapping (DESIGN 8.6 V2's `templates.override`).

    `name` is a short label used in `explain()` output and error messages, e.g. "block",
    "project", "org", "pack".
    """

    name: str
    directory: Path | None = None
    override: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BlockSource:
    """Which layer (and file, if any) a rendered block's content came from."""

    block: str
    layer: str
    path: Path | None


@dataclass(frozen=True)
class _Contribution:
    """One layer's compiled contribution to a template's inheritance chain."""

    layer_name: str
    source: str
    is_root: bool  # True: a whole-file base/replacement (does not extend the chain so far)
    block_paths: dict[str, Path | None]


def _read(path: str | Path) -> str:
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateSetError(f"cannot read template file {p}: {exc}") from exc


def _is_pure_fragment(source: str, parser_env: Environment, origin: str) -> bool:
    """True if `source`'s top level has only `{% block %}` tags and/or whitespace text."""
    try:
        parsed = parser_env.parse(source)
    except Exception as exc:
        raise TemplateSetError(f"cannot parse template fragment {origin}: {exc}") from exc
    for top in parsed.body:
        if isinstance(top, nodes.Block):
            continue
        if isinstance(top, nodes.Output) and all(
            isinstance(child, nodes.TemplateData) and child.data.strip() == ""
            for child in top.nodes
        ):
            continue
        return False
    return True


def _block_names(source: str, parser_env: Environment, origin: str) -> list[str]:
    try:
        parsed = parser_env.parse(source)
    except Exception as exc:
        raise TemplateSetError(f"cannot parse template fragment {origin}: {exc}") from exc
    return [b.name for b in parsed.find_all(nodes.Block)]


class _LayeredLoader(BaseLoader):
    """Serves the virtual `layer:<n>/<name>` sources a `TemplateSet` compiles."""

    def __init__(self) -> None:
        self._sources: dict[str, str] = {}

    def add(self, virtual_name: str, source: str) -> None:
        self._sources[virtual_name] = source

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str | None, Callable[[], bool] | None]:
        try:
            source = self._sources[template]
        except KeyError as exc:
            raise Jinja2TemplateNotFound(template) from exc
        return source, None, lambda: True


class TemplateSet:
    """Renders templates built from ordered layers (most specific first)."""

    def __init__(self, layers: Sequence[TemplateLayer]) -> None:
        self._layers: tuple[TemplateLayer, ...] = tuple(layers)
        self._loader = _LayeredLoader()
        self._env = SandboxedEnvironment(
            loader=self._loader,
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        # A plain (non-sandboxed) environment used only to parse candidate sources when
        # deciding whether they are whole files or block-only fragments, and to list the
        # block names they define. No template of ours is *rendered* through it.
        self._parser_env = Environment()
        self._chains: dict[str, tuple[list[str], dict[str, BlockSource]]] = {}

    # -- chain construction --------------------------------------------------------

    def _gather_layer_candidates(
        self, layer: TemplateLayer, name: str
    ) -> tuple[str | None, Path | None, dict[str, str]]:
        """Return (whole_or_fragment_source_for_name, its_path, {block_name: path}) for
        one layer's directory file plus any `override` entries keyed by `name` itself."""
        whole_source: str | None = None
        whole_path: Path | None = None

        if name in layer.override:
            whole_path = Path(layer.override[name])
            whole_source = _read(whole_path)
        elif layer.directory is not None:
            candidate = layer.directory / name
            if candidate.is_file():
                whole_path = candidate
                whole_source = _read(candidate)

        block_overrides = {
            block_name: path for block_name, path in layer.override.items() if block_name != name
        }
        return whole_source, whole_path, block_overrides

    def _build_contribution(self, layer: TemplateLayer, name: str) -> _Contribution | None:
        whole_source, whole_path, block_override_paths = self._gather_layer_candidates(layer, name)

        pieces: list[str] = []
        block_paths: dict[str, Path | None] = {}
        is_root = False

        if whole_source is not None:
            origin = str(whole_path)
            if _is_pure_fragment(whole_source, self._parser_env, origin):
                pieces.append(whole_source)
                for block_name in _block_names(whole_source, self._parser_env, origin):
                    block_paths[block_name] = whole_path
            else:
                # A whole (non-fragment) file: it is the base, replacing anything less
                # specific accumulated so far.
                pieces = [whole_source]
                for block_name in _block_names(whole_source, self._parser_env, origin):
                    block_paths[block_name] = whole_path
                is_root = True

        for block_name, path_str in block_override_paths.items():
            path = Path(path_str)
            source = _read(path)
            origin = str(path)
            if not _is_pure_fragment(source, self._parser_env, origin):
                raise TemplateSetError(
                    f"templates.override[{block_name!r}] = {origin!r} must contain only "
                    "{% block %} definitions, not whole-template content"
                )
            pieces.append(source)
            for found in _block_names(source, self._parser_env, origin):
                block_paths[found] = path

        if not pieces:
            return None

        return _Contribution(
            layer_name=layer.name,
            source="\n".join(pieces),
            is_root=is_root,
            block_paths=block_paths,
        )

    def _build_chain(self, name: str) -> tuple[list[str], dict[str, BlockSource]]:
        cached = self._chains.get(name)
        if cached is not None:
            return cached

        # Layers are stored most-specific-first; process least-specific-first so each
        # more specific contribution can `{% extends %}` the previous virtual template.
        layers_asc = list(reversed(self._layers))

        parent_virtual: str | None = None
        chain_layer_names: list[str] = []
        block_sources: dict[str, BlockSource] = {}
        index = 0

        for layer in layers_asc:
            contribution = self._build_contribution(layer, name)
            if contribution is None:
                continue

            if contribution.is_root:
                # Resets the chain: this whole file replaces anything accumulated so far.
                source = contribution.source
                parent_virtual = None
                chain_layer_names = []
            else:
                if parent_virtual is None:
                    raise TemplateSetError(
                        f"layer {layer.name!r} overrides block(s) "
                        f"{sorted(contribution.block_paths)} of template {name!r}, but no "
                        "less specific layer provides a whole (base) file for it"
                    )
                source = f'{{% extends "{parent_virtual}" %}}\n{contribution.source}'

            virtual_name = f"{_VIRTUAL_PREFIX}{index}/{name}"
            self._loader.add(virtual_name, source)
            index += 1
            parent_virtual = virtual_name
            chain_layer_names.append(layer.name)

            for block_name, path in contribution.block_paths.items():
                block_sources[block_name] = BlockSource(
                    block=block_name, layer=layer.name, path=path
                )

        if parent_virtual is None:
            raise TemplateNotFoundError(name)

        # Publish the final, most specific virtual template under the public name too.
        self._loader.add(name, f'{{% extends "{parent_virtual}" %}}\n')

        result = (chain_layer_names, block_sources)
        self._chains[name] = result
        return result

    # -- public API ------------------------------------------------------------------

    def render(self, name: str, context: Mapping[str, object] | None = None) -> str:
        """Render template `name` against `context`. Deterministic, no implicit state."""
        chain_layers, _ = self._build_chain(name)
        template = self._env.get_template(name)
        try:
            return template.render(dict(context) if context is not None else {})
        except UndefinedError as exc:
            raise TemplateRenderError(name, chain_layers, exc) from exc

    def explain(self, name: str) -> list[tuple[str, str, Path | None]]:
        """Which layer (and file, if any) each block of `name` was resolved from.

        Returns a list of `(block_name, layer_name, path)`, sorted by block name.
        """
        _, block_sources = self._build_chain(name)
        return sorted((b.block, b.layer, b.path) for b in block_sources.values())
