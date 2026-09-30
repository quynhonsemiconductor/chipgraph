"""`DuplicateCheck`: no two module declarations share a name, no two IP files are copies.

A repo-wide, file-reading check (DESIGN.md 7.3 `duplicate`, 4.8 layer 1):

- `duplicate.module`: two `module` declarations with the same name in different files,
  found with the pyslang syntax tree (reusing `_naming_pyslang`);
- `duplicate.file`: two byte-identical RTL files at different paths ("two copies of the
  same IP").

It reads files, not the Design Model; the idempotency key is over the files it read.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_list_of_str,
    sha256_bytes,
)
from chipgraph.checks._naming_pyslang import declared_identifiers, parse_file
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_SCOPE = ("**/*.sv", "**/*.v")
_DEFAULT_IGNORE = ("**/vendor/**", "vendor/**")


class DuplicateCheck:
    """Checks for duplicate module names and byte-identical RTL files."""

    id = "duplicate"
    name = "Duplicate"
    per_block = False  # chip-wide: `chipgraph check` runs it once, not per block

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        try:
            scope = optional_list_of_str(spec.args, "scope") or list(_DEFAULT_SCOPE)
            ignore = optional_list_of_str(spec.args, "ignore") or list(_DEFAULT_IGNORE)
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        scope_regexes = [glob_to_regex(p) for p in scope]
        ignore_regexes = [glob_to_regex(p) for p in ignore]
        in_scope = [
            f
            for f in iter_repo_files(ctx.repo_root)
            if any(r.fullmatch(f) for r in scope_regexes)
            and not any(r.fullmatch(f) for r in ignore_regexes)
        ]

        issues: list[Issue] = []
        file_hashes: list[tuple[str, str]] = []
        modules: dict[str, list[tuple[str, int]]] = defaultdict(list)
        by_content: dict[str, list[str]] = defaultdict(list)

        for rel in in_scope:
            path = ctx.repo_root / rel
            try:
                data = path.read_bytes()
            except OSError:
                continue
            file_hashes.append((rel, sha256_bytes(data)))
            by_content[sha256_bytes(data)].append(rel)
            for name, line in _module_decls(path):
                modules[name].append((rel, line))

        issues.extend(_duplicate_modules(modules))
        issues.extend(_duplicate_files(by_content))

        ordered = sorted(issues, key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))
        key = compute_idempotency_key(spec.id, spec.args, file_hashes)
        status: CheckStatus = "fail" if ordered else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(ordered),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


def _module_decls(path: Path) -> list[tuple[str, int]]:
    """The (name, line) of every `module` declared in `path` (empty on a parse error)."""
    try:
        tree = parse_file(path)
    except OSError:
        return []
    return [(d.name, d.line) for d in declared_identifiers(tree) if d.kind == "module"]


def _duplicate_modules(modules: dict[str, list[tuple[str, int]]]) -> list[Issue]:
    issues: list[Issue] = []
    for name, sites in sorted(modules.items()):
        # Only a name declared in more than one distinct file is a duplicate; the same
        # file declaring a name twice is a different (syntax) problem.
        distinct_files = sorted({rel for rel, _ in sites})
        if len(distinct_files) < 2:
            continue
        first = min(sites)  # (file, line) of the first declaration
        for rel, line in sorted(sites):
            if (rel, line) == first:
                continue
            issues.append(
                Issue(
                    file=rel,
                    line=line,
                    rule="duplicate.module",
                    severity="error",
                    msg=(
                        f"module {name!r} is also declared at {first[0]}:{first[1]}; "
                        "a module name must be unique across the repo"
                    ),
                )
            )
    return issues


def _duplicate_files(by_content: dict[str, list[str]]) -> list[Issue]:
    issues: list[Issue] = []
    for _digest, paths in by_content.items():
        if len(paths) < 2:
            continue
        ordered = sorted(paths)
        first = ordered[0]
        for rel in ordered[1:]:
            issues.append(
                Issue(
                    file=rel,
                    line=None,
                    rule="duplicate.file",
                    severity="error",
                    msg=(
                        f"{rel} is byte-identical to {first}; "
                        "two copies of the same RTL at different paths"
                    ),
                )
            )
    return issues


__all__ = ["DuplicateCheck"]
