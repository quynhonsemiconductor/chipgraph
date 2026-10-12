"""`WrapCheck` (`wrap_check`): a wrapper's generated file is what its source expands to.

The check of the `digital-rtl/wrapper` rules (task M2-12), and the `chipgraph check`
equivalent of QSoC's `make wrap-check`. For each wrapper (the task's, when the params
name one; else every wrapper of the params' block, or of the project) it

1. refuses Lisp the source may not carry (`wrapper.lisp`), and reads the IP module's
   port declaration from the wrapper's `vendor_files` (pyslang, header only);
2. regenerates the expansion with emacs, in a scratch copy (`wrapper.emacs.expand`);
3. checks the expanded instance against the declaration (`wrapper.expansion.validate`):
   an `/*AUTOINST*/` that expanded to nothing, an IP port no template line maps (left
   unconnected to any project signal), an open input, a port the IP does not declare,
   an IP port not connected. These issues point at the source, and quote the expanded
   connection: the author cannot run emacs, so this text is its redo;
4. compares the regeneration with the committed generated file: each differing hunk is
   an issue at the committed file's line, with both texts and "run the generator".
   A difference in whitespace only is a warning (verilog-mode's column alignment can
   differ between emacs versions; QSoC's check ignores whitespace too).

When it compares (arg `compare`): `always`; `never`; `auto` (default): over a block or
the project, always; for a task (a `wrapper` param), only when the generated file was
generated from the current source (its header's source sha256): the author's check runs
before the generator, so a missing or older file is not the author's fault.

Without emacs (or without verilog-mode) the check cannot regenerate. With arg
`require_emacs: true` that is an `error`; `require_emacs` defaults to true when the
environment variable `CI` is set (to anything but empty, `0`, `false` or `no`), so a
CI run never passes on the weaker fallback unless the profile says
`require_emacs: false`. Otherwise (outside CI, so a machine without emacs can still run
`chipgraph check`), when the committed file's `chipgraph:generated` marker matches its
content and its header names the current source's sha256, the committed expansion is
validated as in 3 and the check passes with a warning that the regeneration was not
verified; anything else is an `error` with the install hint.

The fallback is weaker than a regeneration, by design: the marker is a plain sha256 of
the file's content, not a MAC or a signature, so anyone who edits the generated body and
re-renders the marker and header passes it (with the `wrap.not_regenerated` warning).
Only a regeneration with emacs catches that edit (`wrap.differs`).

Args (all optional): `src`, `out` (path templates with `{block}`, `{wrapper}`; default
`rtl/emacs/{wrapper}.src.sv`, `rtl/{wrapper}.sv`, as the rules; refused when they
resolve outside the repository, or under `.git/` or `.chipgraph/`), `compare`,
`require_emacs`, `emacs` (the executable: a trusted profile value, since the check runs
it; a bare name is looked up on `PATH`, a value with a path separator must be an
absolute path to an executable file), `timeout_s` (a number of seconds,
`0 < timeout_s <= 600`; default 120). A source or generated file that cannot be read,
or is not UTF-8, is a `wrap.read` issue.

Profile example (`.chipgraph.yml`)::

    adapters:
      wrap_check: { use: wrap_check }
"""

from __future__ import annotations

import difflib
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipgraph.checks._common import compute_idempotency_key, sha256_file
from chipgraph.checks.generated import verify
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.digital_rtl.wrapper import emacs as gen
from chipgraph.packs.digital_rtl.wrapper import lisp
from chipgraph.packs.digital_rtl.wrapper.common import (
    GEN_RULE,
    Wrapper,
    WrapperError,
    all_wrappers,
    find_wrapper,
    inside,
    instance_id,
    paths,
)
from chipgraph.packs.digital_rtl.wrapper.expansion import instances, validate
from chipgraph.packs.dv.declaration import extract_declaration

COMPARE_MODES = ("auto", "always", "never")
MAX_HUNKS = 20
"""Most differing hunks reported per file."""
MAX_HUNK_LINES = 12
"""Most lines of each side shown per hunk."""
_MARKER_RE = re.compile(r"^\s*//\s*chipgraph:generated sha256=[0-9a-f]{64}\s*$")
_NO_EMACS = ("wrap.emacs_missing", "wrap.verilog_mode_missing")


