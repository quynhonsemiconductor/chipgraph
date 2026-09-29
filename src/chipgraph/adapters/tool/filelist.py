"""A small, tool-agnostic reader for EDA filelists (``.f`` files).

Filelists are a de facto standard with no single grammar: every simulator/linter reads
its own dialect of the same idea (a list of source files, plus a handful of options).
This reader supports the common subset used across QSoC's filelists (see
``docs/spikes/S2.md``): source paths, ``+incdir+``, ``+define+NAME[=VALUE]``, nested
``-f``/``-F`` (recursive includes), ``-y``/``-v`` library directories (recorded, not
searched), ``--top-module``/``--top``/``-top`` (recorded in ``tops``), ``#``/``//``
comments, blank lines, and ``$VAR``/``${VAR}`` environment expansion. Anything else
(e.g. ``-Wno-*``) is skipped and
reported as a warning rather than raised, so a filelist written for another tool still
loads: chipgraph reads the filelist itself instead of handing it to `slang` verbatim.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PathStyle = Literal["filelist", "cwd"]
"""How relative paths inside a filelist resolve.

``-F <file>``-style filelists (and most simulators' ``-f``) resolve source paths
relative to the *filelist's own directory*; some tools resolve relative to the process's
current working directory instead. This reader takes the style as a parameter rather
than guessing from the flag used to invoke it, since a project may standardize on one
style regardless of which flag its own tools happen to use.
"""


@dataclass(frozen=True, slots=True)
class Filelist:
    """The result of reading one or more filelists: sources, options, and warnings.

    ``sources`` and ``incdirs`` are absolute, de-duplicated, and in first-seen order.
    """

    sources: tuple[Path, ...] = field(default_factory=tuple)
    incdirs: tuple[Path, ...] = field(default_factory=tuple)
    defines: dict[str, str | None] = field(default_factory=dict)
    libdirs: tuple[Path, ...] = field(default_factory=tuple)
    libexts: tuple[str, ...] = field(default_factory=tuple)
    tops: tuple[str, ...] = field(default_factory=tuple)
    """Top modules named by ``--top-module``/``--top``/``-top``, in first-seen order."""
    warnings: tuple[str, ...] = field(default_factory=tuple)


class FilelistError(Exception):
    """Raised only for conditions the caller cannot route around: a missing filelist."""


def read_filelist(
    path: Path, *, relative_to: PathStyle = "filelist", root: Path | None = None
) -> Filelist:
    """Read `path` (and any filelist it nests via `-f`/`-F`) into a `Filelist`.

    `relative_to` picks how relative source/incdir paths resolve: `"filelist"` (each
    path is relative to the directory of the filelist that mentioned it) or `"cwd"`
    (relative to the process's current working directory). Nested filelists keep using
    the same `relative_to` style for their own entries. `root` is the directory `"cwd"`
    paths resolve against (default: the process's working directory), so a caller can read
    a project's filelists without changing directory.
    """
    path = path.resolve()
    if not path.is_file():
        raise FilelistError(f"filelist not found: {path}")

    sources: list[Path] = []
    incdirs: list[Path] = []
    defines: dict[str, str | None] = {}
    libdirs: list[Path] = []
    libexts: list[str] = []
    tops: list[str] = []
    warnings: list[str] = []
    seen_filelists: set[Path] = set()

    _read_one(
        path,
        relative_to=relative_to,
        root=(root or Path.cwd()).resolve(),
        sources=sources,
        incdirs=incdirs,
        defines=defines,
        libdirs=libdirs,
        libexts=libexts,
        tops=tops,
        warnings=warnings,
        seen_filelists=seen_filelists,
    )

    return Filelist(
        sources=_dedup(sources),
        incdirs=_dedup(incdirs),
        defines=defines,
        libdirs=_dedup(libdirs),
        libexts=tuple(dict.fromkeys(libexts)),
        tops=tuple(tops),
        warnings=tuple(warnings),
    )


# Options this reader understands take an argument as a separate token (rather than
# glued on, like `+incdir+` or `-Wno-foo`). Each is skipped-with-a-warning, since the
# argument itself must not be mistaken for a source file.
_TOP_OPTIONS = {"--top-module", "--top", "-top"}
"""Options naming a top module; their argument is recorded in ``Filelist.tops``."""

_KNOWN_VALUE_OPTIONS = {
    "--timescale",
    "-o",
}


def _read_one(
    path: Path,
    *,
    relative_to: PathStyle,
    root: Path,
    sources: list[Path],
    incdirs: list[Path],
    defines: dict[str, str | None],
    libdirs: list[Path],
    libexts: list[str],
    tops: list[str],
    warnings: list[str],
    seen_filelists: set[Path],
) -> None:
    if path in seen_filelists:
        warnings.append(f"{path}: filelist already read, skipping to avoid a cycle")
        return
    seen_filelists.add(path)

    base_dir = path.parent
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"{path}: could not read filelist: {exc}")
        return

    tokens = _tokenize(text)
    i = 0
    while i < len(tokens):
        line_no, token = tokens[i]
        i += 1

        if token in ("-f", "-F"):
            if i >= len(tokens):
                warnings.append(f"{path}:{line_no}: {token} with no argument")
                continue
            _, nested_raw = tokens[i]
            i += 1
            nested_style: PathStyle = "filelist" if token == "-F" else relative_to
            nested = _resolve_path(nested_raw, base_dir, style=nested_style, cwd_base=root)
            _read_one(
                nested,
                relative_to=nested_style,
                root=root,
                sources=sources,
                incdirs=incdirs,
                defines=defines,
                libdirs=libdirs,
                libexts=libexts,
                tops=tops,
                warnings=warnings,
                seen_filelists=seen_filelists,
            )
            continue

        if token.startswith("+incdir+"):
            for raw in token[len("+incdir+") :].split("+"):
                if raw:
                    incdirs.append(_resolve_path(raw, base_dir, style=relative_to, cwd_base=root))
            continue

        if token.startswith("+define+"):
            body = token[len("+define+") :]
            name, sep, value = body.partition("=")
            if name:
                defines[name] = value if sep else None
            continue

        if token in ("-y", "-v"):
            if i >= len(tokens):
                warnings.append(f"{path}:{line_no}: {token} with no argument")
                continue
            _, raw = tokens[i]
            i += 1
            resolved = _resolve_path(raw, base_dir, style=relative_to, cwd_base=root)
            libdirs.append(resolved)
            warnings.append(f"{path}:{line_no}: {token} library directory recorded, not searched")
            continue

        if token.startswith("+libext+"):
            for ext in token[len("+libext+") :].split("+"):
                if ext:
                    libexts.append(ext)
            continue

        if token in _TOP_OPTIONS:
            if i < len(tokens):
                top = tokens[i][1]
                i += 1
                if top not in tops:
                    tops.append(top)
            continue

        if token in _KNOWN_VALUE_OPTIONS:
            if i < len(tokens):
                i += 1  # consume and discard the option's argument too
            warnings.append(f"{path}:{line_no}: unsupported option {token!r} skipped")
            continue

        if token.startswith("-") or token.startswith("+"):
            warnings.append(f"{path}:{line_no}: unknown option {token!r} skipped")
            continue

        # Anything else is a source file path.
        sources.append(_resolve_path(token, base_dir, style=relative_to, cwd_base=root))


def _tokenize(text: str) -> list[tuple[int, str]]:
    """Split filelist text into `(line_number, token)` pairs.

    Strips `#` and `//` comments (a `//` inside a bare path is vanishingly rare in
    filelists and not supported), skips blank lines, and uses `shlex` per line so a
    quoted path with spaces stays one token.
    """
    tokens: list[tuple[int, str]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        for token in shlex.split(line):
            tokens.append((line_no, token))
    return tokens


def _strip_comment(line: str) -> str:
    for marker in ("//", "#"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line


def _resolve_path(raw: str, base_dir: Path, *, style: PathStyle, cwd_base: Path) -> Path:
    expanded = os.path.expandvars(raw)
    candidate = Path(expanded)
    if candidate.is_absolute():
        return candidate
    anchor = base_dir if style == "filelist" else cwd_base
    return (anchor / candidate).resolve()


def _dedup(paths: list[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(paths))
