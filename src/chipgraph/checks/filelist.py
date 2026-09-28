"""`FilelistCheck`: every RTL file is listed, and every listed file exists.

See DESIGN.md 7.3 for the `filelist` check.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chipgraph.checks._common import (
    ArgError,
    compile_template,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_list_of_str,
    optional_str,
    require_str,
    sha256_file,
    to_repo_rel,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

_ENV_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")


class FilelistCheck:
    """Checks a project's per-block RTL filelists against the RTL files actually on disk."""

    id = "filelist"
    name = "Filelist"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            filelist_tmpl_str = require_str(args, "filelist")
            sources_tmpl_str = require_str(args, "sources")
            package_suffix = optional_str(args, "package_suffix", "_pkg.sv")
            ignore = optional_list_of_str(args, "ignore")
            filelist_tmpl = compile_template(filelist_tmpl_str)
            sources_tmpl = compile_template(sources_tmpl_str)
            if set(filelist_tmpl.names) != {"block"}:
                raise ArgError("args['filelist'] must use exactly the `{block}` placeholder")
            if set(sources_tmpl.names) != {"block"}:
                raise ArgError("args['sources'] must use exactly the `{block}` placeholder")
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        ignore_regexes = [glob_to_regex(p) for p in ignore]
        all_files = [
            f
            for f in iter_repo_files(ctx.repo_root)
            if not any(r.fullmatch(f) for r in ignore_regexes)
        ]

        blocks: set[str] = set()
        sources_by_block: dict[str, list[str]] = {}
        filelist_path_by_block: dict[str, str] = {}
        for f in all_files:
            src_match = sources_tmpl.match(f)
            if src_match is not None:
                block = src_match["block"]
                blocks.add(block)
                sources_by_block.setdefault(block, []).append(f)
            fl_match = filelist_tmpl.match(f)
            if fl_match is not None:
                block = fl_match["block"]
                blocks.add(block)
                filelist_path_by_block[block] = f

        issues: list[Issue] = []
        read_files: dict[str, str] = {}

        for block in sorted(blocks):
            expected_filelist = filelist_tmpl_str.replace("{block}", block)
            block_sources = sorted(sources_by_block.get(block, []))
            for f in block_sources:
                read_files[f] = sha256_file(ctx.repo_root / f)

            if block not in filelist_path_by_block:
                if block_sources:
                    issues.append(
                        Issue(
                            file=expected_filelist,
                            rule="filelist",
                            severity="error",
                            msg=f"block `{block}` has RTL sources but no filelist",
                        )
                    )
                continue

            visited: set[Path] = set()
            entries, parse_issues = _parse_filelist(
                ctx.repo_root / expected_filelist, ctx.repo_root, ctx.env, visited
            )
            issues.extend(parse_issues)
            for visited_path in visited:
                rel = to_repo_rel(visited_path, ctx.repo_root)
                if rel is not None:
                    read_files[rel] = sha256_file(visited_path)

            listed_issues, listed_repo_rel = _check_entries(entries, package_suffix, ctx.repo_root)
            issues.extend(listed_issues)

            for f in block_sources:
                if f not in listed_repo_rel:
                    issues.append(
                        Issue(
                            file=f,
                            rule="filelist",
                            severity="error",
                            msg=f"`{f}` is not in `{expected_filelist}`",
                        )
                    )

        key = compute_idempotency_key(spec.id, args, tuple(read_files.items()))
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


@dataclass(frozen=True)
class _Entry:
    raw: str
    line: int
    filelist_rel: str
    resolved: Path
    is_absolute: bool


def _expand_env(text: str, env: Mapping[str, str]) -> str | None:
    """Expand `$VAR`/`${VAR}` in `text` from `env`; `None` if any variable is unknown."""
    ok = True

    def _sub(m: re.Match[str]) -> str:
        nonlocal ok
        name = m.group(1) or m.group(2)
        if name in env:
            return env[name]
        ok = False
        return m.group(0)

    result = _ENV_VAR.sub(_sub, text)
    return result if ok else None


