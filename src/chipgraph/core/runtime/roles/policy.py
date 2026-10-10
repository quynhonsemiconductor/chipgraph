"""Applying a role's tool table to rules and tasks (DESIGN.md 5.1, 5.4).

- `model_ladder`: a task's first tier and escalation tier, from its role unless the rule
  sets its own.
- `check_agent_rules`: the rule validator for roles: an unknown role, a role agent rules
  may not use, a rule that breaks its role's write scope (a role that writes no files
  may only have the one output the engine writes from its reply, of a kind its
  `engine_writes` names), or a rule that gives a role an input of a kind it must not see.
- `denied_reads`: the repo-relative paths and globs a task's agent must not read, from
  its role's read policy resolved against the project (the build graph's artifacts and
  the profile's layout).
- `path_denied` / `covers_denied`: how a path is matched against those (the runtime's
  guard implements the same rules on its own).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase

from chipgraph.core.contracts import ArtifactRef, Budget, ModelTier, RuleInstance, RuleSpec
from chipgraph.core.runtime.roles.registry import find_role
from chipgraph.core.runtime.roles.spec import RoleError, RoleSpec

LayoutValue = str | tuple[str, ...]
"""A profile layout entry: one path template, or several (an empty tuple means none)."""

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_GLOB_CHARS = frozenset("*?[{")


def model_ladder(role: RoleSpec | None, budget: Budget) -> tuple[ModelTier, ModelTier | None]:
    """The task's `(tier, escalate)`: the rule's when its budget sets either, else the role's.

    The ladder is taken as a whole, so a rule that sets only `tier` does not escalate
    unless it says so. An unknown role (`None`) leaves the budget as it is.
    """
    if role is None or {"tier", "escalate"} & budget.model_fields_set:
        return budget.tier, budget.escalate
    return role.default_tier, role.escalate_to


def check_agent_rules(rules: Mapping[str, RuleSpec], instances: Iterable[RuleInstance]) -> None:
    """Raise `RoleError` listing every agent rule that breaks its role's tool table."""
    problems: list[str] = []
    for rule in sorted(rules.values(), key=lambda r: r.id):
        if rule.kind != "agent" or rule.role is None:
            continue
        role = find_role(rule.role)
        if role is None:
            problems.append(f"rule {rule.id!r}: unknown role {rule.role!r}")
        elif not role.dispatch:
            problems.append(
                f"rule {rule.id!r}: role {role.id!r} is not handed out as an agent task "
                f"({role.description})"
            )
        elif role.write_scope == "none" and not role.engine_writes:
            problems.append(
                f"rule {rule.id!r}: role {role.id!r} writes no files, so it cannot produce "
                "the rule's outputs"
            )
        elif role.write_scope == "none" and len(rule.outputs) != 1:
            problems.append(
                f"rule {rule.id!r}: role {role.id!r} writes no files; the engine writes "
                f"exactly one output from its reply, so the rule must have exactly one "
                f"output (it has {len(rule.outputs)})"
            )
        elif role.write_scope in ("plan", "proposal") and len(rule.outputs) != 1:
            problems.append(
                f"rule {rule.id!r}: role {role.id!r} writes only its {role.write_scope} "
                f"file, so the rule must have exactly one output (it has {len(rule.outputs)})"
            )
    seen: set[str] = set()
    for instance in sorted(instances, key=lambda i: i.instance_id):
        owner = rules.get(instance.rule_id)
        if owner is None or owner.kind != "agent" or owner.role is None:
            continue
        role = find_role(owner.role)
        if role is None:
            continue
        if role.dispatch and role.write_scope == "none" and role.engine_writes:
            for ref in instance.outputs:
                key = f"{owner.id}:out:{ref.kind}"
                if ref.kind not in role.engine_writes and key not in seen:
                    seen.add(key)
                    kinds = ", ".join(repr(k) for k in role.engine_writes)
                    problems.append(
                        f"rule {owner.id!r}: role {role.id!r} writes no files, and the engine "
                        f"writes only a {kinds} output from its reply, but output "
                        f"{ref.path!r} is a {ref.kind!r} artifact"
                    )
        if not role.read_policy.deny_kinds:
            continue
        for ref in instance.inputs:
            locator = ref.path or ref.model_key
            key = f"{owner.id}:{locator}"
            if ref.kind in role.read_policy.deny_kinds and key not in seen:
                seen.add(key)
                problems.append(
                    f"rule {owner.id!r}: role {role.id!r} must not see {ref.kind!r} "
                    f"artifacts, but input {locator!r} is one"
                )
    if problems:
        raise RoleError("; ".join(problems))


