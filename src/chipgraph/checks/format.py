"""`FormatCheck`: the project's formatter, run by the engine on an agent task's outputs.

The first check of `digital-rtl/rtl_module` (DESIGN.md 8.6 V3, task M2-04). The engine,
never the agent, runs it once the agent has finished and before the other checks, so
lint, naming and style see the formatted file, and the output hash the engine records
(the task's accepted outputs, the build record) is the formatted file's. It works the
same in both runtimes: `submit` (claude-code) and `AgentRuleExecutor` (in process) both
run a rule's checks after the attempt and hash the outputs after the checks.

Which files: `target_files` (shared with the `style` check):

- arg `files`: path templates (`{block}`, `{BLOCK}`, `{module}` filled from the task's
  params);
- else, for a task with params `block` and `module` (a `foreach: plan.modules` rule), the
  module's write set in the block's approved plan (arg `plan`, default
  `plan/{block}.approved.json`), its HDL files only (`.sv`, `.svh`, `.v`, `.vh`). The
  write set bounds the task's outputs, so the check writes only what the task may write.

Without either (e.g. `chipgraph check` over a block) it is `skipped`: it never formats a
person's files outside a task.

Which formatter: arg `formatter` (an inline adapter config), else the profile's
`style.formatter`, e.g.::

    style:
      formatter: { use: cmd, cmd: ["verible-verilog-format", "--inplace", "{file}"] }

It runs once per file, with the task's params plus `file` (the repo-relative path) and
`file_abs`; it must rewrite the file in place. With no formatter configured,
`verible-verilog-format --inplace` is used when it is on PATH; otherwise the check is
`skipped` with a note. A configured formatter that fails, or cannot be started, makes
the check `error` with the formatter's message (an infra failure: no try is used); the
file is then put back as it was.

Profile example (`.chipgraph.yml`)::

    adapters:
      format: { use: format }
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from chipgraph.checks._common import compute_idempotency_key, sha256_file
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.config.models import AdapterCfg
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.registry import PluginError, Registry
from chipgraph.core.plugin_api.types import ToolContext

DEFAULT_FORMATTER = "verible-verilog-format"
"""Used, with `--inplace`, when the profile names no formatter and it is on PATH."""
DEFAULT_APPROVED_PLAN = "plan/{block}.approved.json"
"""Where a block's approved plan is (the `plan_expand` tool's default output)."""
HDL_SUFFIXES = (".sv", ".svh", ".v", ".vh")
"""The files of a module's write set the per-task checks format and measure."""


class TargetError(Exception):
    """The files of a per-task check cannot be named (bad args, an unreadable plan)."""


def _fill(template: str, params: Mapping[str, str]) -> str | None:
    """`template` with `{key}`/`{KEY}` from `params`; `None` if a key is not a param."""
    out = template
    for key, value in params.items():
        out = out.replace("{" + key + "}", value).replace("{" + key.upper() + "}", value.upper())
    return None if "{" in out else out


def _relative(path: str) -> str:
    text = path.replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if text.startswith("/") or ".." in parts or not parts:
        raise TargetError(f"{path!r} is not a repo-relative path")
    return "/".join(parts)


def target_files(
    args: Mapping[str, Any], params: Mapping[str, str], root: Path
) -> list[str] | None:
    """The repo-relative files a per-task check works on, or `None` for none (see above).

    Raises `TargetError` for a malformed `files`/`plan` arg or an approved plan that
    cannot be read.
    """
    files = args.get("files")
    if files is not None:
        templates = [files] if isinstance(files, str) else files
        if not isinstance(templates, list) or not all(isinstance(t, str) for t in templates):
            raise TargetError("args['files'] must be a path template or a list of them")
        filled = [_fill(t, params) for t in templates]
        if any(f is None for f in filled):
            return None
        return list(dict.fromkeys(_relative(f) for f in filled if f is not None))
    block, module = params.get("block"), params.get("module")
    if not block or not module:
        return None
    plan_t = args.get("plan", DEFAULT_APPROVED_PLAN)
    if not isinstance(plan_t, str):
        raise TargetError("args['plan'] must be a path template")
    plan_rel = _fill(plan_t, params)
    if plan_rel is None:
        return None
    path = root / _relative(plan_rel)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = next(m for m in data.get("modules", []) if m.get("name") == module)
        writes = [str(w) for w in entry.get("writes", [])]
    except (OSError, ValueError, AttributeError, StopIteration) as exc:
        raise TargetError(f"cannot read module {module!r} from {plan_rel}: {exc!r}") from exc
    return [_relative(w) for w in writes if w.endswith(HDL_SUFFIXES)]


