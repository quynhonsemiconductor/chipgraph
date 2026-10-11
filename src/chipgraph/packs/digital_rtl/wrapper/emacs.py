"""The generator: emacs verilog-mode expands a wrapper source (`emacs_auto`, M2-12).

`expand` copies the source into a scratch directory outside the repository, as
`<wrapper>.sv`, and runs, through the `Runner` (never `subprocess` here)::

    emacs -Q --batch --eval <settings> <scratch>/<wrapper>.sv -f verilog-batch-auto

`-Q` loads no init file, site file or site-lisp; `<settings>` turns backups off, and pins
every verilog-mode variable the expansion depends on (`PINNED`), to verilog-mode's
defaults except the three QSoC's flow sets too: `indent-tabs-mode nil`,
`verilog-auto-inst-param-value t` (instance parameters reach the declared widths),
`verilog-auto-star-expand nil`. The IP is found through `verilog-library-files`: its
`vendor_files`, copied (neutralised too) into the scratch directory, so the files in the
repository are neither written nor given to emacs. Before emacs runs, `lisp.scan` refuses
Lisp the source may not carry.

File-local variables are neutralised in the scratch copies (`neutralize`), not just turned
off: verilog-mode's batch command sets `enable-local-variables` and `enable-local-eval`
itself and rereads the `Local Variables:` block of the file it expands, and of the library
files it reads, so an `eval:` there would run (a test shows it). The copies keep their
line numbers, with `Local Variables:` and the `-*-` mode-line marks made inert; a
QSoC-style source that carries such a block (library flags, `indent-tabs-mode`) expands
without it.

The result is the expanded file, line endings `\\n`, exactly one trailing newline, under
a header that names its source and the source's sha256, stamped with the `generated`
check's marker (`chipgraph.checks.generated.stamp`): the same source and IP give the same
bytes. No emacs version goes in the file, so two machines with the same expansion write
the same file; the versions are in the tool's log.

Missing emacs, or an emacs without verilog-mode, is an `error` (infra) with the install
hint. A source emacs cannot expand (an IP module it cannot find, a template it cannot
read) is a `fail` whose issues point at the source's lines.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipgraph.checks._common import compute_idempotency_key, sha256_file
from chipgraph.checks.generated import stamp
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.protocols import Runner
from chipgraph.core.plugin_api.types import ToolContext
from chipgraph.packs.digital_rtl.wrapper import lisp
from chipgraph.packs.digital_rtl.wrapper.common import (
    GEN_RULE,
    Wrapper,
    WrapperError,
    find_wrapper,
    inside,
    paths,
)

EMACS = "emacs"
"""The emacs executable, unless the tool's or check's `emacs` arg names another."""
TIMEOUT_S = 120.0
"""How long one emacs run may take."""
INSTALL_HINT = (
    "install GNU Emacs, which ships verilog-mode: `brew install emacs` (macOS), "
    "`apt install emacs-nox` (Debian, Ubuntu), `dnf install emacs-nox` (Fedora)"
)
_MISSING = 127

PINNED: tuple[tuple[str, str], ...] = (
    ("verilog-auto-inst-param-value", "t"),
    ("verilog-auto-star-expand", "nil"),
    ("verilog-indent-level", "3"),
    ("verilog-indent-level-module", "3"),
    ("verilog-indent-level-declaration", "3"),
    ("verilog-indent-level-behavioral", "3"),
    ("verilog-indent-level-directive", "1"),
    ("verilog-case-indent", "2"),
    ("verilog-cexp-indent", "2"),
    ("verilog-indent-lists", "t"),
    ("verilog-auto-lineup", "'declarations"),
    ("verilog-auto-endcomments", "t"),
    ("verilog-minimum-comment-distance", "10"),
    ("verilog-auto-inst-column", "40"),
    ("verilog-auto-inst-vector", "t"),
    ("verilog-auto-inst-sort", "nil"),
    ("verilog-auto-arg-sort", "nil"),
    ("verilog-auto-arg-format", "'packed"),
    ("verilog-auto-inst-template-numbers", "nil"),
    ("verilog-auto-inst-dot-name", "nil"),
    ("verilog-auto-inst-interfaced-ports", "nil"),
    ("verilog-auto-inst-param-value-type", "t"),
    ("verilog-auto-declare-nettype", "nil"),
    ("verilog-auto-wire-type", "nil"),
    ("verilog-auto-wire-comment", "t"),
    ("verilog-auto-template-warn-unused", "nil"),
    ("verilog-auto-delete-trailing-whitespace", "nil"),
    ("verilog-auto-tieoff-declaration", '"wire"'),
    ("verilog-auto-reset-widths", "t"),
    ("verilog-auto-reset-blocking-in-non", "t"),
    ("verilog-auto-ignore-concat", "nil"),
    ("verilog-auto-read-includes", "nil"),
    ("verilog-auto-sense-include-inputs", "nil"),
    ("verilog-auto-sense-defines-constant", "nil"),
    ("verilog-auto-input-ignore-regexp", "nil"),
    ("verilog-auto-output-ignore-regexp", "nil"),
    ("verilog-auto-inout-ignore-regexp", "nil"),
    ("verilog-auto-unused-ignore-regexp", "nil"),
    ("verilog-auto-tieoff-ignore-regexp", "nil"),
    ("verilog-typedef-regexp", "nil"),
    ("verilog-active-low-regexp", "nil"),
    ("verilog-case-fold", "t"),
    ("verilog-library-flags", "nil"),
    ("verilog-library-directories", '\'(".")'),
    ("verilog-library-extensions", '\'(".v" ".sv")'),
)
"""verilog-mode's variables, as the generator sets them (Lisp values)."""