def _strip_comment(line: str) -> str:
    idx = len(line)
    for token in ("//", "#"):
        pos = line.find(token)
        if pos != -1:
            idx = min(idx, pos)
    return line[:idx]


def _parse_filelist(
    path: Path, root: Path, env: Mapping[str, str], visited: set[Path]
) -> tuple[list[_Entry], list[Issue]]:
    entries: list[_Entry] = []
    issues: list[Issue] = []
    _parse_filelist_into(path, root, env, visited, entries, issues)
    return entries, issues


def _parse_filelist_into(
    path: Path,
    root: Path,
    env: Mapping[str, str],
    visited: set[Path],
    entries: list[_Entry],
    issues: list[Issue],
) -> None:
    key = Path(os.path.normpath(str(path)))
    if key in visited:
        return
    visited.add(key)

    filelist_rel = to_repo_rel(path, root) or str(path)
    try:
        text = path.read_text()
    except OSError:
        return

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = _strip_comment(raw_line).strip()
        if not stripped:
            continue
        if stripped.startswith("+"):
            continue

        tokens = stripped.split(None, 1)
        head = tokens[0]

        if head in ("-f", "-F") and len(tokens) > 1:
            target_raw = tokens[1].strip()
            expanded = _expand_env(target_raw, env)
            if expanded is None:
                issues.append(
                    Issue(
                        file=filelist_rel,
                        line=lineno,
                        severity="warning",
                        msg=f"unknown environment variable in `{target_raw}`",
                    )
                )
                continue
            target_path = Path(expanded)
            if not target_path.is_absolute():
                target_path = path.parent / target_path
            _parse_filelist_into(target_path, root, env, visited, entries, issues)
            continue

        if head.startswith("-"):
            continue

        raw_entry = stripped
        expanded = _expand_env(raw_entry, env)
        if expanded is None:
            issues.append(
                Issue(
                    file=filelist_rel,
                    line=lineno,
                    severity="warning",
                    msg=f"unknown environment variable in `{raw_entry}`",
                )
            )
            continue

        entry_path = Path(expanded)
        is_absolute = entry_path.is_absolute()
        if is_absolute:
            issues.append(
                Issue(
                    file=filelist_rel,
                    line=lineno,
                    severity="warning",
                    msg=f"`{raw_entry}` is an absolute path",
                )
            )
            resolved = entry_path
        else:
            resolved = path.parent / entry_path

        entries.append(
            _Entry(
                raw=raw_entry,
                line=lineno,
                filelist_rel=filelist_rel,
                resolved=resolved,
                is_absolute=is_absolute,
            )
        )


def _check_entries(
    entries: list[_Entry], package_suffix: str, root: Path
) -> tuple[list[Issue], set[str]]:
    issues: list[Issue] = []
    seen: set[str] = set()
    listed_repo_rel: set[str] = set()
    seen_non_pkg_name: str | None = None

    for entry in entries:
        if not entry.resolved.is_file():
            issues.append(
                Issue(
                    file=entry.filelist_rel,
                    line=entry.line,
                    severity="error",
                    msg=f"`{entry.raw}` is listed but missing",
                )
            )
            continue

        key = os.path.normpath(str(entry.resolved))
        name = Path(key).name

        if key in seen:
            issues.append(
                Issue(
                    file=entry.filelist_rel,
                    line=entry.line,
                    severity="warning",
                    msg=f"`{entry.raw}` is listed twice",
                )
            )
        else:
            seen.add(key)
            rel = to_repo_rel(entry.resolved, root)
            if rel is not None:
                listed_repo_rel.add(rel)

        is_pkg = name.endswith(package_suffix)
        if is_pkg:
            if seen_non_pkg_name is not None:
                issues.append(
                    Issue(
                        file=entry.filelist_rel,
                        line=entry.line,
                        rule="order",
                        severity="error",
                        msg=f"package `{name}` must come before `{seen_non_pkg_name}`",
                    )
                )
        elif seen_non_pkg_name is None:
            seen_non_pkg_name = name

    return issues, listed_repo_rel