def formatter_config(args: Mapping[str, Any], root: Path) -> dict[str, Any] | None:
    """The formatter's adapter config: arg `formatter`, else the profile's `style.formatter`."""
    inline = args.get("formatter")
    if inline is not None:
        return AdapterCfg.model_validate(inline).model_dump()
    resolved = load_profile(root)
    if resolved is None or resolved.profile.style.formatter is None:
        return None
    return resolved.profile.style.formatter.model_dump()


def _result(
    spec: CheckSpec,
    status: CheckStatus,
    issues: list[Issue],
    start: float,
    hashes: list[tuple[str, str]],
    log_tail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=spec.id,
        status=status,
        issues=tuple(issues),
        log_tail=log_tail[-4000:],
        duration_s=time.monotonic() - start,
        idempotency_key=compute_idempotency_key(spec.id, spec.args, hashes),
    )


def _plugin(use: str) -> Any:
    registry = Registry()
    registry.discover()
    for kind in ("tool", "check"):
        try:
            return registry.get(kind, use)
        except PluginError:
            continue
    raise PluginError(f"no tool or check adapter named {use!r} (the formatter)")


def _message(result: CheckResult) -> str:
    msgs = "; ".join(i.msg for i in result.issues if i.severity == "error")
    return msgs or result.log_tail.strip()[-400:] or f"the formatter returned {result.status}"


class FormatCheck:
    """Runs the project's formatter in place on a task's output files."""

    id = "format"
    name = "Format"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        root = ctx.repo_root
        try:
            files = target_files(spec.args, ctx.params, root)
            cfg = formatter_config(spec.args, root)
        except (TargetError, ValueError, ConfigError) as exc:
            issue = Issue(rule="format.args", msg=str(exc))
            return _result(spec, "error", [issue], start, [])
        if files is None:
            note = Issue(
                severity="info",
                rule="format.no_files",
                msg=(
                    "nothing to format: the format check runs on an agent task's outputs "
                    "(args 'files', or params block and module with an approved plan)"
                ),
            )
            return _result(spec, "skipped", [note], start, [])
        if cfg is None:
            found = shutil.which(DEFAULT_FORMATTER)
            if found is None:
                note = Issue(
                    severity="info",
                    rule="format.no_formatter",
                    msg=(
                        f"no formatter: the profile sets no style.formatter and "
                        f"{DEFAULT_FORMATTER} is not on PATH; the files are left as written"
                    ),
                )
                return _result(spec, "skipped", [note], start, [])
            cfg = {"use": "cmd", "cmd": [found, "--inplace", "{file}"]}
        use = str(cfg.pop("use"))
        try:
            plugin = _plugin(use)
        except PluginError as exc:
            return _result(spec, "error", [Issue(rule="format.adapter", msg=str(exc))], start, [])

        issues: list[Issue] = []
        logs: list[str] = []
        hashes: list[tuple[str, str]] = []
        failed = False
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue  # the engine's own output check reports a missing output
            before = path.read_bytes()
            params = {**ctx.params, "file": rel, "file_abs": str(path.resolve())}
            sub_ctx = ctx.model_copy(update={"params": params})
            sub_spec = CheckSpec(
                id=f"{spec.id}.formatter", capability="format", adapter=use, args=cfg
            )
            try:
                result: CheckResult = await plugin.run(sub_spec, sub_ctx)
            except Exception as exc:  # a broken formatter adapter: infra, never the agent's
                result = CheckResult(
                    check_id=sub_spec.id,
                    status="error",
                    issues=(Issue(msg=f"{type(exc).__name__}: {exc}"),),
                    duration_s=0.0,
                    idempotency_key="format:raised",
                )
            if result.log_tail:
                logs.append(result.log_tail)
            if result.status != "pass" or not path.is_file():
                failed = True
                path.write_bytes(before)  # leave the agent's file as it was
                issues.append(
                    Issue(
                        file=rel,
                        rule="format.failed",
                        msg=f"the formatter ({use}) failed on {rel}: {_message(result)}",
                    )
                )
                continue
            if path.read_bytes() != before:
                issues.append(
                    Issue(severity="info", file=rel, rule="format.changed", msg=f"formatted {rel}")
                )
            hashes.append((rel, sha256_file(path)))
        status: CheckStatus = "error" if failed else "pass"
        return _result(spec, status, issues, start, hashes, "\n".join(logs))


__all__ = [
    "DEFAULT_APPROVED_PLAN",
    "DEFAULT_FORMATTER",
    "HDL_SUFFIXES",
    "FormatCheck",
    "TargetError",
    "formatter_config",
    "target_files",
]
