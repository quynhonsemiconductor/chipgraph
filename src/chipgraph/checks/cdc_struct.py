"""`CdcStructCheck`: structural clock-domain-crossing check on the Design Model (layer 1).

A layer-1 (structure) check (DESIGN.md 4.8): a signal that crosses from one clock domain
to another must pass through a synchroniser cell declared in the profile (e.g.
`qnsc_sync`). A crossing that goes straight into a flop of another domain -- directly or
through plain combinational logic -- is an error.

The Design Model does not hold nets or connections (`adapters/tool/pyslang.py`), so this
check elaborates the block's RTL itself (`_cdc_elaborate`) and analyses clock domains and
their crossings structurally (`_cdc_analyze`). It is a per-block check: it runs on the RTL
of the block being checked. Issues cite the destination flop's file and line.

Rules:

* `cdc.unsynchronised` (error): a register samples another domain with no synchroniser.
* `cdc.unknown_domain` (info): a register samples an input whose clock domain is unknown.
* `cdc.not_analysed` (info): a path leaves what this structural check resolves.

Args:

* `sync_cells` (list of str, required): module names that are synchronisers.
* `scope` (list of glob str, optional): RTL files to analyse; `{block}` is substituted
  from `ctx.params`. Defaults to the files of the RTL modules the model attributes to the
  block, plus the files of every module they instantiate (so a synchroniser or child IP
  the block wires up is elaborated and recognised, not treated as an unknown blackbox).
* `clock_patterns` (list of str, optional): regexes that recognise a clock port by name
  (default `^i_clk`, `^clk`).
* `ignore` (list of glob str, optional): files to drop (default: any `vendor` path).

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  cdc_struct:
    use: cdc_struct
    sync_cells: [qnsc_sync]
```
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from chipgraph.checks._cdc_analyze import Crossing, Kind, analyse
from chipgraph.checks._cdc_elaborate import elaborate
from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_list_of_str,
    require_list_of_str,
    sha256_file,
)
from chipgraph.checks._model import ModelUnavailable, block_param, load_model
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus, Severity
from chipgraph.core.model.entities import ModuleEntity
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_CLOCK_PATTERNS = (r"^i_clk", r"^clk")
_DEFAULT_IGNORE = ("**/vendor/**", "vendor/**")

_RULE_BY_KIND: dict[Kind, tuple[str, Severity]] = {
    Kind.UNSYNCHRONISED: ("cdc.unsynchronised", "error"),
    Kind.UNKNOWN_DOMAIN: ("cdc.unknown_domain", "info"),
    Kind.NOT_ANALYSED: ("cdc.not_analysed", "info"),
}


class CdcStructCheck:
    """Checks that every clock-domain crossing goes through a declared synchroniser."""

    id = "cdc_struct"
    name = "CDC (structural)"
    per_block = True  # a block's RTL is analysed at a time

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args
        block = block_param(ctx)
        try:
            sync_cells = frozenset(require_list_of_str(args, "sync_cells"))
            scope = [_substitute(g, block) for g in optional_list_of_str(args, "scope")]
            ignore = optional_list_of_str(args, "ignore") or list(_DEFAULT_IGNORE)
            clock_patterns = optional_list_of_str(args, "clock_patterns") or list(
                _DEFAULT_CLOCK_PATTERNS
            )
            clock_matchers = [re.compile(p) for p in clock_patterns]
        except (ArgError, re.error) as exc:
            return error_result(spec, str(exc), start)

        try:
            loaded = load_model(ctx.repo_root)
        except ModelUnavailable as exc:
            return error_result(spec, str(exc), start, rule="model")

        block_modules = _block_modules(loaded.model, block)

        scope_files = _scope_files(ctx.repo_root, scope, ignore, loaded.model, block_modules)
        if not scope_files:
            # Nothing to analyse (a block with no RTL yet): a clean pass, not an error.
            return _result(spec, [], scope_files, ctx.repo_root, loaded.build_inputs_hash, start)

        tops = {m.name for m in block_modules} or None
        elaboration = elaborate(
            (ctx.repo_root / f for f in scope_files),
            sync_cells=sync_cells,
            clock_matchers=clock_matchers,
            repo_root=ctx.repo_root,
            tops=tops,
        )

        issues: list[Issue] = [
            Issue(
                file=diag.file,
                line=diag.line,
                rule="parse",
                severity="error",
                msg=f"elaboration error: {diag.message}",
            )
            for diag in elaboration.diagnostics
        ]
        for module in sorted(elaboration.modules):
            for crossing in analyse(elaboration.modules[module]):
                issues.append(_issue(crossing))

        return _result(spec, issues, scope_files, ctx.repo_root, loaded.build_inputs_hash, start)


def _issue(crossing: Crossing) -> Issue:
    rule, severity = _RULE_BY_KIND[crossing.kind]
    return Issue(
        file=crossing.loc.file,
        line=crossing.loc.line,
        rule=rule,
        severity=severity,
        msg=crossing.detail,
    )


def _block_modules(model: DesignModel, block: str | None) -> list[ModuleEntity]:
    """The RTL modules the model attributes to `block` (all modules when no block)."""
    block_key = f"block:{block}" if block else None
    out: list[ModuleEntity] = []
    for entity in model.by_kind("module"):
        assert isinstance(entity, ModuleEntity)
        if block_key is None or entity.block == block_key:
            out.append(entity)
    return out


def _scope_files(
    repo_root: Path,
    scope: list[str],
    ignore: list[str],
    model: DesignModel,
    block_modules: list[ModuleEntity],
) -> list[str]:
    """The repo-relative RTL files to analyse, deterministically ordered.

    An explicit `scope` (globs, `{block}` already substituted) wins. Otherwise the files
    of the block's modules, *plus* the files of every module they instantiate (following
    `instantiates` transitively): so a synchroniser or child IP the block wires up is
    elaborated too and can be recognised, instead of being an unknown blackbox. `ignore`
    globs (vendor by default) are dropped either way, so a vendored cell is not analysed
    as a top of its own.
    """
    ignore_regexes = [glob_to_regex(p) for p in ignore]

    def kept(rel: str) -> bool:
        return not any(r.fullmatch(rel) for r in ignore_regexes)

    if scope:
        regexes = [glob_to_regex(g) for g in scope]
        return sorted(
            f
            for f in iter_repo_files(repo_root)
            if any(r.fullmatch(f) for r in regexes) and kept(f)
        )

    files = {m.file for m in _with_instantiated(model, block_modules) if m.file}
    return sorted(f for f in files if kept(f))


def _with_instantiated(model: DesignModel, block_modules: list[ModuleEntity]) -> list[ModuleEntity]:
    """`block_modules` plus every module reachable through `instantiates`, deduped."""
    by_key = {m.key: m for m in model.by_kind("module") if isinstance(m, ModuleEntity)}
    seen: dict[str, ModuleEntity] = {m.key: m for m in block_modules}
    frontier = list(seen)
    while frontier:
        src = frontier.pop()
        for rel in model.get_relations(src=src, kind="instantiates"):
            child = by_key.get(rel.dst)
            if child is not None and child.key not in seen:
                seen[child.key] = child
                frontier.append(child.key)
    return list(seen.values())


def _substitute(glob: str, block: str | None) -> str:
    if "{block}" not in glob:
        return glob
    if block is None:
        raise ArgError(f"scope glob {glob!r} uses {{block}} but no block is set in ctx.params")
    return glob.replace("{block}", block)


def _result(
    spec: CheckSpec,
    issues: list[Issue],
    scope_files: list[str],
    repo_root: Path,
    build_inputs_hash: str | None,
    start: float,
) -> CheckResult:
    ordered = sorted(issues, key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))
    file_hashes = [(f, sha256_file(repo_root / f)) for f in scope_files]
    file_hashes.append(("<model>", build_inputs_hash or "none"))
    key = compute_idempotency_key(spec.id, spec.args, file_hashes)
    status: CheckStatus = "fail" if any(i.severity == "error" for i in ordered) else "pass"
    return CheckResult(
        check_id=spec.id,
        status=status,
        issues=tuple(ordered),
        duration_s=time.monotonic() - start,
        idempotency_key=key,
    )


__all__ = ["CdcStructCheck"]
