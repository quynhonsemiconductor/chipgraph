"""`GeneratedCheck`: generated files are not edited by hand.

Also exports `stamp`/`verify`, the two functions generators use to mark a file as
generated and later confirm it has not been hand-edited. See DESIGN.md 7.3.
"""

from __future__ import annotations

import hashlib
import re
import time

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_bool,
    require_list_of_str,
    sha256_file,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

_MARKER_SEARCH_LINES = 20
_COMMENT_CLOSERS = {"<!--": " -->"}

# A marker line: an optional comment prefix, then `chipgraph:generated sha256=<64 hex>`,
# then an optional closer (for `<!--`-style comments).
_MARKER_RE = re.compile(
    r"^\s*(?://|#|--|;|<!--)?\s*chipgraph:generated sha256=([0-9a-f]{64})\s*(?:-->)?\s*$"
)


def stamp(text: str, comment_prefix: str = "//") -> str:
    """Insert or replace the generation marker as the first line of `text`.

    The marker's hash covers `text` with any existing marker's first line removed, so
    calling `stamp` again on already-stamped, unedited text is a no-op (byte for byte).
    """
    lines = text.splitlines(keepends=True)
    has_marker = bool(lines) and _MARKER_RE.match(_strip_eol(lines[0])) is not None
    base_text = "".join(lines[1:]) if has_marker else text

    digest = hashlib.sha256(base_text.encode("utf-8")).hexdigest()
    closer = _COMMENT_CLOSERS.get(comment_prefix, "")
    eol = _eol_of(lines[0]) if lines else "\n"
    marker_line = f"{comment_prefix} chipgraph:generated sha256={digest}{closer}{eol}"
    return marker_line + base_text


def verify(text: str) -> bool | None:
    """Check `text`'s generation marker against its content.

    Returns `True` if the marker matches, `False` if it does not (hand-edited), or
    `None` if no marker is found in the first 20 lines.
    """
    lines = text.splitlines(keepends=True)
    limit = min(len(lines), _MARKER_SEARCH_LINES)
    for i in range(limit):
        m = _MARKER_RE.match(_strip_eol(lines[i]))
        if m is None:
            continue
        stored = m.group(1)
        remainder = "".join(lines[:i] + lines[i + 1 :])
        actual = hashlib.sha256(remainder.encode("utf-8")).hexdigest()
        return actual == stored
    return None


def _strip_eol(line: str) -> str:
    return line.rstrip("\n").rstrip("\r")


def _eol_of(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


class GeneratedCheck:
    """Checks that generated files still match the marker hash they were stamped with."""

    id = "generated"
    name = "Generated"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            globs = require_list_of_str(args, "files")
            require_marker = optional_bool(args, "require_marker", False)
            regexes = [glob_to_regex(g) for g in globs]
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        all_files = iter_repo_files(ctx.repo_root)
        matched = [f for f in all_files if any(r.fullmatch(f) for r in regexes)]

        issues: list[Issue] = []
        read_files: dict[str, str] = {}
        for f in matched:
            full_path = ctx.repo_root / f
            read_files[f] = sha256_file(full_path)
            try:
                text = full_path.read_text()
            except (OSError, UnicodeDecodeError) as exc:
                issues.append(
                    Issue(
                        file=f,
                        rule="generated",
                        severity="error",
                        msg=f"could not read `{f}`: {exc}",
                    )
                )
                continue

            result = verify(text)
            if result is False:
                issues.append(
                    Issue(
                        file=f,
                        rule="generated",
                        severity="error",
                        msg=f"`{f}` was edited by hand after it was generated",
                    )
                )
            elif result is None:
                issues.append(
                    Issue(
                        file=f,
                        rule="generated",
                        severity="error" if require_marker else "warning",
                        msg=f"`{f}` has no generation marker",
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
