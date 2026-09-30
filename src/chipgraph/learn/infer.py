"""Infer a project's conventions from an existing repository (DESIGN.md 8.6 V1).

`learn(root)` walks the repository, classifies its files (RTL, filelist, spec, testbench,
doc), infers the block names from where files repeat, and derives a path template per
artifact kind and a naming pattern per identifier kind (through pyslang). Every inference
carries a coverage statistic; a naming pattern is only *emitted* as a rule when its
coverage meets the threshold, otherwise it is reported as an observation for the reviewer.

Nothing here writes into the repository: `learn` is pure inference over the file tree.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from chipgraph.checks._common import iter_repo_files
from chipgraph.checks._naming_pyslang import Decl, declared_identifiers, parse_file
from chipgraph.learn.models import (
    Coverage,
    LayoutRule,
    LearnResult,
    NamingRule,
    Observation,
)

DEFAULT_THRESHOLD = 0.9
"""Minimum coverage for a naming pattern to be emitted as a rule rather than observed."""

_RTL_EXTS = (".sv", ".svh", ".v", ".vh")
_FILELIST_EXTS = (".f", ".F")
_DOC_EXTS = (".md",)
_TB_EXTS = (".py",)

# Directory segments whose contents keep an upstream project's conventions; they are
# exempted from checks (a `paths:` rule) and excluded from inference.
_VENDOR_SEGMENTS = frozenset({"vendor", "third_party", "thirdparty", "external", "extern"})

_MAS_RE = re.compile(r"(?:^|[_/])(?:MAS|SPEC|DATASHEET)(?:[._]|$)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")


@dataclass(slots=True)
class _Files:
    """The repository's files, bucketed by inferred artifact kind (repo-relative posix)."""

    rtl: list[str] = field(default_factory=list)
    filelist: list[str] = field(default_factory=list)
    spec: list[str] = field(default_factory=list)
    tb: list[str] = field(default_factory=list)
    doc: list[str] = field(default_factory=list)


def learn(root: Path, *, threshold: float = DEFAULT_THRESHOLD) -> LearnResult:
    """Infer a draft profile and its evidence for the repository at `root`."""
    root = root.resolve()
    all_files = [f for f in iter_repo_files(root) if not _is_vendor(f)]
    files = _bucket_files(all_files)

    blocks = _infer_blocks(files)
    layout, layout_skipped = _infer_layout(files, blocks)
    naming, naming_skipped = _infer_naming(root, files.rtl, threshold)
    filelist_obs, filelist_style = _observe_filelist(root, files.filelist)
    observations = [
        obs
        for obs in (
            _observe_header(root, files.rtl),
            filelist_obs,
            _observe_doc(root, files.spec),
        )
        if obs is not None
    ]
    observations.extend(_naming_observations(naming))
    observations.extend(layout_skipped)
    observations.extend(naming_skipped)
    vendor_paths = _vendor_paths(iter_repo_files(root))

    return LearnResult(
        root=str(root),
        project=root.name or "project",
        threshold=threshold,
        blocks=tuple(blocks),
        layout=tuple(layout),
        naming=tuple(naming),
        observations=tuple(observations),
        vendor_paths=tuple(vendor_paths),
        filelist_style=filelist_style,
        naming_rules_document=f"{root.name or 'project'}-learned",
    )


# --------------------------------------------------------------------------------------
# file bucketing
# --------------------------------------------------------------------------------------


def _is_vendor(rel: str) -> bool:
    return any(part.lower() in _VENDOR_SEGMENTS for part in Path(rel).parts)


def _bucket_files(files: Iterable[str]) -> _Files:
    out = _Files()
    for rel in files:
        p = Path(rel)
        suffix = p.suffix
        if suffix in _FILELIST_EXTS:
            out.filelist.append(rel)
        elif suffix in _RTL_EXTS:
            out.rtl.append(rel)
        elif suffix in _DOC_EXTS:
            out.doc.append(rel)
            if _MAS_RE.search(p.name):
                out.spec.append(rel)
        elif suffix in _TB_EXTS and _looks_like_tb(rel):
            out.tb.append(rel)
    return out


def _looks_like_tb(rel: str) -> bool:
    name = Path(rel).name.lower()
    parts = {part.lower() for part in Path(rel).parts}
    return (
        "tb" in parts
        or "dv" in parts
        or "test" in parts
        or name.startswith(("tb_", "test_"))
        or name.endswith(("_tb.py", "_test.py"))
    )


# --------------------------------------------------------------------------------------
# block inference
# --------------------------------------------------------------------------------------


