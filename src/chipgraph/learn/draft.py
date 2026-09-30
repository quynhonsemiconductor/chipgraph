"""Turn a `LearnResult` into a draft `.chipgraph.yml` profile and a naming-rules file.

The draft is what `chipgraph learn --out` writes and what `chipgraph try` / `chipgraph
init --from-learn` consume. It is deliberately small: the layout templates, a `naming`
adapter pointing at a written rules file, a `layout` and a `filelist` adapter, the block
list, and a `paths:` exception per vendored directory. A human reviews it before use
(DESIGN.md 8.6 V1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from chipgraph.checks._naming_rules import KindRule, NamingRules
from chipgraph.learn.models import LearnResult

DRAFT_PROFILE_NAME = "chipgraph.draft.yml"
"""File name `learn --out` writes the draft profile as (never `.chipgraph.yml`)."""

DRAFT_NAMING_RULES_NAME = "chipgraph.draft.naming.yml"
"""File name `learn --out` writes the learned naming rules as."""

_NAMING_MESSAGES = {
    "module": "does not match the learned module pattern",
    "port": "does not match the learned port pattern",
    "parameter": "does not match the learned parameter pattern",
    "localparam": "does not match the learned localparam pattern",
    "enum_value": "does not match the learned enum-value pattern",
    "instance": "does not match the learned instance pattern",
    "signal": "does not match the learned signal pattern",
    "memory": "does not match the learned memory pattern",
    "genvar": "does not match the learned genvar pattern",
}


def build_naming_rules(result: LearnResult) -> NamingRules | None:
    """The `NamingRules` model for the emitted (threshold-meeting) naming patterns.

    Returns `None` when no pattern met the threshold, so no rules file is written and the
    draft omits the `naming` adapter entirely (an empty rule file would reject everything).
    """
    emitted = result.emitted_naming
    if not emitted:
        return None
    identifiers = {
        rule.kind: KindRule(
            rule=f"learned.{rule.kind}",
            pattern=rule.pattern,
            message=_NAMING_MESSAGES.get(rule.kind, "does not match the learned pattern"),
        )
        for rule in emitted
    }
    return NamingRules(
        document=result.naming_rules_document,
        version="draft",
        identifiers=identifiers,
    )


def build_profile_dict(result: LearnResult, *, naming_rules_ref: str | None) -> dict[str, Any]:
    """The draft profile as a plain dict, ready to dump as YAML.

    `naming_rules_ref` is the profile-relative path the naming-rules file will live at
    (e.g. `chipgraph.draft.naming.yml` next to the draft, or `.chipgraph/naming.yml` for
    `init --from-learn`); `None` when no naming rule met the threshold.
    """
    layout = {rule.kind: rule.template for rule in result.layout}
    adapters: dict[str, Any] = {}

    if layout:
        scope = sorted({_scope_glob(rule.template) for rule in result.layout})
        adapters["layout"] = {"use": "layout", "templates": layout, "scope": scope}

    filelist_tmpl = layout.get("filelist")
    rtl_tmpl = layout.get("rtl")
    # The `filelist` check resolves listed paths relative to the filelist's own directory,
    # so it only fits repos with that style. A root-relative repo (e.g. tinysoc, whose
    # Makefile runs Verilator from the root) would fail every entry; the style is reported
    # as an observation instead, and no `filelist` adapter is emitted.
    if filelist_tmpl is not None and rtl_tmpl is not None and result.filelist_style == "filelist":
        adapters["filelist"] = {
            "use": "filelist",
            "filelist": filelist_tmpl,
            "sources": rtl_tmpl,
        }

    if naming_rules_ref is not None and rtl_tmpl is not None:
        adapters["naming"] = {
            "use": "naming",
            "rules": f"path:{naming_rules_ref}",
            "scope": [rtl_tmpl],
        }

    profile: dict[str, Any] = {
        "schema_version": 1,
        "project": result.project,
        "packs": [],
        "layout": layout,
        "adapters": adapters,
        "blocks": {block: {} for block in result.blocks},
    }

    paths = {
        glob: {"checks": {"naming": "off", "layout": "off"}, "reason": "vendored, learned"}
        for glob in result.vendor_paths
    }
    if paths:
        profile["paths"] = paths

    return profile


def _scope_glob(template: str) -> str:
    """A scope glob for a layout template: the placeholders become `*`.

    The `layout` check scopes which files it inspects; a scope that mirrors the template
    keeps the check from complaining about unrelated files (READMEs, scripts).
    """
    return template.replace("{block}", "*").replace("{BLOCK}", "*")


def dump_profile_yaml(result: LearnResult, *, naming_rules_ref: str | None) -> str:
    """The draft profile rendered as a YAML document string."""
    header = (
        "# chipgraph draft profile inferred by `chipgraph learn` (DESIGN.md 8.6 V1).\n"
        "# Review before use: each rule was inferred from the repo with a coverage figure.\n"
        # The directory name only: this file is committed, so no machine-local path.
        f"# Learned from: {Path(result.root).name}/\n"
    )
    body = yaml.safe_dump(
        build_profile_dict(result, naming_rules_ref=naming_rules_ref),
        sort_keys=True,
        default_flow_style=False,
    )
    return header + body


def dump_naming_rules_yaml(rules: NamingRules) -> str:
    """The learned naming rules rendered as a YAML document string."""
    header = "# chipgraph naming rules inferred by `chipgraph learn`. Review before use.\n"
    body = yaml.safe_dump(rules.model_dump(mode="json"), sort_keys=True, default_flow_style=False)
    return header + body


def write_draft(result: LearnResult, out_dir: Path) -> tuple[Path, Path | None]:
    """Write the draft profile (and naming-rules file) into `out_dir`.

    Returns `(profile_path, naming_rules_path_or_None)`. `out_dir` is created if missing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = build_naming_rules(result)
    naming_path: Path | None = None
    naming_ref: str | None = None
    if rules is not None:
        naming_path = out_dir / DRAFT_NAMING_RULES_NAME
        naming_path.write_text(dump_naming_rules_yaml(rules), encoding="utf-8")
        naming_ref = DRAFT_NAMING_RULES_NAME
    profile_path = out_dir / DRAFT_PROFILE_NAME
    profile_path.write_text(
        dump_profile_yaml(result, naming_rules_ref=naming_ref), encoding="utf-8"
    )
    return profile_path, naming_path


__all__ = [
    "DRAFT_NAMING_RULES_NAME",
    "DRAFT_PROFILE_NAME",
    "build_naming_rules",
    "build_profile_dict",
    "dump_naming_rules_yaml",
    "dump_profile_yaml",
    "write_draft",
]
