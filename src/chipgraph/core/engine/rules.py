"""Loading `RuleSpec` from rule files and packs.

Pure data loading: this module only turns YAML into validated `RuleSpec` objects. It
does not expand `foreach`, resolve inputs/outputs, or build a graph (see `graph.py`).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from chipgraph.core.contracts import RuleSpec
from chipgraph.core.plugin_api.pack import Pack


class RuleLoadError(Exception):
    """Raised when a rule file, or a pack's set of rules, fails to load."""


def load_rule_file(path: Path, *, namespace: str | None = None) -> RuleSpec:
    """Load a single `RuleSpec` from a YAML file at `path`.

    The id key may be written `rule:` (as in DESIGN.md 3.4) or `id:`; having both is
    an error. With `namespace` (the pack name), a short id such as `rtl_module` becomes
    `<namespace>/rtl_module`. Any other validation failure (bad YAML, schema mismatch) raises
    `RuleLoadError` naming `path`.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise RuleLoadError(f"cannot read rule file {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleLoadError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuleLoadError(f"rule file {path} must contain a YAML mapping")

    data = dict(raw)
    if "rule" in data and "id" in data:
        raise RuleLoadError(f"rule file {path} has both 'rule' and 'id' keys; use only one")
    if "rule" in data:
        data["id"] = data.pop("rule")
    if namespace is not None and isinstance(data.get("id"), str) and "/" not in data["id"]:
        data["id"] = f"{namespace}/{data['id']}"

    try:
        return RuleSpec.model_validate(data)
    except ValidationError as exc:
        raise RuleLoadError(f"invalid rule spec in {path}: {exc}") from exc


def _rule_files_under(path: Path) -> list[Path]:
    """Return the rule files a `provides.rules` entry resolves to.

    A file resolves to itself; a directory resolves to every `*.yml`/`*.yaml` file
    directly inside it, sorted for determinism.
    """
    if path.is_dir():
        return sorted({*path.glob("*.yml"), *path.glob("*.yaml")})
    return [path]


def load_pack_rules(pack: Pack) -> list[RuleSpec]:
    """Load every rule a pack provides.

    Every path in `pack.manifest.provides.rules` is loaded (a file directly, or every
    `*.yml`/`*.yaml` file inside a directory). Each rule's id namespace (the part
    before `/`) must equal the pack's name, and rule ids must be unique across all of
    the pack's rule files; either violation raises `RuleLoadError`.
    """
    specs: dict[str, RuleSpec] = {}
    for rel in pack.manifest.provides.rules:
        for file_path in _rule_files_under(pack.path(rel)):
            spec = load_rule_file(file_path, namespace=pack.manifest.name)
            namespace = spec.id.split("/", 1)[0]
            if namespace != pack.manifest.name:
                raise RuleLoadError(
                    f"rule {spec.id!r} in {file_path} does not belong to namespace "
                    f"{pack.manifest.name!r}"
                )
            if spec.id in specs:
                raise RuleLoadError(
                    f"duplicate rule id {spec.id!r} in pack {pack.manifest.name!r} "
                    f"(also defined in {file_path})"
                )
            specs[spec.id] = spec
    return list(specs.values())