def _fill(template: str, block: str | None) -> str:
    """A layout template as a glob: `{block}`/`{BLOCK}` filled in, any other field `*`."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if block is not None and key == "block":
            return block
        if block is not None and key == "BLOCK":
            return block.upper()
        return "*"

    return _PLACEHOLDER_RE.sub(_replace, template)


def _templates(value: LayoutValue | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def _clean(path: str) -> str | None:
    """A repo-relative POSIX path or glob, normalised; `None` if it leaves the repo."""
    text = path.replace("\\", "/").strip()
    if not text or text.startswith("/"):
        return None
    norm = posixpath.normpath(text)
    if norm == "." or norm == ".." or norm.startswith("../"):
        return None
    return norm


def denied_reads(
    role: RoleSpec,
    *,
    artifacts: Iterable[ArtifactRef],
    layout: Mapping[str, LayoutValue],
    block_layouts: Mapping[str, Mapping[str, LayoutValue]] | None = None,
    keep: Iterable[str] = (),
) -> tuple[str, ...]:
    """The paths and globs a task of `role` must not read, sorted (empty for `any`).

    - every artifact of a denied kind among `artifacts` (the build graph's inputs and
      outputs), and the directory holding it (`<dir>/**`, never the project root);
    - the profile's `layout` templates for each denied kind, per block when
      `block_layouts` (block name to its layout overrides) is given, with `{block}`
      filled in and any other field as `*`;
    - the role's own `deny_globs`.

    Paths in `keep` (the task's own outputs) are never listed.
    """
    policy = role.read_policy
    if policy.mode == "any":
        return ()
    kinds = set(policy.deny_kinds)
    denied: set[str] = set()
    for ref in artifacts:
        if ref.path is None or ref.kind not in kinds:
            continue
        path = _clean(ref.path)
        if path is None:
            continue
        denied.add(path)
        parent = posixpath.dirname(path)
        if parent:
            denied.add(f"{parent}/**")
    blocks = dict(block_layouts or {})
    for kind in sorted(kinds):
        if blocks:
            for block, overrides in sorted(blocks.items()):
                value = overrides[kind] if kind in overrides else layout.get(kind)
                denied.update(_fill(t, block) for t in _templates(value))
        else:
            denied.update(_fill(t, None) for t in _templates(layout.get(kind)))
    denied.update(policy.deny_globs)
    cleaned = {c for c in (_clean(d) for d in denied) if c is not None}
    return tuple(sorted(cleaned - set(keep)))


def static_prefix(pattern: str) -> str:
    """The leading path segments of `pattern` that hold no glob character ('' for none)."""
    parts: list[str] = []
    for part in pattern.split("/"):
        if _GLOB_CHARS & set(part):
            break
        parts.append(part)
    return "/".join(parts)


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_CHARS & set(pattern))


def _within(path: str, base: str) -> bool:
    """`path` is `base` or below it ('' is the project root, above everything)."""
    return base == "" or path == base or path.startswith(base + "/")


def path_denied(path: str, denied: Iterable[str]) -> bool:
    """Whether reading the file `path` (repo-relative) is denied by one of `denied`.

    A literal entry denies itself and everything below it; a glob denies what it matches
    (`*` also crosses `/`, so `rtl/*.sv` covers `rtl/sub/x.sv`: the safe side).
    """
    for entry in denied:
        if _is_glob(entry):
            if fnmatchcase(path, entry):
                return True
        elif _within(path, entry):
            return True
    return False


def covers_denied(scope: str, denied: Iterable[str]) -> bool:
    """Whether a listing or search rooted at directory `scope` could reach a denied path.

    True when `scope` holds a denied path (it is an ancestor of one: `''`, the project
    root, holds them all), or lies inside one, or inside the static prefix of a glob.
    """
    for entry in denied:
        prefix = static_prefix(entry) if _is_glob(entry) else entry
        if _within(prefix, scope) or _within(scope, prefix):
            return True
        if _is_glob(entry) and fnmatchcase(scope, entry):
            return True
    return False


__all__ = [
    "LayoutValue",
    "check_agent_rules",
    "covers_denied",
    "denied_reads",
    "model_ladder",
    "path_denied",
    "static_prefix",
]