@dataclass
class _Outcome:
    issues: list[Issue] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    infra: bool = False
    log: str = ""


def _without_marker(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[1:] if lines and _MARKER_RE.match(lines[0]) else lines


def _squash(lines: Sequence[str]) -> str:
    return re.sub(r"\s+", "", "\n".join(lines))


def _shown(lines: Sequence[str], sign: str) -> str:
    head = [f"{sign} {line}" for line in lines[:MAX_HUNK_LINES]]
    if len(lines) > MAX_HUNK_LINES:
        head.append(f"{sign} ... ({len(lines) - MAX_HUNK_LINES} more lines)")
    return "\n".join(head)


def compare(committed: str, generated: str, out_rel: str, rerun: str) -> list[Issue]:
    """The hunks where `committed` differs from `generated` (see the module docstring)."""
    old, new = _without_marker(committed), _without_marker(generated)
    if old == new:
        return []
    if _squash(old) == _squash(new):
        return [
            Issue(
                file=out_rel,
                severity="warning",
                rule="wrap.whitespace",
                msg=(
                    f"{out_rel} differs from the generator's output in whitespace only "
                    f"(another emacs or verilog-mode version?); {rerun} to refresh it"
                ),
            )
        ]
    edited = verify(committed) is False
    note = (
        " The file was edited after it was generated (its chipgraph:generated marker no "
        "longer matches): never edit a generated wrapper."
        if edited
        else ""
    )
    key_old = [" ".join(line.split()) for line in old]
    key_new = [" ".join(line.split()) for line in new]
    offset = len(committed.splitlines()) - len(old)  # the marker line, when there is one
    issues: list[Issue] = []
    matcher = difflib.SequenceMatcher(a=key_old, b=key_new, autojunk=False)
    hunks = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    for tag, i1, i2, j1, j2 in hunks[:MAX_HUNKS]:
        parts = []
        if i2 > i1:
            parts.append(_shown(old[i1:i2], "-"))
        if j2 > j1:
            parts.append(_shown(new[j1:j2], "+"))
        what = {
            "replace": "differs from",
            "delete": "has lines that are not in",
            "insert": "is missing lines of",
        }[tag]
        issues.append(
            Issue(
                file=out_rel,
                line=min(i1, max(len(old) - 1, 0)) + 1 + offset,
                rule="wrap.differs",
                msg=(
                    f"{out_rel} {what} the generator's output here ('-' committed, '+' "
                    f"generated); {rerun}.{note}\n" + "\n".join(parts)
                ),
            )
        )
        note = ""
    if len(hunks) > MAX_HUNKS:
        issues.append(
            Issue(
                file=out_rel,
                rule="wrap.differs",
                msg=f"{len(hunks) - MAX_HUNKS} more differing hunks in {out_rel}; {rerun}",
            )
        )
    return issues


def _mode(args: Mapping[str, Any]) -> str:
    mode = args.get("compare", "auto")
    if mode not in COMPARE_MODES:
        raise WrapperError(f"args['compare'] must be one of {', '.join(COMPARE_MODES)}")
    return str(mode)


def _opt_str(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is not None and not isinstance(value, str):
        raise WrapperError(f"args[{key!r}] must be a string")
    return value


MAX_TIMEOUT_S = 600.0
"""The longest `timeout_s` the check accepts."""
_PROTECTED_DIRS = (".git", ".chipgraph")
_CI_OFF = ("", "0", "false", "no")


def _timeout(args: Mapping[str, Any]) -> float:
    """`timeout_s`: a finite number, `0 < t <= MAX_TIMEOUT_S` (never a bool or a string)."""
    value = args.get("timeout_s", gen.TIMEOUT_S)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT_S
    ):
        raise WrapperError(
            f"args['timeout_s'] must be a number of seconds, 0 < timeout_s <= {MAX_TIMEOUT_S:g}"
        )
    return float(value)


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in _CI_OFF


def _require(args: Mapping[str, Any]) -> bool:
    """`require_emacs`: as given, else whether this runs in CI (see the module)."""
    value = args.get("require_emacs")
    if value is None:
        return _in_ci()
    if not isinstance(value, bool):
        raise WrapperError("args['require_emacs'] must be true or false")
    return value


def _emacs(args: Mapping[str, Any]) -> str:
    """`emacs`: a bare name (looked up on PATH) or an absolute path to an executable."""
    value = _opt_str(args, "emacs") or gen.EMACS
    if "/" in value or "\\" in value or os.sep in value:
        path = Path(value)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise WrapperError(
                "args['emacs'] must be a command name or an absolute path to an executable"
            )
    return value


def _guarded(root: Path, rel: str, key: str) -> Path:
    """The repo file `rel` (`inside`), refused under `.git/` or `.chipgraph/`, as written
    or as resolved."""
    path = inside(root, rel)
    real = Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
    for parts in (Path(rel).parts, real.parts):
        folded = [p.casefold() for p in parts]
        if ".git" in folded or (folded and folded[0] in _PROTECTED_DIRS):
            raise WrapperError(f"args[{key!r}] may not name a file under .git/ or .chipgraph/")
    return path


def _read_issue(rel: str, exc: OSError | UnicodeDecodeError) -> Issue:
    why = "not UTF-8" if isinstance(exc, UnicodeDecodeError) else exc.strerror or str(exc)
    return Issue(file=rel, rule="wrap.read", msg=f"cannot read {rel}: {why}")


class WrapCheck:
    """Regenerates each wrapper and compares it with the committed file (see the module)."""

    id = "wrap_check"
    name = "Wrap check"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args
        try:
            mode = _mode(args)
            require = _require(args)
            emacs = _emacs(args)
            timeout = _timeout(args)
            resolved = load_profile(ctx.repo_root)
            if resolved is None:
                raise WrapperError("no .chipgraph.yml: the wrappers are listed in the profile")
            profile = resolved.profile
            if ctx.params.get("wrapper"):
                targets = [find_wrapper(profile, ctx.params)]
            else:
                targets = all_wrappers(profile, ctx.params.get("block"))
        except (WrapperError, ConfigError, TypeError, ValueError) as exc:
            return self._result(spec, "error", [Issue(rule="wrap.args", msg=str(exc))], start)
        if not targets:
            note = Issue(severity="info", rule="wrap.none", msg="no wrapper to check here")
            return self._result(spec, "skipped", [note], start)

        task = bool(ctx.params.get("wrapper"))
        outcomes = [
            await self._one(
                ctx, w, args, mode=mode, task=task, require=require, emacs=emacs, timeout=timeout
            )
            for w in targets
        ]
        issues = [i for o in outcomes for i in o.issues]
        files = [f for o in outcomes for f in o.files]
        log = "\n".join(o.log for o in outcomes if o.log)
        if any(o.infra for o in outcomes):
            status: CheckStatus = "error"
        elif any(i.severity == "error" for i in issues):
            status = "fail"
        else:
            status = "pass"
        return self._result(spec, status, issues, start, files, log)

    @staticmethod
    def _result(
        spec: CheckSpec,
        status: CheckStatus,
        issues: Sequence[Issue],
        start: float,
        files: Sequence[tuple[str, str]] = (),
        log: str = "",
    ) -> CheckResult:
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            log_tail=log[-4000:],
            duration_s=time.monotonic() - start,
            idempotency_key=compute_idempotency_key(spec.id, spec.args, files),
        )

    async def _one(
        self,
        ctx: ToolContext,
        wrapper: Wrapper,
        args: Mapping[str, Any],
        *,
        mode: str,
        task: bool,
        require: bool,
        emacs: str,
        timeout: float,
    ) -> _Outcome:
        root = ctx.repo_root
        out = _Outcome()
        try:
            src_rel, out_rel = paths(wrapper, _opt_str(args, "src"), _opt_str(args, "out"))
            src_path, out_path = _guarded(root, src_rel, "src"), _guarded(root, out_rel, "out")
            vendor = [inside(root, rel) for rel in wrapper.cfg.vendor_files]
        except WrapperError as exc:
            out.issues.append(Issue(rule="wrap.args", msg=str(exc)))
            out.infra = True
            return out
        rerun = f"run the generator (`chipgraph build '{instance_id(GEN_RULE, wrapper)}'`)"
        if not src_path.is_file():
            out.issues.append(
                Issue(file=src_rel, rule="wrap.src_missing", msg=f"no wrapper source at {src_rel}")
            )
            return out
        reading = src_rel
        try:
            source = src_path.read_text(encoding="utf-8")
            out.files.append((src_rel, sha256_file(src_path)))
            for path, rel in zip(vendor, wrapper.cfg.vendor_files, strict=True):
                reading = rel
                if path.is_file():
                    out.files.append((rel, sha256_file(path)))
            reading = out_rel
            committed = out_path.read_text(encoding="utf-8") if out_path.is_file() else None
            if committed is not None:
                out.files.append((out_rel, sha256_file(out_path)))
        except (OSError, UnicodeDecodeError) as exc:
            out.issues.append(_read_issue(reading, exc))
            out.infra = isinstance(exc, OSError)
            return out
        for note in lisp.scan(source):
            if note.note:
                out.issues.append(
                    Issue(
                        file=src_rel,
                        line=note.line,
                        severity="info",
                        rule="wrap.lisp",
                        msg=note.message,
                    )
                )

        declaration = extract_declaration(vendor, wrapper.cfg.vendor_module)
        if declaration is None:
            out.infra = True
            out.issues.append(
                Issue(
                    rule="wrap.vendor_declaration",
                    msg=(
                        f"no declaration of module `{wrapper.cfg.vendor_module}` in "
                        f"{', '.join(wrapper.cfg.vendor_files)} (the wrapper's vendor_files)"
                    ),
                )
            )
            return out

        current = gen.source_sha(source)
        origin = gen.generated_from(committed) if committed is not None else None
        fresh = committed is not None and origin is not None and origin[1] == current
        expansion = await gen.expand(
            ctx.runner,
            root,
            src_rel,
            wrapper.cfg.vendor_files,
            name=wrapper.name,
            emacs=emacs,
            timeout_s=timeout,
            text=source,
        )
        out.log = expansion.log
        regenerated: str | None = None
        if expansion.ok:
            expanded = expansion.text
            regenerated = gen.render(expanded, src_rel, source)
        elif expansion.infra and {i.rule for i in expansion.issues} <= set(_NO_EMACS):
            marked = committed is not None and verify(committed) is True
            if require or not marked or not fresh:
                if require and "require_emacs" not in args:
                    why = (
                        "the CI environment variable is set, so emacs is required (the "
                        "marker fallback is not a proof; `require_emacs: false` accepts it)"
                    )
                elif require:
                    why = "the profile requires emacs (require_emacs)"
                elif committed is None:
                    why = f"{out_rel} does not exist"
                elif not marked:
                    why = f"{out_rel} has no valid generated marker (edited by hand?)"
                else:
                    why = f"{out_rel} was not generated from the current source"
                out.infra = True
                out.issues.extend(
                    i.model_copy(update={"msg": f"{i.msg}; cannot check {src_rel}: {why}"})
                    for i in expansion.issues
                )
                return out
            assert committed is not None
            expanded = gen.body_of(committed)
            out.issues.append(
                Issue(
                    file=out_rel,
                    severity="warning",
                    rule="wrap.not_regenerated",
                    msg=(
                        f"emacs is not available, so {src_rel} was not regenerated: {out_rel} "
                        "is checked by its generated marker and its source's sha256 instead. "
                        "The marker is a plain sha256, not a signature: an edit of the body "
                        "with a re-rendered header passes here, and only a regeneration "
                        f"catches it ({gen.INSTALL_HINT})"
                    ),
                )
            )
        else:
            out.infra = expansion.infra
            out.issues.extend(expansion.issues)
            return out

        src_lines = {i.name: i.line for i in instances(source, declaration.module)}
        out.issues.extend(
            validate(
                expanded,
                declaration,
                file=src_rel,
                line_for=lambda inst: src_lines.get(inst.name),
                wrapper=wrapper.name,
            )
        )

        wanted = mode == "always" or (mode == "auto" and (not task or fresh))
        if not wanted or regenerated is None:
            return out
        if committed is None:
            out.issues.append(
                Issue(
                    file=out_rel,
                    rule="wrap.not_generated",
                    msg=f"{out_rel} does not exist; {rerun} and commit the result",
                )
            )
            return out
        out.issues.extend(compare(committed, regenerated, out_rel, rerun))
        return out


__all__ = ["COMPARE_MODES", "MAX_TIMEOUT_S", "WrapCheck", "compare"]
