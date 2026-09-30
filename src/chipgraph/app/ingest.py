"""Wire a project's profile to the extractors and run the core ingest (DESIGN.md 4.1-4.5).

`run_ingest` reads the resolved profile, builds a `SourcePart` for every input it can
(the chip-level spec, each block's RTL via the pyslang extractor, each block's MAS via the
`mas-markdown` extractor), calls `chipgraph.core.model.ingest.ingest` to merge them
deterministically, and writes the result to the project's `ModelStore` (rebuilt from
scratch, under `.chipgraph/state/cache/`, so the repo tree stays clean).

This layer may import adapters, packs and extractors; the merge logic itself is in
`core.model.ingest`, which knows nothing about them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from chipgraph.adapters.tool.filelist import Filelist, FilelistError, read_filelist
from chipgraph.adapters.tool.pyslang import ParseDiagnostic, PyslangExtractor
from chipgraph.app.context import AppContext
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.config.models import Profile
from chipgraph.core.model.ingest import (
    IngestIssue,
    IngestResult,
    InputFile,
    SourcePart,
    ingest,
)
from chipgraph.core.model.model import DesignModel
from chipgraph.core.model.store import ModelStore, default_model_db_path
from chipgraph.core.plugin_api.registry import PluginError, Registry
from chipgraph.packs.spec_core.extract.mas import MasDiagnostic, MasExtractor


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The result of `run_ingest`: what was written, plus the model, stats and issues."""

    model: DesignModel
    result: IngestResult
    db_path: Path

    @property
    def issues(self) -> tuple[IngestIssue, ...]:
        return self.result.issues

    @property
    def has_error(self) -> bool:
        return any(issue.severity == "error" for issue in self.result.issues)


def run_ingest(ctx: AppContext) -> IngestReport:
    """Run every extractor for the project, merge, and write the Design Model store.

    Raises `AppError` if no profile is loaded. A missing file or an extractor exception
    for one block becomes an `error` `IngestIssue` for that block; the rest of ingest
    still runs.
    """
    resolved = ctx.require_profile()
    root = ctx.root

    builder = _PartBuilder(root, resolved, ctx.registry)
    builder.build_chip_spec()
    builder.build_blocks()

    model, result = ingest(
        builder.parts,
        input_files=builder.input_files,
        profile_digest=_profile_digest(resolved.profile),
        ip_blocks={name: override.instances for name, override in resolved.profile.blocks.items()},
    )
    # Fold the builder's own file-level issues (missing files, adapter exceptions) in.
    result = _with_extra_issues(result, builder.issues)

    db_path = default_model_db_path(root)
    store = ModelStore(db_path)
    store.write(model, build_inputs_hash=result.build_inputs_hash)
    return IngestReport(model=model, result=result, db_path=db_path)


