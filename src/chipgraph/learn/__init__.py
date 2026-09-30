"""`chipgraph learn` / `chipgraph try`: infer a project's conventions (DESIGN.md 8.6 V1).

`learn(root)` reads an existing repository and returns a `LearnResult`: a draft profile
plus the evidence and coverage behind each inferred rule. `draft` turns that into a
`.chipgraph.yml` and a naming-rules file; `try_run` runs chipgraph read-only against a
repo; `init_from_learn` writes the reviewed draft into a project as its `.chipgraph.yml`.

This package sits outside `chipgraph.core`: it reasons about chips, tools and file
layouts, and imports checks/app freely.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from chipgraph.learn.draft import (
    DRAFT_NAMING_RULES_NAME,
    DRAFT_PROFILE_NAME,
    build_naming_rules,
    build_profile_dict,
    dump_naming_rules_yaml,
    dump_profile_yaml,
    write_draft,
)
from chipgraph.learn.infer import DEFAULT_THRESHOLD, learn
from chipgraph.learn.models import (
    Coverage,
    LayoutRule,
    LearnResult,
    NamingRule,
    Observation,
)
from chipgraph.learn.try_run import TryReport, try_run

_INIT_NAMING_RULES_REL = ".chipgraph/naming.yml"
"""Where `init --from-learn` writes the learned naming rules, referenced as `path:...`."""


class InitFromLearnError(Exception):
    """Raised when `init --from-learn` cannot write (e.g. `.chipgraph.yml` exists)."""


def init_from_learn(
    root: Path, *, threshold: float = DEFAULT_THRESHOLD
) -> tuple[Path, Path | None]:
    """Write `.chipgraph.yml` (and a naming-rules file) into `root` from a fresh learn.

    Refuses to overwrite an existing `.chipgraph.yml`. Returns `(profile_path,
    naming_rules_path_or_None)`.
    """
    profile_path = root / ".chipgraph.yml"
    if profile_path.is_file():
        raise InitFromLearnError(
            f"{profile_path} already exists; refusing to overwrite (remove it first)"
        )

    result = learn(root, threshold=threshold)
    rules = build_naming_rules(result)
    naming_path: Path | None = None
    naming_ref: str | None = None
    if rules is not None:
        naming_path = root / _INIT_NAMING_RULES_REL
        naming_path.parent.mkdir(parents=True, exist_ok=True)
        naming_path.write_text(dump_naming_rules_yaml(rules), encoding="utf-8")
        naming_ref = _INIT_NAMING_RULES_REL

    data = build_profile_dict(result, naming_rules_ref=naming_ref)
    header = (
        "# chipgraph project profile written by `chipgraph init --from-learn` (DESIGN.md 8.6).\n"
        "# Review it: each rule was inferred from the repo. Edit freely.\n"
    )
    profile_path.write_text(
        header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False), encoding="utf-8"
    )
    return profile_path, naming_path


__all__ = [
    "DEFAULT_THRESHOLD",
    "DRAFT_NAMING_RULES_NAME",
    "DRAFT_PROFILE_NAME",
    "Coverage",
    "InitFromLearnError",
    "LayoutRule",
    "LearnResult",
    "NamingRule",
    "Observation",
    "TryReport",
    "build_naming_rules",
    "build_profile_dict",
    "dump_naming_rules_yaml",
    "dump_profile_yaml",
    "init_from_learn",
    "learn",
    "try_run",
    "write_draft",
]
