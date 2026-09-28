"""`CmdTool`: a `ToolAdapter` that runs a project's own command line, e.g.

``make lint BLOCK={block}``, and reads its log through a `LogParser`. This lets a
project with its own EDA flow plug straight into chipgraph without a dedicated
adapter (DESIGN.md 7.1).
"""

from __future__ import annotations

import hashlib
import shlex
import time
from collections.abc import Sequence
from pathlib import Path

from chipgraph.adapters.parser import PARSERS, GenericRegexParser
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.protocols import LogParser, Runner
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_OK_RETURNCODES = (0,)
_LOG_TAIL_MAX_CHARS = 4000
_MISSING_EXECUTABLE_RETURNCODE = 127

# `ToolContext.runner: Runner` is a forward reference (core/plugin_api/types.py only
# imports `Runner` under `TYPE_CHECKING`, since core must not depend on a concrete
# adapter). Resolve it here, the first place a concrete `Runner` and `ToolContext` are
# both in scope, so constructing a `ToolContext` does not raise `PydanticUserError`.
ToolContext.model_rebuild(_types_namespace={"Runner": Runner})


class CmdTool:
    """Runs `spec.args["cmd"]` through `ctx.runner` and parses its log into `Issue`s."""

    name = "cmd"
    capability = "any"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()

        try:
            argv = _build_argv(spec, ctx)
        except _CmdArgError as exc:
            return CheckResult(
                check_id=spec.id,
                status="error",
                issues=(Issue(msg=str(exc), severity="error"),),
                duration_s=time.monotonic() - start,
                idempotency_key=_hash_key(spec, ctx, argv=None),
            )

        cwd = _resolve_cwd(spec, ctx)
        timeout_s = spec.args.get("timeout_s")
        ok_returncodes = tuple(spec.args.get("ok_returncodes", _DEFAULT_OK_RETURNCODES))

        result = await ctx.runner.run(argv, cwd=cwd, env=ctx.env, timeout_s=timeout_s)

        log = result.stdout + result.stderr
        log_tail = log[-_LOG_TAIL_MAX_CHARS:]

        try:
            parser = _build_parser(spec)
        except _CmdArgError as exc:
            return CheckResult(
                check_id=spec.id,
                status="error",
                issues=(Issue(msg=str(exc), severity="error"),),
                log_tail=log_tail,
                duration_s=time.monotonic() - start,
                idempotency_key=_hash_key(spec, ctx, argv=argv),
            )

        issues = parser.parse(log) if parser is not None else ()

        duration_s = time.monotonic() - start
        idempotency_key = _hash_key(spec, ctx, argv=argv)

        if result.timed_out:
            timeout_issue = Issue(
                msg=f"command timed out after {timeout_s}s: {' '.join(argv)}",
                severity="error",
            )
            return CheckResult(
                check_id=spec.id,
                status="error",
                issues=(*issues, timeout_issue),
                log_tail=log_tail,
                duration_s=duration_s,
                idempotency_key=idempotency_key,
            )

        if result.returncode == _MISSING_EXECUTABLE_RETURNCODE:
            missing_issue = Issue(
                msg=f"command not found: {' '.join(argv)}",
                severity="error",
            )
            return CheckResult(
                check_id=spec.id,
                status="error",
                issues=(*issues, missing_issue),
                log_tail=log_tail,
                duration_s=duration_s,
                idempotency_key=idempotency_key,
            )

        has_error_issue = any(issue.severity == "error" for issue in issues)

        if result.returncode not in ok_returncodes:
            if not issues:
                issues = (
                    Issue(
                        msg=f"command exited with {result.returncode}",
                        severity="error",
                    ),
                )
            return CheckResult(
                check_id=spec.id,
                status="fail",
                issues=issues,
                log_tail=log_tail,
                duration_s=duration_s,
                idempotency_key=idempotency_key,
            )

        status: CheckStatus = "fail" if has_error_issue else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=issues,
            log_tail=log_tail,
            duration_s=duration_s,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def key_for(spec: CheckSpec, ctx: ToolContext) -> str:
        """A stable idempotency key for this check invocation.

        Hashes the substituted argv, the (repo-relative) cwd, the sorted `ctx.env`
        items and `ctx.params`. Never raises: an invalid or unsubstitutable `cmd`
        (the same case that makes `run` return an `error` result) still yields a key,
        it is just keyed on the raw, unsubstituted `args["cmd"]` instead.
        """
        try:
            argv: tuple[str, ...] | None = tuple(_build_argv(spec, ctx))
        except _CmdArgError:
            argv = None
        return _hash_key(spec, ctx, argv=argv)


class _CmdArgError(Exception):
    """Raised for a malformed `CheckSpec.args`, turned into an `error` `CheckResult`."""


def _hash_key(spec: CheckSpec, ctx: ToolContext, *, argv: Sequence[str] | None) -> str:
    cwd = spec.args.get("cwd", "")
    digest_input = repr(
        (
            spec.id,
            tuple(argv) if argv is not None else repr(spec.args.get("cmd")),
            str(cwd),
            tuple(sorted(ctx.env.items())),
            tuple(sorted(ctx.params.items())),
        )
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def _build_argv(spec: CheckSpec, ctx: ToolContext) -> list[str]:
    cmd = spec.args.get("cmd")
    if cmd is None:
        raise _CmdArgError(f"check {spec.id!r}: args['cmd'] is required")

    if isinstance(cmd, str):
        try:
            template = cmd.format(**ctx.params)
        except KeyError as exc:
            raise _CmdArgError(
                f"check {spec.id!r}: missing param {exc.args[0]!r} for cmd template {cmd!r}"
            ) from exc
        return shlex.split(template)

    if isinstance(cmd, list):
        argv: list[str] = []
        for token in cmd:
            try:
                argv.append(str(token).format(**ctx.params))
            except KeyError as exc:
                raise _CmdArgError(
                    f"check {spec.id!r}: missing param {exc.args[0]!r} for cmd token {token!r}"
                ) from exc
        return argv

    raise _CmdArgError(f"check {spec.id!r}: args['cmd'] must be a string or a list of strings")


def _resolve_cwd(spec: CheckSpec, ctx: ToolContext) -> Path:
    cwd_arg = spec.args.get("cwd")
    if cwd_arg is None:
        return ctx.repo_root
    return ctx.repo_root / str(cwd_arg)


def _build_parser(spec: CheckSpec) -> LogParser | None:
    parser_name = spec.args.get("parser")
    regex = spec.args.get("regex")

    if parser_name is not None and regex is not None:
        raise _CmdArgError(
            f"check {spec.id!r}: args['parser'] and args['regex'] are mutually exclusive"
        )

    if regex is not None:
        severity_group = spec.args.get("severity_group")
        default_severity = spec.args.get("default_severity", "error")
        try:
            return GenericRegexParser(
                pattern=regex,
                severity_group=severity_group,
                default_severity=default_severity,
            )
        except ValueError as exc:
            raise _CmdArgError(f"check {spec.id!r}: {exc}") from exc

    if parser_name is not None:
        factory = PARSERS.get(parser_name)
        if factory is None:
            raise _CmdArgError(
                f"check {spec.id!r}: unknown parser {parser_name!r}; "
                f"known parsers: {sorted(PARSERS)}"
            )
        return factory()

    return None