def _infer_blocks(files: _Files) -> list[str]:
    """Infer block names from where files of the same kind repeat.

    Two shapes are common: a per-block directory (`design/<block>/<block>.f`, the QSoC
    style) and a per-block file stem (`filelists/<block>.f`, the tinysoc style). Filelists
    name blocks most reliably, so they are tried first; RTL directory levels are the
    fallback for a repo with no filelists.
    """
    from_filelists = _blocks_from_filelists(files.filelist)
    if from_filelists:
        return from_filelists
    return _blocks_from_rtl(files.rtl)


def _blocks_from_filelists(filelists: list[str]) -> list[str]:
    names: list[str] = []
    for rel in filelists:
        p = Path(rel)
        # `design/<block>/<block>.f` -> the parent dir; else the file stem.
        parent = p.parent.name
        stem = p.stem
        block = parent if parent and parent == stem else stem
        if block and block not in names:
            names.append(block)
    return sorted(names)


def _blocks_from_rtl(rtl: list[str]) -> list[str]:
    """Blocks from a directory level that repeats across RTL files (e.g. `design/<block>/`)."""
    counts: Counter[str] = Counter()
    for rel in rtl:
        parts = Path(rel).parts
        if len(parts) >= 2:
            counts[parts[-2]] += 1
    repeated = sorted(name for name, n in counts.items() if n >= 1 and name not in ("rtl", "src"))
    return repeated


# --------------------------------------------------------------------------------------
# layout inference
# --------------------------------------------------------------------------------------


MIN_SAMPLES = 2
"""The fewest files (of one artifact kind) or identifiers (of one kind) a rule is learned
from. One example says nothing about a convention: it would be "learned" at 100%."""


def _infer_layout(files: _Files, blocks: list[str]) -> tuple[list[LayoutRule], list[Observation]]:
    out: list[LayoutRule] = []
    skipped: list[Observation] = []
    for kind, paths in (
        ("rtl", files.rtl),
        ("filelist", files.filelist),
        ("spec", files.spec),
        ("tb", files.tb),
    ):
        if 0 < len(paths) < MIN_SAMPLES:
            skipped.append(_too_few("layout", kind, "file", paths))
            continue
        rule = _layout_rule_for(kind, paths, blocks)
        if rule is not None:
            out.append(rule)
    return out, skipped


def _too_few(topic: str, kind: str, noun: str, samples: list[str]) -> Observation:
    shown = ", ".join(f"`{s}`" for s in sorted(samples)[:3])
    return Observation(
        topic=topic,
        summary=(
            f"{kind}: only {len(samples)} {noun}(s) ({shown}); at least {MIN_SAMPLES} are "
            "needed to learn a rule, so none was emitted"
        ),
    )


def _layout_rule_for(kind: str, paths: list[str], blocks: list[str]) -> LayoutRule | None:
    if not paths:
        return None
    template = _best_template(paths, blocks)
    matched = sum(1 for p in paths if _template_matches(template, p, blocks))
    return LayoutRule(
        kind=kind,
        template=template,
        coverage=Coverage(matched=matched, total=len(paths)),
        examples=tuple(sorted(paths)[:3]),
    )


def _best_template(paths: list[str], blocks: list[str]) -> str:
    """A single path template covering the most files, using `{block}`/`{BLOCK}`.

    The template is built from a representative path (the shortest, so a generic
    `rtl/{block}.sv` wins over one carrying a specific name), replacing each block token
    with the `{block}` placeholder and its upper-case form with `{BLOCK}`.
    """
    representative = min(paths, key=lambda p: (len(Path(p).parts), len(p)))
    if not blocks:
        return _glob_template(representative)
    template = _blockify(representative, blocks)
    directory, _, name = template.rpartition("/")
    if "{block}" in name or "{BLOCK}" in name:
        return template
    # The file name does not carry the block, so it is one file's own name (e.g.
    # `qnsc_pkg.sv`), not a convention: keep the directory, glob the name.
    star = f"*{Path(name).suffix}"
    return f"{directory}/{star}" if directory else star


def _blockify(path: str, blocks: list[str]) -> str:
    parts = list(Path(path).parts)
    out: list[str] = []
    for part in parts:
        out.append(_blockify_segment(part, blocks))
    return "/".join(out)


def _blockify_segment(segment: str, blocks: list[str]) -> str:
    for block in sorted(blocks, key=len, reverse=True):
        if not block:
            continue
        if segment == block:
            return "{block}"
        upper = block.upper()
        if block.lower() != upper and upper in segment:
            segment = segment.replace(upper, "{BLOCK}")
        if block in segment and block != segment:
            segment = segment.replace(block, "{block}")
    return segment


def _glob_template(path: str) -> str:
    """A generic template for a repo with no blocks: keep the dir, glob the stem."""
    p = Path(path)
    parent = p.parent.as_posix()
    star = f"*{p.suffix}"
    return f"{parent}/{star}" if parent not in ("", ".") else star