_PROBE = (
    '(progn (require (quote verilog-mode)) (princ (format "chipgraph-emacs %s|%s\\n" '
    "emacs-version verilog-mode-version)))"
)
_VERSION_RE = re.compile(r"chipgraph-emacs ([^|\s]+)\|(\S+)")
_ERROR_RE = re.compile(
    r"^%Error:\s*(?P<file>[^\n:]+):(?P<line>\d+):\s*(?P<msg>[^\n]*)", re.MULTILINE
)
_HEADER_SOURCE_RE = re.compile(r"^// Generated by chipgraph from (\S+)\s*$")
_HEADER_SHA_RE = re.compile(r"^// source sha256 ([0-9a-f]{64})\s*$")


def lisp_string(text: str) -> str:
    """`text` as an Emacs Lisp string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def settings(library_files: Sequence[Path]) -> str:
    """The `--eval` form: file-local variables off, no backups, `PINNED`, the library."""
    files = " ".join(lisp_string(str(p)) for p in library_files)
    pinned = " ".join(f"{name} {value}" for name, value in PINNED)
    return (
        "(progn (setq-default indent-tabs-mode nil) "
        "(setq enable-local-variables nil enable-local-eval nil enable-dir-local-variables nil "
        "make-backup-files nil auto-save-default nil create-lockfiles nil "
        "large-file-warning-threshold nil) "
        f"(require (quote verilog-mode)) (setq {pinned} verilog-library-files (list {files})))"
    )


def emacs_argv(emacs: str, scratch_file: Path, library_files: Sequence[Path]) -> list[str]:
    """The command line that expands `scratch_file` in place."""
    return [
        emacs,
        "-Q",
        "--batch",
        "--eval",
        settings(library_files),
        str(scratch_file),
        "-f",
        "verilog-batch-auto",
    ]


_LOCAL_VARIABLES_RE = re.compile(r"Local\s+Variables\s*:", re.IGNORECASE)
_MODE_LINE_RE = re.compile(r"-\*-")


def neutralize(text: str) -> str:
    """`text` with file-local variables made inert, line for line (see the module)."""
    text = _LOCAL_VARIABLES_RE.sub("Local Variables (ignored):", text)
    return _MODE_LINE_RE.sub("- * -", text)


def normalize(text: str) -> str:
    """Line endings `\\n` and exactly one trailing newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def source_sha(text: str) -> str:
    """The sha256 of a source's text, as the header records it (normalised first)."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def header(src_rel: str, src_text: str) -> str:
    """The comment a generated wrapper starts with (after the marker): its source, the
    source's sha256, and that it must not be edited."""
    return (
        f"// Generated by chipgraph from {src_rel}\n"
        f"// source sha256 {source_sha(src_text)}\n"
        f"// by `{GEN_RULE}` (tool emacs_auto): emacs verilog-mode expanded its AUTO\n"
        "// comments. Do not edit this file: edit the source and run the generator again;\n"
        "// the `wrap_check` check fails when the two disagree.\n"
    )


HEADER_LINES = 5
"""Lines of `header`."""


def render(body: str, src_rel: str, src_text: str) -> str:
    """The generated file: the marker, the header, then the expansion."""
    return stamp(header(src_rel, src_text) + normalize(body))


def generated_from(text: str) -> tuple[str, str] | None:
    """The (source path, source sha256) a generated file's header names, if any."""
    lines = text.splitlines()[:8]
    for i, line in enumerate(lines[:-1]):
        source = _HEADER_SOURCE_RE.match(line)
        sha = _HEADER_SHA_RE.match(lines[i + 1])
        if source and sha:
            return source.group(1), sha.group(1)
    return None


