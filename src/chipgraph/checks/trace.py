"""`TraceCheck`: every requirement must be referenced by at least one test (layer 5).

A layer-5 (process) cross check (DESIGN.md 4.8, DECISIONS D37). It reads the Design Model
`chipgraph ingest` wrote and the test files under the `tests` globs, and reports:

* `req.no_test` -- a *declared* requirement whose ID appears in no test file.
* `test.unknown_req` -- a test mentions a string that matches the profile's requirement
  `id_pattern` for some block but is not a requirement in the model (a stale or mistyped
  REQ-ID).
* `req.inferred_untraceable` -- an `info` per block with requirements that have no
  declared ID: *inferred* ones (D37) and Verification items *missing* their ID in a file
  that declares IDs (`spec_schema` reports each of those as `requirement.missing_id`).
  They have no stable ID to grep for, so instead of one failure each, one `info` says how
  many a block has, of which kind, and that they cannot be traced by ID.

A missing model is a whole-check `error` (run `chipgraph ingest` first). With a `block`
param (`chipgraph check --block`), only that block's requirements and its inferred summary
are considered; `test.unknown_req` still uses every block's pattern, so a test naming
another block's stale ID is still caught.

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  trace: { use: trace, tests: ["dv/**/*.py", "dv/**/*.sv"] }
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
    require_list_of_str,
    sha256_file,
)
from chipgraph.checks._model import ModelUnavailable, block_param, load_model
from chipgraph.core.config.models import RequirementsCfg
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext


class TraceCheck:
    """Checks that every declared requirement is referenced by at least one test."""

    id = "trace"
    name = "Trace"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            test_globs = require_list_of_str(args, "tests")
            if not test_globs:
                raise ArgError("args['tests'] must list at least one glob")
            id_pattern = args.get("id_pattern")
            if id_pattern is not None and not isinstance(id_pattern, str):
                raise ArgError("args['id_pattern'] must be a string")
        except ArgError as exc:
            return error_result(spec, str(exc), start)

        try:
            loaded = load_model(ctx.repo_root)
        except ModelUnavailable as exc:
            return error_result(spec, str(exc), start, rule="model")

        block = block_param(ctx)
        req_cfg = RequirementsCfg(id_pattern=id_pattern) if id_pattern else RequirementsCfg()

        test_files = _match_files(ctx.repo_root, test_globs)
        texts = {rel: (ctx.repo_root / rel).read_text(errors="ignore") for rel in test_files}

        issues = _trace_issues(loaded.model, texts, req_cfg, block)
        issues.sort(key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))

        file_hashes = [(rel, sha256_file(ctx.repo_root / rel)) for rel in test_files]
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


def _match_files(root: Path, globs: list[str]) -> list[str]:
    regexes = [glob_to_regex(g) for g in globs]
    return [f for f in iter_repo_files(root) if any(r.fullmatch(f) for r in regexes)]


def _trace_issues(
    model: DesignModel,
    texts: dict[str, str],
    req_cfg: RequirementsCfg,
    block: str | None,
) -> list[Issue]:
    issues: list[Issue] = []
    block_key = f"block:{block}" if block else None

    requirements = model.by_kind("requirement")
    declared = [
        r
        for r in requirements
        if r.attrs.get("id_source") == "declared"
        and (block_key is None or r.attrs.get("block") == block_key)
    ]
    # No declared ID: inferred (D37) or missing in a file that declares IDs.
    untraceable = [
        r
        for r in requirements
        if r.attrs.get("id_source") in ("inferred", "missing")
        and (block_key is None or r.attrs.get("block") == block_key)
    ]

    # req.no_test: a declared ID mentioned in no test (word-boundary search on its name).
    for req in sorted(declared, key=lambda r: r.key):
        pattern = re.compile(rf"(?<![\w-]){re.escape(req.name)}(?![\w-])")
        if not any(pattern.search(text) for text in texts.values()):
            issues.append(
                Issue(
                    file=req.source.file,
                    line=req.source.line,
                    rule="req.no_test",
                    severity="error",
                    msg=f"requirement {req.name!r} is referenced by no test under `tests`",
                )
            )

    # test.unknown_req: a test names a REQ-ID-shaped string that is no requirement.
    known_names = {r.name for r in requirements}
    id_regexes = _block_id_regexes(model, req_cfg)
    for rel in sorted(texts):
        for line_no, token in _req_like_tokens(texts[rel], id_regexes):
            if token not in known_names:
                issues.append(
                    Issue(
                        file=rel,
                        line=line_no,
                        rule="test.unknown_req",
                        severity="error",
                        msg=(
                            f"test names {token!r}, which matches a requirement id pattern "
                            "but is not a requirement in the model"
                        ),
                    )
                )

    # No declared ID: one info per block, not one failure per requirement (D37).
    by_block: dict[str, dict[str, int]] = {}
    for req in untraceable:
        b = str(req.attrs.get("block") or "")
        counts = by_block.setdefault(b, {"inferred": 0, "missing": 0})
        counts[str(req.attrs.get("id_source"))] += 1
    for b in sorted(by_block):
        issues.append(
            Issue(
                rule="req.inferred_untraceable",
                severity="info",
                msg=_untraceable_msg(b or "(no block)", by_block[b]),
            )
        )

    return issues


def _untraceable_msg(block: str, counts: dict[str, int]) -> str:
    """The `req.inferred_untraceable` message for one block's requirements with no ID."""
    inferred, missing = counts["inferred"], counts["missing"]
    if not missing:
        return (
            f"{inferred} inferred requirement(s) in {block} cannot be traced by ID "
            "(they are inferred, not declared REQ-IDs)"
        )
    parts = [f"{inferred} inferred"] if inferred else []
    parts.append(f"{missing} Verification item(s) missing an ID (requirement.missing_id)")
    return (
        f"{inferred + missing} requirement(s) in {block} cannot be traced by ID: "
        f"{', '.join(parts)}; none of them is a declared REQ-ID"
    )


def _block_id_regexes(model: DesignModel, req_cfg: RequirementsCfg) -> list[re.Pattern[str]]:
    """A requirement-id regex per block name in the model (plus a block-less default).

    The `id_pattern` may use `{block}`/`{BLOCK}`; compiled once per block so a test naming
    another block's stale ID is still recognised as REQ-ID-shaped.
    """
    uses_block = "{block}" in req_cfg.id_pattern or "{BLOCK}" in req_cfg.id_pattern
    if not uses_block:
        return [req_cfg.id_regex()]
    regexes: list[re.Pattern[str]] = []
    for blk in model.by_kind("block"):
        regexes.append(req_cfg.id_regex(blk.name))
    return regexes or [req_cfg.id_regex("x")]


_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def _req_like_tokens(text: str, id_regexes: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    """Every (line, token) in `text` whose token fully matches a requirement id regex."""
    out: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in _TOKEN.finditer(line):
            token = m.group(0)
            if any(rx.fullmatch(token) for rx in id_regexes):
                out.append((line_no, token))
    return out


__all__ = ["TraceCheck"]
