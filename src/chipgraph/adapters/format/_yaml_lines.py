"""YAML loading that remembers the source line of every mapping key and list item."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

type YamlPath = tuple[str | int, ...]


class LinedYaml:
    """A parsed YAML document plus the 1-based line of every node, by path.

    `data` is what `yaml.safe_load` returns. `line(("memory_map", 3, "base"))` is the line
    of that node; a path that is not found falls back to its longest known prefix, so an
    error about a missing field still points at the item that lacks it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        text = path.read_text(encoding="utf-8")
        self.data: Any = yaml.safe_load(text)
        self._lines: dict[YamlPath, int] = {}
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if root is not None:
            self._index(root, ())

    def _index(self, node: yaml.Node, at: YamlPath) -> None:
        self._lines[at] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = str(key_node.value)
                self._lines[(*at, key)] = key_node.start_mark.line + 1
                self._index(value_node, (*at, key))
                # A key's own line, not its value's (which may start on the next line).
                self._lines[(*at, key)] = key_node.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                self._index(item, (*at, i))

    def line(self, path: Sequence[str | int]) -> int:
        """The line of `path`, or of its longest known prefix (1 for the document)."""
        at = tuple(path)
        while at:
            if at in self._lines:
                return self._lines[at]
            at = at[:-1]
        return self._lines.get((), 1)


__all__ = ["LinedYaml", "YamlPath"]