def _template_matches(template: str, path: str, blocks: list[str]) -> bool:
    regex = _template_to_regex(template, blocks)
    return regex.fullmatch(path) is not None


def _template_to_regex(template: str, blocks: list[str]) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(template):
        if template.startswith("{block}", i):
            parts.append(r"[^/]+")
            i += len("{block}")
        elif template.startswith("{BLOCK}", i):
            parts.append(r"[^/]+")
            i += len("{BLOCK}")
        elif template[i] == "*":
            parts.append(r"[^/]*")
            i += 1
        else:
            parts.append(re.escape(template[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


# --------------------------------------------------------------------------------------
# naming inference (through pyslang)
# --------------------------------------------------------------------------------------

# The regex families a kind's identifiers are tested against, most specific first. The
# first family that covers >= threshold of the kind's identifiers is chosen; if none does,
# a permissive fallback keeps coverage high so the emitted rule never rejects real names.
_PREFIX_FAMILIES: dict[str, tuple[str, ...]] = {
    "port": (r"^(?:i|o|io)_[A-Za-z0-9_]+$", r"^[a-z][A-Za-z0-9_]*$"),
    "instance": (r"^u_[A-Za-z0-9_]+$", r"^[a-z][A-Za-z0-9_]*$"),
    "signal": (r"^(?:r|w|mem)_[A-Za-z0-9_]+$", r"^[a-z][A-Za-z0-9_]*$"),
    "parameter": (r"^(?:P|C|S)_[A-Z0-9_]+$", r"^[A-Za-z][A-Za-z0-9_]*$"),
    "localparam": (r"^(?:P|C|S)_[A-Z0-9_]+$", r"^[A-Za-z][A-Za-z0-9_]*$"),
}
_LOWER_SNAKE = r"^[a-z][a-z0-9_]*$"
_SNAKE_ANY = r"^[A-Za-z][A-Za-z0-9_]*$"


def _infer_naming(
    root: Path, rtl: list[str], threshold: float
) -> tuple[list[NamingRule], list[Observation]]:
    by_kind: dict[str, list[str]] = {}
    for rel in rtl:
        try:
            tree = parse_file(root / rel)
        except OSError:
            continue
        for decl in _safe_decls(tree):
            by_kind.setdefault(decl.kind, []).append(decl.name)

    out: list[NamingRule] = []
    skipped: list[Observation] = []
    for kind in sorted(by_kind):
        names = by_kind[kind]
        if len(set(names)) < MIN_SAMPLES:
            if kind in _NAMING_KINDS:
                skipped.append(_too_few("naming", kind, "identifier", sorted(set(names))))
            continue
        rule = _naming_rule_for(kind, names, threshold)
        if rule is not None:
            out.append(rule)
    return out, skipped


def _safe_decls(tree: object) -> list[Decl]:
    try:
        return declared_identifiers(tree)
    except Exception:
        return []


def _naming_rule_for(kind: str, names: list[str], threshold: float) -> NamingRule | None:
    if not names or kind not in _NAMING_KINDS:
        return None
    pattern, matched = _choose_pattern(kind, names)
    coverage = Coverage(matched=matched, total=len(names))
    emitted = coverage.ratio >= threshold
    return NamingRule(
        kind=kind,  # type: ignore[arg-type]
        pattern=pattern,
        coverage=coverage,
        emitted=emitted,
        description=f"{coverage.percent}% of {kind}s match `{pattern}`",
        examples=tuple(dict.fromkeys(names))[:3],
    )


_NAMING_KINDS: frozenset[str] = frozenset(
    {
        "module",
        "port",
        "parameter",
        "localparam",
        "enum_value",
        "instance",
        "signal",
        "memory",
        "genvar",
    }
)


def _choose_pattern(kind: str, names: list[str]) -> tuple[str, int]:
    """Pick the most specific regex family that covers the most names for `kind`.

    Returns `(pattern, matched)`. Among the kind's candidate families, the one with the
    highest match count wins; on a tie the more specific (earlier) family wins. When even
    the fallback misses names (mixed case, digits), the pattern still describes the
    dominant style, and the coverage statistic tells the reviewer how well it fits.
    """
    families = list(_PREFIX_FAMILIES.get(kind, ()))
    fallback = _dominant_case_pattern(names)
    families.append(fallback)
    best_pattern = fallback
    best_matched = -1
    for pattern in families:
        regex = re.compile(pattern)
        matched = sum(1 for n in names if regex.fullmatch(n))
        if matched > best_matched:
            best_matched = matched
            best_pattern = pattern
    return best_pattern, best_matched


def _dominant_case_pattern(names: list[str]) -> str:
    """A permissive case pattern: all-lower-snake if every name is, else mixed snake."""
    if all(re.fullmatch(_LOWER_SNAKE, n) for n in names):
        return _LOWER_SNAKE
    return _SNAKE_ANY


def _naming_observations(rules: list[NamingRule]) -> list[Observation]:
    """Report the naming patterns that did *not* meet the threshold as observations."""
    out: list[Observation] = []
    for rule in rules:
        if not rule.emitted:
            out.append(
                Observation(
                    topic="naming",
                    summary=(
                        f"{rule.kind}: {rule.description} (below threshold; reported, not enforced)"
                    ),
                    coverage=rule.coverage,
                    detail={"kind": rule.kind, "pattern": rule.pattern},
                )
            )
    return out


# --------------------------------------------------------------------------------------
# header, filelist, doc observations
# --------------------------------------------------------------------------------------


def _observe_header(root: Path, rtl: list[str]) -> Observation | None:
    if not rtl:
        return None
    first_lines: list[str] = []
    total = 0
    for rel in rtl:
        header = _leading_comment(root / rel)
        if header is None:
            continue
        total += 1
        if header:
            first_lines.append(header[0])
    if total == 0:
        return None
    common = Counter(first_lines).most_common(1)
    if not common:
        return None
    line, count = common[0]
    return Observation(
        topic="header",
        summary=f"{round(count / total * 100)}% of RTL files start with `{line.strip()}`",
        coverage=Coverage(matched=count, total=total),
        detail={"first_line": line.strip()},
    )


def _leading_comment(path: Path) -> list[str] | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("//") or line.startswith("/*") or line.startswith("*"):
            out.append(line)
        elif line == "":
            if out:
                break
        else:
            break
    return out


def _observe_filelist(
    root: Path, filelists: list[str]
) -> tuple[Observation | None, Literal["filelist", "root", "none"]]:
    if not filelists:
        return None, "none"
    incdir = 0
    filelist_rel = 0
    root_rel = 0
    for rel in filelists:
        try:
            text = (root / rel).read_text(errors="ignore")
        except OSError:
            continue
        base = (root / rel).parent
        for raw in text.splitlines():
            line = raw.split("//", 1)[0].split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("+incdir"):
                incdir += 1
                continue
            if line.startswith(("-", "+", "$")):
                continue
            if (base / line).is_file():
                filelist_rel += 1
            elif (root / line).is_file():
                root_rel += 1
    style: Literal["filelist", "root"] = "filelist" if filelist_rel >= root_rel else "root"
    label = "relative-to-filelist" if style == "filelist" else "relative-to-root"
    obs = Observation(
        topic="filelist",
        summary=(
            f"{len(filelists)} filelist(s); paths look {label}"
            + (f"; {incdir} +incdir line(s)" if incdir else "")
        ),
        detail={
            "count": len(filelists),
            "style": label,
            "incdir_lines": incdir,
            "filelist_relative_hits": filelist_rel,
            "root_relative_hits": root_rel,
        },
    )
    return obs, style


def _observe_doc(root: Path, specs: list[str]) -> Observation | None:
    if not specs:
        return None
    heading_counts: Counter[str] = Counter()
    for rel in specs:
        try:
            text = (root / rel).read_text(errors="ignore")
        except OSError:
            continue
        seen: set[str] = set()
        for raw in text.splitlines():
            m = _HEADING_RE.match(raw)
            if m is not None:
                title = _normalize_heading(m.group(2))
                if title and title not in seen:
                    seen.add(title)
                    heading_counts[title] += 1
    common = [title for title, n in heading_counts.most_common(6) if n >= max(1, len(specs) // 2)]
    return Observation(
        topic="doc",
        summary=(
            f"{len(specs)} MAS-like doc(s); common headings: "
            + (", ".join(common) if common else "none")
        ),
        detail={"count": len(specs), "template": sorted(specs)[0], "headings": common},
    )


def _normalize_heading(title: str) -> str:
    """Drop a leading section number so `2. Registers` and `Registers` group together."""
    return re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", title).strip()


# --------------------------------------------------------------------------------------
# vendor paths
# --------------------------------------------------------------------------------------


def _vendor_paths(all_files: list[str]) -> list[str]:
    """Vendor/third-party top-level globs present in the repo, as `paths:` exceptions."""
    globs: list[str] = []
    for rel in all_files:
        for part in Path(rel).parts:
            low = part.lower()
            if low in _VENDOR_SEGMENTS:
                glob = f"**/{part}/**" if Path(rel).parts[0] != part else f"{part}/**"
                if glob not in globs:
                    globs.append(glob)
                break
    return sorted(globs)


__all__ = ["DEFAULT_THRESHOLD", "learn"]