@dataclass(slots=True)
class _PartBuilder:
    """Accumulates `SourcePart`s, the files they were read from, and file-level issues."""

    root: Path
    resolved: ResolvedProfile
    registry: Registry
    parts: list[SourcePart] = None  # type: ignore[assignment]
    input_files: list[InputFile] = None  # type: ignore[assignment]
    issues: list[IngestIssue] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.parts = []
        self.input_files = []
        self.issues = []

    # -- chip-level spec ------------------------------------------------------------

    def build_chip_spec(self) -> None:
        chip = self.resolved.profile.spec.chip
        if chip is None:
            return
        path = (self.root / chip.path).resolve()
        # Any `format` adapter in the registry (built-in, entry point or local plugin) that
        # offers `load_model(path, root)` can build the chip-level part.
        try:
            adapter = self.registry.get("format", chip.format)
        except PluginError as exc:
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="chip_spec_format",
                    message=f"unknown chip spec format {chip.format!r}: {exc}",
                    file=chip.path,
                )
            )
            return
        load_model = getattr(adapter, "load_model", None)
        if not callable(load_model):
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="chip_spec_format",
                    message=f"format adapter {chip.format!r} has no load_model(path, root)",
                    file=chip.path,
                )
            )
            return
        if not path.is_file():
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="missing_file",
                    message=f"chip spec not found: {chip.path}",
                    file=chip.path,
                )
            )
            return
        try:
            model, warnings = load_model(path, root=self.root)
        except Exception as exc:  # extractor must not crash ingest
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="chip_spec_error",
                    message=f"failed to read chip spec {chip.path}: {exc}",
                    file=chip.path,
                )
            )
            return
        self._record_file(path)
        diagnostics = tuple(
            IngestIssue(severity="warning", code="chip_spec_warning", message=w) for w in warnings
        )
        self.parts.append(
            SourcePart(source="chip", block=None, model=model, diagnostics=diagnostics)
        )

    # -- per-block RTL and MAS ------------------------------------------------------

    def build_blocks(self) -> None:
        for block in sorted(self.resolved.profile.blocks):
            block_profile = self.resolved.for_block(block)
            self._build_block_rtl(block, block_profile)
            for template in _layout_templates(block_profile, "spec"):
                self._build_block_mas(block, _fill(template, block))
        self._report_unread_specs()

    def _report_unread_specs(self) -> None:
        """Warn about spec files that match the project's spec template but no block read.

        A spec named after a block the profile does not list (or lists under another name)
        would otherwise be skipped silently. Map it with a per-block `layout.spec`, add the
        block, or exempt the file with `paths: {<file>: {checks: {ingest: off}, reason: ...}}`.
        """
        read = {f.rel_path for f in self.input_files}
        patterns: set[str] = set()
        for template in _layout_templates(self.resolved.profile, "spec"):
            if "{block}" in template or "{BLOCK}" in template:
                patterns.add(template.replace("{block}", "*").replace("{BLOCK}", "*"))
        seen: set[str] = set()
        for pattern in sorted(patterns):
            for path in sorted(self.root.glob(pattern)):
                rel = _rel(path, self.root)
                if rel in read or rel in seen or not path.is_file():
                    continue
                seen.add(rel)
                if self.resolved.for_path(rel).checks.get("ingest") == "off":
                    continue
                self.issues.append(
                    IngestIssue(
                        severity="warning",
                        code="unread_spec",
                        message=(
                            f"{rel} matches the spec template {pattern!r} but no block reads "
                            "it; map it in blocks.<name>.layout.spec, or exempt it in paths:"
                        ),
                        file=rel,
                    )
                )

    def _build_block_rtl(self, block: str, block_profile: Profile) -> None:
        templates = _layout_templates(block_profile, "filelist")
        if not templates:
            return  # no filelist template, or an explicit `filelist: []` for this block
        filelist_path = (self.root / _fill(templates[0], block)).resolve()
        rel = _rel(filelist_path, self.root)
        if not filelist_path.is_file():
            self.issues.append(
                IngestIssue(
                    severity="warning",
                    code="missing_filelist",
                    message=(
                        f"block {block!r}: no filelist at {rel}; its RTL is not in the model "
                        "(set blocks.<name>.layout.filelist, or [] if it has none)"
                    ),
                    block=_block_key(block),
                    file=rel,
                )
            )
            return
        try:
            filelist = _read_filelist_resolving(filelist_path, self.root)
        except FilelistError as exc:
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="filelist_error",
                    message=f"cannot read filelist {rel}: {exc}",
                    block=_block_key(block),
                    file=rel,
                )
            )
            return
        if not filelist.sources:
            self.issues.append(
                IngestIssue(
                    severity="info",
                    code="empty_filelist",
                    message=f"filelist for block {block!r} lists no sources",
                    block=_block_key(block),
                    file=rel,
                )
            )
            return

        parse = _parse_options(block_profile)
        try:
            model, diags = PyslangExtractor.extract_model(
                filelist,
                block=None,
                root=self.root,
                tops=parse.tops,
                defines=parse.defines,
                clock_patterns=parse.clock_patterns,
                reset_patterns=parse.reset_patterns,
            )
        except Exception as exc:
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="rtl_error",
                    message=f"RTL extraction failed for block {block!r}: {exc}",
                    block=_block_key(block),
                    file=rel,
                )
            )
            return

        self._record_file(filelist_path)
        for source in filelist.sources:
            self._record_file(source)
        diagnostics = tuple(_parse_diag_to_issue(d, block, self.root) for d in diags)
        self.parts.append(
            SourcePart(source="rtl", block=_block_key(block), model=model, diagnostics=diagnostics)
        )

    def _build_block_mas(self, block: str, rel_path: str) -> None:
        block_profile = self.resolved.for_block(block)
        mas_path = (self.root / rel_path).resolve()
        rel = _rel(mas_path, self.root)
        if not mas_path.is_file():
            self.issues.append(
                IngestIssue(
                    severity="warning",
                    code="missing_spec",
                    message=(
                        f"block {block!r}: no spec at {rel}; its requirements are not in the "
                        "model (set blocks.<name>.layout.spec, or [] if it has none)"
                    ),
                    block=_block_key(block),
                    file=rel,
                )
            )
            return
        requirements = block_profile.spec.requirements
        try:
            model, diags = MasExtractor.extract_model(
                mas_path, block=block, requirements=requirements, root=self.root
            )
        except Exception as exc:
            self.issues.append(
                IngestIssue(
                    severity="error",
                    code="mas_error",
                    message=f"MAS extraction failed for block {block!r}: {exc}",
                    block=_block_key(block),
                    file=rel,
                )
            )
            return

        self._record_file(mas_path)
        diagnostics = tuple(_mas_diag_to_issue(d, block) for d in diags)
        self.parts.append(
            SourcePart(source="mas", block=_block_key(block), model=model, diagnostics=diagnostics)
        )

    # -- helpers --------------------------------------------------------------------

    def _record_file(self, path: Path) -> None:
        rel = _rel(path, self.root)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return
        self.input_files.append(InputFile(rel_path=rel, sha256=digest))


