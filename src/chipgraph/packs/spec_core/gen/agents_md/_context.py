"""Build the template context for the managed `AGENTS.md` block from a resolved profile.

Everything here is plain data (dicts, lists, strings, booleans), sorted where the profile
has no meaningful order, so the rendered block is deterministic. No path in the context
is made absolute: values are shown as the profile spells them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chipgraph.checks._naming_rules import NamingRules
from chipgraph.core.config import ConfigError, ResolvedProfile, resolve_data_ref
from chipgraph.core.config.models import AdapterCfg, PathRule
from chipgraph.packs.spec_core.gen.agents_md._markers import AgentsMdError

_WS = re.compile(r"\s+")


def _one_line(value: object) -> str:
    return _WS.sub(" ", str(value)).strip()


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return [str(value)]


def _command(value: object) -> str | None:
    if isinstance(value, str):
        return _one_line(value)
    if isinstance(value, list | tuple) and value:
        return " ".join(str(v) for v in value)
    return None


def _check_row(check_id: str, cfg: AdapterCfg) -> dict[str, Any]:
    extras = cfg.model_dump()
    severity = extras.get("severity")
    return {
        "id": check_id,
        "use": cfg.use,
        "severity": str(severity) if severity is not None else None,
        "command": _command(extras.get("cmd")) if cfg.use == "cmd" else None,
    }


def _path_row(glob: str, rule: PathRule, block: str | None) -> dict[str, Any]:
    return {
        "glob": glob,
        "block": block,
        "checks": [f"{name} {rule.checks[name]}" for name in sorted(rule.checks)],
        "write": rule.write,
        "reason": _one_line(rule.reason) if rule.reason else None,
    }


def _load_naming(ref: str, root: Path) -> dict[str, Any]:
    try:
        path = resolve_data_ref(ref, root)
    except ConfigError as exc:
        raise AgentsMdError(f"naming rules {ref!r}: {exc}") from exc
    try:
        data = yaml.safe_load(path.read_bytes())
    except (OSError, yaml.YAMLError) as exc:
        raise AgentsMdError(f"naming rules {ref!r}: cannot read: {exc}") from exc
    try:
        rules = NamingRules.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise AgentsMdError(f"naming rules {ref!r}: invalid: {loc}: {first['msg']}") from exc
    vocabulary = None
    if rules.vocabulary is not None:
        vocabulary = {
            "rule": rules.vocabulary.rule,
            "pairs": [[k, rules.vocabulary.replace[k]] for k in sorted(rules.vocabulary.replace)],
        }
    return {
        "ref": ref,
        "document": rules.document,
        "version": rules.version,
        "ignore_comment": rules.ignore_comment,
        # The rule file's own order (module, port, ...) is kept: it follows the source
        # document, and it is as deterministic as the file itself.
        "identifiers": [
            {
                "kind": kind,
                "pattern": rule.pattern,
                "rule": rule.rule,
                "message": _one_line(rule.message),
            }
            for kind, rule in rules.identifiers.items()
        ],
        "lexical": [{"rule": lex.rule, "message": _one_line(lex.message)} for lex in rules.lexical],
        "vocabulary": vocabulary,
    }


def _naming_refs(resolved: ResolvedProfile) -> list[str]:
    profile = resolved.profile
    refs: list[str] = []
    if profile.naming.rules:
        refs.append(profile.naming.rules)
    for check_id in sorted(profile.adapters):
        cfg = profile.adapters[check_id]
        rules = cfg.model_dump().get("rules")
        if cfg.use == "naming" and isinstance(rules, str) and rules not in refs:
            refs.append(rules)
    return refs


def _generated(resolved: ResolvedProfile) -> list[str]:
    profile = resolved.profile
    globs: set[str] = set(_as_list(profile.layout.get("generated")))
    for cfg in profile.adapters.values():
        extras = cfg.model_dump()
        if cfg.use == "generated":
            globs.update(_as_list(extras.get("files")))
        globs.update(_as_list(extras.get("generated")))
    return sorted(globs)


def _requirements(resolved: ResolvedProfile) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profile = resolved.profile
    req = profile.spec.requirements
    per_block: list[dict[str, Any]] = []
    for name in sorted(profile.blocks):
        if profile.blocks[name].spec.requirements is None:
            continue
        eff = resolved.for_block(name).spec.requirements
        per_block.append(
            {"block": name, "id_pattern": eff.id_pattern, "infer": eff.infer == "verification"}
        )
    uses_block = "{block}" in req.id_pattern or "{BLOCK}" in req.id_pattern
    overridden = {row["block"] for row in per_block}
    example = None
    if uses_block:
        candidates = [b for b in sorted(profile.blocks) if b not in overridden]
        if candidates:
            example = {"block": candidates[0], "pattern": req.id_regex(candidates[0]).pattern}
    trace = [cid for cid in sorted(profile.adapters) if profile.adapters[cid].use == "trace"]
    return (
        {
            "id_pattern": req.id_pattern,
            "uses_block": uses_block,
            "example": example,
            "infer": req.infer == "verification",
            "infer_heading": req.infer_heading,
            "trace_check": trace[0] if trace else None,
        },
        per_block,
    )


def build_context(resolved: ResolvedProfile, root: Path) -> dict[str, Any]:
    """The data the `agents_md.md.j2` template renders, from `resolved` and `root`.

    `root` is only used to resolve the naming rules reference (``org:``, ``preset:``,
    ``path:`` or relative), exactly as the `naming` check does.
    """
    profile = resolved.profile

    layout = [
        {"kind": kind, "templates": _as_list(profile.layout[kind])}
        for kind in sorted(profile.layout)
    ]
    block_layout = [
        {"block": name, "kind": kind, "templates": _as_list(override.layout[kind])}
        for name, override in sorted(profile.blocks.items())
        for kind in sorted(override.layout)
    ]
    all_templates = [t for row in (*layout, *block_layout) for t in row["templates"]]

    block_checks = [
        {"block": name, **_check_row(check_id, override.adapters[check_id])}
        for name, override in sorted(profile.blocks.items())
        for check_id in sorted(override.adapters)
    ]

    paths = [_path_row(glob, profile.paths[glob], None) for glob in sorted(profile.paths)]
    paths += [
        _path_row(glob, override.paths[glob], name)
        for name, override in sorted(profile.blocks.items())
        for glob in sorted(override.paths)
    ]

    checker = profile.naming.checker
    requirements, block_requirements = _requirements(resolved)
    chip = profile.spec.chip

    return {
        "project": {
            "name": profile.project,
            "extends": list(profile.extends),
            "packs": list(profile.packs),
            "target_kind": profile.target.kind,
            "pdk": profile.target.pdk,
            "chip_spec": {"path": chip.path, "format": chip.format} if chip else None,
            "ip_dir": profile.spec.ip_dir,
        },
        "blocks": [
            {"name": name, "instances": list(profile.blocks[name].instances)}
            for name in sorted(profile.blocks)
        ],
        "layout": layout,
        "block_layout": block_layout,
        "placeholders": [p for p in ("{block}", "{BLOCK}") if any(p in t for t in all_templates)],
        "naming": [_load_naming(ref, root) for ref in _naming_refs(resolved)],
        "naming_checker": (
            {"use": checker.use, "command": _command(checker.model_dump().get("cmd"))}
            if checker is not None
            else None
        ),
        "checks": [_check_row(cid, profile.adapters[cid]) for cid in sorted(profile.adapters)],
        "block_checks": block_checks,
        "requirements": requirements,
        "block_requirements": block_requirements,
        "paths": paths,
        "nda": (
            {"paths": sorted(set(profile.data.nda_paths)), "model": profile.data.nda_model}
            if profile.data.nda_paths
            else None
        ),
        "generated": _generated(resolved),
    }


__all__ = ["build_context"]
