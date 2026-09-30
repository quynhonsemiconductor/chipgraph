"""Shared helpers for the model-based cross checks (M1-07 part A, DESIGN.md 4.4/4.8).

Everything a `spec_schema`/`cross_chip`/`ports_diff` check needs beyond `_model.py`:
building the `error` result when the model is missing, turning a loaded model plus a
list of `Issue`s into a `pass`/`fail` `CheckResult`, and small model helpers (the block
key a scope maps to, the entities owned by a block).
"""

from __future__ import annotations

import time

from chipgraph.checks._common import compute_idempotency_key, error_result
from chipgraph.checks._model import LoadedModel, ModelUnavailable, block_param, load_model
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.model.model import DesignModel
from chipgraph.core.plugin_api.types import ToolContext


def load_model_or_skip(
    spec: CheckSpec, ctx: ToolContext, start: float
) -> LoadedModel | CheckResult:
    """Load the Design Model, or return an `error` `CheckResult` when it is missing.

    The cross checks never build the model themselves (`_model.py` docstring); a missing
    store means `chipgraph ingest` has not run yet, and the message says so. The same
    rule holds for every model-based check (`trace`, `connect`, `hardcode` too).
    """
    try:
        return load_model(ctx.repo_root)
    except ModelUnavailable as exc:
        return error_result(spec, str(exc), start, rule="model")


def result_from_issues(
    spec: CheckSpec,
    issues: list[Issue],
    loaded: LoadedModel,
    start: float,
) -> CheckResult:
    """Build a deterministic `pass`/`fail` `CheckResult` from model-derived issues.

    Issues are sorted by (file, line, rule, msg) so the same model always yields the same
    order. The idempotency key is over the check id, args and the model's build-inputs
    hash: the same ingested model gives the same key, and re-ingesting after an edit
    changes it. A result is `fail` when any issue is an `error`, else `pass` (an `info`
    issue does not fail a check).
    """
    ordered = sort_issues(issues)
    key = compute_idempotency_key(spec.id, spec.args, [("<model>", loaded.build_inputs_hash or "")])
    status: CheckStatus = "fail" if any(i.severity == "error" for i in ordered) else "pass"
    return CheckResult(
        check_id=spec.id,
        status=status,
        issues=tuple(ordered),
        duration_s=time.monotonic() - start,
        idempotency_key=key,
    )


def sort_issues(issues: list[Issue]) -> list[Issue]:
    """Return `issues` in a stable, deterministic order."""
    return sorted(issues, key=lambda i: (i.file or "", i.line or 0, i.rule, i.msg))


def block_key(name: str) -> str:
    """The model key of the block named `name` (`block:<name>`)."""
    return f"block:{name}"


def scope_block_keys(model: DesignModel, block: str) -> set[str]:
    """The block key `block` scopes to, plus its instances (`instance_of` -> it).

    A `--block timer` run covers `block:timer` and every `block:<instance>` that is an
    `instance_of` it (D38: `timer_0`, `timer_1`), so an issue about an instance of the
    scoped IP is still reported.
    """
    ip_key = block_key(block)
    keys = {ip_key}
    for rel in model.get_relations(dst=ip_key, kind="instance_of"):
        keys.add(rel.src)
    return keys


__all__ = [
    "LoadedModel",
    "ModelUnavailable",
    "block_key",
    "block_param",
    "load_model",
    "load_model_or_skip",
    "result_from_issues",
    "scope_block_keys",
    "sort_issues",
]