def _read_filelist_resolving(filelist_path: Path, root: Path) -> Filelist:
    """Read a filelist, choosing the path style that actually resolves to real files.

    QSoC-style filelists resolve source paths relative to the filelist's own directory;
    tinysoc-style filelists resolve them relative to the project root (how its Makefile
    runs Verilator). Try filelist-relative first, and fall back to root-relative when that
    leaves listed sources unresolved, so both conventions ingest without a profile flag.
    """
    filelist = read_filelist(filelist_path, relative_to="filelist", root=root)
    if filelist.sources and all(src.is_file() for src in filelist.sources):
        return filelist
    alt = read_filelist(filelist_path, relative_to="cwd", root=root)
    if alt.sources and sum(src.is_file() for src in alt.sources) > sum(
        src.is_file() for src in filelist.sources
    ):
        return alt
    return filelist


@dataclass(frozen=True, slots=True)
class _ParseOptions:
    """Typed pyslang options, with the extractor's own defaults when unset."""

    clock_patterns: tuple[str, ...] = (r"^i_clk", r"^clk")
    reset_patterns: tuple[str, ...] = (r"^i_rst", r"^rst")
    tops: tuple[str, ...] | None = None
    defines: dict[str, str | None] | None = None


def _parse_options(profile: Profile) -> _ParseOptions:
    """The pyslang extractor options from `adapters.parse` (all optional)."""
    cfg = profile.adapters.get("parse")
    if cfg is None:
        return _ParseOptions()
    extra = cfg.model_dump()
    updates: dict[str, object] = {}
    clock = extra.get("clock_patterns")
    reset = extra.get("reset_patterns")
    tops = extra.get("tops")
    defines = extra.get("defines")
    if isinstance(clock, list | tuple):
        updates["clock_patterns"] = tuple(str(p) for p in clock)
    if isinstance(reset, list | tuple):
        updates["reset_patterns"] = tuple(str(p) for p in reset)
    if isinstance(tops, list | tuple):
        updates["tops"] = tuple(str(t) for t in tops)
    if isinstance(defines, dict):
        updates["defines"] = {str(k): (None if v is None else str(v)) for k, v in defines.items()}
    return _ParseOptions(**updates)  # type: ignore[arg-type]


def _layout_templates(profile: Profile, key: str) -> tuple[str, ...]:
    """The layout template(s) for `key`: one string, a list of several, or `()` for none.

    An explicit empty list (`spec: []`) says the block has no such file, so nothing is
    reported missing for it.
    """
    value = profile.layout.get(key)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, tuple):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def _fill(template: str, block: str) -> str:
    return template.replace("{block}", block).replace("{BLOCK}", block.upper())


def _block_key(block: str) -> str:
    return f"block:{block}"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_diag_to_issue(diag: ParseDiagnostic, block: str, root: Path) -> IngestIssue:
    # slang's warnings are lint findings (the `lint` check reports those); for ingest only
    # a parse or elaboration error matters, so warnings are kept as info.
    severity = "error" if diag.severity == "error" else "info"
    file = _rel(Path(diag.file), root) if diag.file else None
    return IngestIssue(
        severity=severity,  # type: ignore[arg-type]
        code="rtl_diagnostic",
        message=diag.message,
        block=_block_key(block),
        file=file,
        line=diag.line,
    )


def _mas_diag_to_issue(diag: MasDiagnostic, block: str) -> IngestIssue:
    return IngestIssue(
        severity=diag.severity,
        code=f"mas.{diag.code}",
        message=diag.message,
        block=_block_key(block),
        file=diag.file,
        line=diag.line,
    )


def _with_extra_issues(result: IngestResult, extra: list[IngestIssue]) -> IngestResult:
    if not extra:
        return result
    from chipgraph.core.model.ingest import IngestStats

    issues = _sort_issues(tuple(result.issues) + tuple(extra))
    by_severity: dict[str, int] = {}
    for issue in issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
    stats = result.stats.model_copy(
        update={"diagnostics_by_severity": dict(sorted(by_severity.items()))}
    )
    assert isinstance(stats, IngestStats)
    return result.model_copy(update={"issues": issues, "stats": stats})


def _sort_issues(issues: tuple[IngestIssue, ...]) -> tuple[IngestIssue, ...]:
    order = {"error": 0, "warning": 1, "info": 2}
    return tuple(
        sorted(
            issues,
            key=lambda i: (
                order.get(i.severity, 3),
                i.code,
                i.key or "",
                i.block or "",
                i.file or "",
                i.line or 0,
                i.message,
            ),
        )
    )


def _profile_digest(profile: Profile) -> str:
    """A stable digest of the ingest-relevant profile sections.

    Changing any of these (spec source, requirement config, layout templates, blocks or
    the parse adapter) changes the build-inputs hash, so a stale cache is detected.
    """
    relevant = {
        "spec": profile.spec.model_dump(mode="json"),
        "layout": {k: v for k, v in sorted(profile.layout.items())},
        "blocks": {k: v.model_dump(mode="json") for k, v in sorted(profile.blocks.items())},
        "parse": profile.adapters["parse"].model_dump(mode="json")
        if "parse" in profile.adapters
        else None,
    }
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["IngestReport", "run_ingest"]