def body_of(text: str) -> str:
    """A generated file without its marker and header: the expansion itself."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[:3]):
        if _HEADER_SOURCE_RE.match(line):
            return "".join(lines[i + HEADER_LINES :])
    return text


@dataclass
class Expansion:
    """What one emacs run gave: the expanded text, or why there is none."""

    ok: bool
    text: str = ""
    issues: list[Issue] = field(default_factory=list)
    infra: bool = False
    """True when emacs could not run at all (missing, no verilog-mode, timed out)."""
    versions: tuple[str, str] | None = None
    log: str = ""


def _hint(what: str, emacs: str) -> Issue:
    return Issue(rule="wrap.emacs_missing", msg=f"{what} ({emacs}): {INSTALL_HINT}")


async def probe(runner: Runner, cwd: Path, emacs: str = EMACS) -> Expansion:
    """Whether `emacs` runs and has verilog-mode; `versions` are (emacs, verilog-mode)."""
    try:
        result = await runner.run([emacs, "-Q", "--batch", "--eval", _PROBE], cwd=cwd, timeout_s=60)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return Expansion(ok=False, infra=True, issues=[_hint(f"emacs cannot run: {exc}", emacs)])
    log = result.stdout + result.stderr
    if result.returncode == _MISSING:
        return Expansion(
            ok=False, infra=True, issues=[_hint("emacs is not on PATH", emacs)], log=log
        )
    match = _VERSION_RE.search(result.stdout)
    if result.returncode != 0 or match is None:
        msg = "emacs runs but has no verilog-mode" if "verilog-mode" in log else "emacs failed"
        tail = log.strip().splitlines()[-1:] or [""]
        issue = _hint(f"{msg}: {tail[0][:200]}", emacs).model_copy(
            update={"rule": "wrap.verilog_mode_missing"}
        )
        return Expansion(ok=False, infra=True, issues=[issue], log=log)
    return Expansion(ok=True, versions=(match.group(1), match.group(2)), log=log)


async def expand(
    runner: Runner,
    root: Path,
    src_rel: str,
    vendor_files: Sequence[str],
    *,
    name: str,
    emacs: str = EMACS,
    timeout_s: float = TIMEOUT_S,
    text: str | None = None,
) -> Expansion:
    """Expand the source `src_rel` (or `text`, as if it were that file) of wrapper `name`."""
    try:
        source = text if text is not None else inside(root, src_rel).read_text(encoding="utf-8")
        library = [inside(root, rel).resolve() for rel in vendor_files]
    except (OSError, UnicodeDecodeError, WrapperError) as exc:
        return Expansion(ok=False, issues=[Issue(file=src_rel, rule="wrap.read", msg=str(exc))])
    missing = [rel for rel, path in zip(vendor_files, library, strict=True) if not path.is_file()]
    if missing:
        msg = f"the IP file(s) {', '.join(missing)} do not exist (the wrapper's vendor_files)"
        return Expansion(ok=False, infra=True, issues=[Issue(rule="wrap.vendor_missing", msg=msg)])
    refused = [p for p in lisp.scan(source) if not p.note]
    if refused:
        issues = [
            Issue(
                file=src_rel, line=p.line, rule="wrap.lisp", msg=f"{p.message}; emacs was not run"
            )
            for p in refused
        ]
        return Expansion(ok=False, issues=issues)

    found = await probe(runner, root, emacs)
    if not found.ok:
        return found
    scratch = Path(tempfile.mkdtemp(prefix="chipgraph-wrap-"))
    try:
        target = scratch / f"{name}.sv"
        target.write_text(neutralize(source), encoding="utf-8", newline="")
        copies = []
        for index, path in enumerate(library):
            copy = scratch / "ip" / str(index) / path.name
            copy.parent.mkdir(parents=True)
            text = path.read_text(encoding="utf-8", errors="replace")
            copy.write_text(neutralize(text), encoding="utf-8", newline="")
            copies.append(copy)
        argv = emacs_argv(emacs, target, copies)
        result = await runner.run(argv, cwd=scratch, timeout_s=timeout_s)
        log = (result.stdout + result.stderr).replace(str(scratch) + "/", "")
        for copy, rel in zip(copies, vendor_files, strict=True):
            log = log.replace(str(copy.relative_to(scratch)), rel)
        if result.timed_out:
            issue = Issue(rule="wrap.emacs_timeout", msg=f"emacs ran over {timeout_s} s")
            return Expansion(ok=False, infra=True, issues=[issue], versions=found.versions, log=log)
        if result.returncode != 0:
            return Expansion(
                ok=False,
                issues=_emacs_errors(log, src_rel, target.name),
                versions=found.versions,
                log=log,
            )
        expanded = target.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return Expansion(ok=True, text=normalize(expanded), versions=found.versions, log=log)


def _emacs_errors(log: str, src_rel: str, scratch_name: str) -> list[Issue]:
    """emacs's `%Error: <file>:<line>: <msg>` lines, pointed at the source."""
    issues: list[Issue] = []
    seen: set[tuple[int, str]] = set()
    for match in _ERROR_RE.finditer(log):
        line, msg = int(match.group("line")), match.group("msg").strip()
        if (line, msg) in seen:
            continue
        seen.add((line, msg))
        here = Path(match.group("file")).name == scratch_name
        hint = (
            " (is the IP module named as in its declaration, and is its file in the "
            "wrapper's vendor_files?)"
            if "locate" in msg
            else ""
        )
        issues.append(
            Issue(
                file=src_rel if here else None,
                line=line if here and line >= 1 else None,
                rule="wrap.emacs",
                msg=f"emacs verilog-mode could not expand the source: {msg}{hint}",
            )
        )
    if not issues:
        tail = "\n".join(log.strip().splitlines()[-6:])
        issues.append(
            Issue(file=src_rel, rule="wrap.emacs", msg=f"emacs failed to expand the source: {tail}")
        )
    return issues


