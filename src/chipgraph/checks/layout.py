"""`LayoutCheck`: every file in scope lives where the project's layout templates say.

See DESIGN.md 7.3 for the `layout` check and its `.chipgraph.yml` example.
"""

from __future__ import annotations

import time
from typing import Any

from chipgraph.checks._common import (
    ArgError,
    CompiledTemplate,
    compile_template,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    require_dict,
    require_list_of_str,
    sha256_file,
    str_or_list_of_str,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext


class LayoutCheck:
    """Checks that every in-scope file matches at least one declared layout template."""

    id = "layout"
    name = "Layout"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            templates_by_kind = _flatten_templates(require_dict(args, "templates"))
            scope = require_list_of_str(args, "scope")
            ignore = args.get("ignore", [])
            if not isinstance(ignore, list) or not all(isinstance(v, str) for v in ignore):
                raise ArgError("args['ignore'] must be a list of strings")
            values = _parse_values(args.get("values", {}))
            _check_no_duplicate_templates(templates_by_kind)
            compiled = [(kind, tmpl, compile_template(tmpl)) for kind, tmpl in templates_by_kind]
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        scope_regexes = [glob_to_regex(p) for p in scope]
        ignore_regexes = [glob_to_regex(p) for p in ignore]

        all_files = iter_repo_files(ctx.repo_root)
        in_scope = [
            f
            for f in all_files
            if any(r.fullmatch(f) for r in scope_regexes)
            and not any(r.fullmatch(f) for r in ignore_regexes)
        ]

        issues: list[Issue] = []
        for path in in_scope:
            issues.extend(_check_file(path, compiled, values))

        file_hashes = tuple((f, sha256_file(ctx.repo_root / f)) for f in in_scope)
        key = compute_idempotency_key(spec.id, args, file_hashes)
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


def _flatten_templates(templates: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for kind, val in templates.items():
        for tmpl in str_or_list_of_str(val, f"args['templates'][{kind!r}]"):
            out.append((kind, tmpl))
    if not out:
        raise ArgError("args['templates'] must declare at least one template")
    return out


def _check_no_duplicate_templates(templates_by_kind: list[tuple[str, str]]) -> None:
    seen: dict[str, str] = {}
    for kind, tmpl in templates_by_kind:
        if tmpl in seen:
            raise ArgError(f"templates {seen[tmpl]!r} and {kind!r} are identical: {tmpl!r}")
        seen[tmpl] = kind


def _parse_values(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ArgError("args['values'] must be a mapping")
    out: dict[str, list[str]] = {}
    for name, allowed in raw.items():
        out[name] = str_or_list_of_str(allowed, f"args['values'][{name!r}]")
    return out


def _check_file(
    path: str,
    compiled: list[tuple[str, str, CompiledTemplate]],
    values: dict[str, list[str]],
) -> list[Issue]:
    matches: list[tuple[str, dict[str, str]]] = []
    for kind, _tmpl, compiled_tmpl in compiled:
        groups = compiled_tmpl.match(path)
        if groups is not None:
            matches.append((kind, groups))

    if not matches:
        return [
            Issue(
                file=path,
                rule="layout",
                severity="error",
                msg=f"`{path}` is not in any layout template",
            )
        ]

    # A file may syntactically match more than one template (e.g. an overly broad and a
    # more specific one). It passes if at least one match has only allowed placeholder
    # values; otherwise we report the value violations of the first match.
    first_violations: list[Issue] | None = None
    for _kind, groups in matches:
        violations = _value_violations(path, groups, values)
        if not violations:
            return []
        if first_violations is None:
            first_violations = violations
    assert first_violations is not None
    return first_violations


def _value_violations(
    path: str, groups: dict[str, str], values: dict[str, list[str]]
) -> list[Issue]:
    issues: list[Issue] = []
    for name, val in groups.items():
        allowed = values.get(name)
        if allowed is not None and val not in allowed:
            issues.append(
                Issue(
                    file=path,
                    rule="layout",
                    severity="error",
                    msg=f"{name} `{val}` is not one of: {', '.join(allowed)}",
                )
            )
    return issues
