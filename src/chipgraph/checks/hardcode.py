"""`HardcodeCheck`: numbers the contract already names must not be typed by hand in RTL.

A layer-1 (structure) cross check (DESIGN.md 7.3). It reads the Design Model
`chipgraph ingest` wrote for the shared numbers a design must not duplicate, scans the
integer literals of the RTL files in scope (via the pyslang token stream, so numbers
inside comments and strings are never seen; sized literals like `32'h8000_4000` are
decoded), and reports each literal that equals a shared number.

Shared numbers come from the model:

* every memory region's base, size and end address (`base + size`) whose value is at
  least arg `min_value` (default `0x100`); and
* every interrupt line number, but only counted when the literal is used as an index on
  the arg `irq_vector` net (small line numbers are otherwise indistinguishable from
  ordinary constants, so they are not flagged elsewhere).

A `hardcode.literal` issue is *not* raised when the literal's line carries the arg
`allow_comment` marker (default `contract:`, matching QSoC's `// contract: <key>` tag) or
when the file is one of the generated packages listed in arg `generated`.

A missing model is a whole-check `error` (run `chipgraph ingest` first).

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  hardcode:
    use: hardcode
    scope: ["design/**/rtl/**/*.sv"]
    min_value: 0x100
    generated: ["design/common/rtl/qnsc_pkg.sv"]
```
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_list_of_str,
    optional_str,
    require_list_of_str,
    sha256_file,
)
from chipgraph.checks._model import ModelUnavailable, load_model
from chipgraph.checks._xref_syntax import parse_rtl
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_MIN_VALUE = 0x100
_DEFAULT_ALLOW_COMMENT = "contract:"


class HardcodeCheck:
    """Checks that no RTL literal duplicates a memory-map or interrupt number."""

    id = "hardcode"
    name = "Hardcode"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            scope = require_list_of_str(args, "scope")
            if not scope:
                raise ArgError("args['scope'] must list at least one glob")
            min_value = _int_arg(args, "min_value", _DEFAULT_MIN_VALUE)
            allow_comment = optional_str(args, "allow_comment", _DEFAULT_ALLOW_COMMENT)
            generated = set(optional_list_of_str(args, "generated"))
            ignore = optional_list_of_str(args, "ignore")
            irq_vector = optional_str(args, "irq_vector", "")
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        try:
            loaded = load_model(ctx.repo_root)
        except ModelUnavailable as exc:
            return error_result(spec, str(exc), start, rule="model")

        shared = _shared_numbers(loaded.model, min_value)
        irq_lines = _interrupt_lines(loaded.model)

        scope_regexes = [glob_to_regex(p) for p in scope]
        ignore_regexes = [glob_to_regex(p) for p in ignore]
        in_scope = [
            f
            for f in iter_repo_files(ctx.repo_root)
            if any(r.fullmatch(f) for r in scope_regexes)
            and not any(r.fullmatch(f) for r in ignore_regexes)
        ]

        issues: list[Issue] = []
        for rel in in_scope:
            if rel in generated:
                continue
            issues.extend(
                _file_issues(ctx.repo_root / rel, rel, shared, irq_lines, allow_comment, irq_vector)
            )
        issues.sort(key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))

        file_hashes = [(f, sha256_file(ctx.repo_root / f)) for f in in_scope]
        file_hashes.append(("<model>", loaded.build_inputs_hash or "none"))
        key = compute_idempotency_key(spec.id, args, file_hashes)
        status: CheckStatus = "fail" if any(i.severity == "error" for i in issues) else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


def _int_arg(args: object, key: str, default: int) -> int:
    from collections.abc import Mapping

    if not isinstance(args, Mapping) or key not in args:
        return default
    val = args[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ArgError(f"args[{key!r}] must be an integer")
    return val


def _shared_numbers(model: DesignModel, min_value: int) -> dict[int, list[str]]:
    """Map each shared memory-map number (>= `min_value`) to the model keys naming it."""
    out: dict[int, list[str]] = {}

    def add(value: object, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            return
        if value < min_value:
            return
        out.setdefault(value, [])
        if label not in out[value]:
            out[value].append(label)

    for region in model.by_kind("memory_region"):
        base = getattr(region, "base", None)
        size = getattr(region, "size", None)
        add(base, f"{region.key}.base")
        add(size, f"{region.key}.size")
        if isinstance(base, int) and isinstance(size, int):
            add(base + size, f"{region.key}.end")
    return out


def _interrupt_lines(model: DesignModel) -> dict[int, list[str]]:
    """Map each interrupt line number to the interrupt keys that use it."""
    out: dict[int, list[str]] = {}
    for irq in model.by_kind("interrupt"):
        line = getattr(irq, "line", None)
        if isinstance(line, int) and not isinstance(line, bool):
            out.setdefault(line, [])
            if irq.key not in out[line]:
                out[line].append(irq.key)
    return out


def _file_issues(
    path: Path,
    rel: str,
    shared: dict[int, list[str]],
    irq_lines: dict[int, list[str]],
    allow_comment: str,
    irq_vector: str,
) -> list[Issue]:
    parsed = parse_rtl(path)
    issues: list[Issue] = [
        Issue(
            file=rel, line=pf.line, rule="parse", severity="error", msg=f"parse error: {pf.message}"
        )
        for pf in parsed.parse_errors
    ]
    text_lines = path.read_text(errors="ignore").splitlines()
    index_re = re.compile(rf"{re.escape(irq_vector)}\s*\[\s*(\d+)\s*\]") if irq_vector else None

    for lit in parsed.literals:
        if _has_marker(parsed.line_comments.get(lit.line, ""), allow_comment):
            continue
        keys = shared.get(lit.value)
        if keys:
            issues.append(
                Issue(
                    file=rel,
                    line=lit.line,
                    rule="hardcode.literal",
                    severity="error",
                    msg=(
                        f"literal {lit.raw!r} (= {lit.value}) duplicates the contract number "
                        f"{', '.join(sorted(keys))}; take it from the contract, "
                        f"or tag the line `// {allow_comment} <key>`"
                    ),
                )
            )
            continue
        # An interrupt line number only counts as a shared number when used as an index
        # on the interrupt vector net.
        if index_re is not None and lit.value in irq_lines:
            line_text = text_lines[lit.line - 1] if 0 < lit.line <= len(text_lines) else ""
            if any(int(m.group(1)) == lit.value for m in index_re.finditer(line_text)):
                keys = irq_lines[lit.value]
                issues.append(
                    Issue(
                        file=rel,
                        line=lit.line,
                        rule="hardcode.literal",
                        severity="error",
                        msg=(
                            f"literal {lit.raw!r} indexes {irq_vector!r} at bit {lit.value}, the "
                            f"contract line of {', '.join(sorted(keys))}; take it from the contract"
                        ),
                    )
                )
    return issues


def _has_marker(comment: str, allow_comment: str) -> bool:
    return allow_comment in comment


__all__ = ["HardcodeCheck"]