def wrapper_for(root: Path, params: Mapping[str, str]) -> Wrapper:
    """The profile's wrapper the params name (`WrapperError` when there is none)."""
    try:
        resolved = load_profile(root)
    except ConfigError as exc:
        raise WrapperError(f"the profile cannot be read: {exc}") from exc
    if resolved is None:
        raise WrapperError("no .chipgraph.yml: the wrappers are listed in the profile")
    return find_wrapper(resolved.profile, params)


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


def _str_arg(args: Mapping[str, Any], key: str, default: str | None) -> str | None:
    value = args.get(key, default)
    if value is not None and not isinstance(value, str):
        raise WrapperError(f"args[{key!r}] must be a string")
    return value


class EmacsAutoTool:
    """The gen tool `emacs_auto`: `<wrapper>.src.sv` -> `<wrapper>.sv` (see the module).

    Args (all optional): `src`, `out` (path templates with `{block}`, `{wrapper}`;
    default `rtl/emacs/{wrapper}.src.sv`, `rtl/{wrapper}.sv`), `emacs` (the executable),
    `timeout_s`. The IP's files are the profile's `blocks.<block>.wrappers` entry.
    """

    name = "emacs_auto"
    capability = "gen"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        root = ctx.repo_root
        try:
            wrapper = wrapper_for(root, ctx.params)
            src_rel, out_rel = paths(
                wrapper, _str_arg(spec.args, "src", None), _str_arg(spec.args, "out", None)
            )
            emacs = _str_arg(spec.args, "emacs", EMACS) or EMACS
            timeout = float(spec.args.get("timeout_s", TIMEOUT_S))
            src_path, out_path = inside(root, src_rel), inside(root, out_rel)
        except (WrapperError, TypeError, ValueError) as exc:
            return _result(spec, "error", [Issue(rule="wrap.args", msg=str(exc))], start)
        if not src_path.is_file():
            issue = Issue(file=src_rel, rule="wrap.src_missing", msg=f"no source at {src_rel}")
            return _result(spec, "fail", [issue], start)
        source = src_path.read_text(encoding="utf-8")
        files = [(src_rel, sha256_file(src_path))]
        expansion = await expand(
            ctx.runner,
            root,
            src_rel,
            wrapper.cfg.vendor_files,
            name=wrapper.name,
            emacs=emacs,
            timeout_s=timeout,
            text=source,
        )
        log = expansion.log
        if expansion.versions is not None:
            log = f"emacs {expansion.versions[0]}, verilog-mode {expansion.versions[1]}\n{log}"
        if not expansion.ok:
            status: CheckStatus = "error" if expansion.infra else "fail"
            return _result(spec, status, expansion.issues, start, files, log)
        text = render(expansion.text, src_rel, source)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not out_path.is_file() or out_path.read_text(encoding="utf-8") != text:
            out_path.write_text(text, encoding="utf-8", newline="")
        files.append((out_rel, sha256_file(out_path)))
        note = Issue(severity="info", file=out_rel, rule="wrap.generated", msg=f"wrote {out_rel}")
        return _result(spec, "pass", [note], start, files, log)


__all__ = [
    "EMACS",
    "INSTALL_HINT",
    "PINNED",
    "EmacsAutoTool",
    "Expansion",
    "body_of",
    "emacs_argv",
    "expand",
    "generated_from",
    "header",
    "normalize",
    "probe",
    "render",
    "settings",
    "source_sha",
    "wrapper_for",
]
