"""Which project files `/ask` may search, and indexing them into the model store.

`ModelStore.write()` rebuilds the database from scratch, which drops every document added
with `ModelStore.add_document`. So `chipgraph ingest` calls `index_documents` right after
it writes the model: every ingest re-indexes the same, deterministic document set.

The document set is:

- every input file ingest read (the chip-level spec, each block's spec, each filelist and
  the RTL sources it lists), and
- the project's own Markdown docs: `README.md`, `AGENTS.md`, `doc/**/*.md`, `docs/**/*.md`,

minus files labelled `nda` (every `/ask` model may be a cloud model, so an `nda` file is
never indexed; ingest reports each one it skipped), vendored paths (a `vendor`,
`third_party`, ... path segment), paths outside the project, and binary or very large files.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.model.store import ModelStore
from chipgraph.core.state.artifacts import LabelRules

MARKDOWN_DOCS: tuple[str, ...] = ("README.md", "AGENTS.md", "doc/**/*.md", "docs/**/*.md")
"""Globs (relative to the project root) of the Markdown docs indexed besides ingest inputs."""

VENDOR_SEGMENTS = frozenset({"vendor", "third_party", "thirdparty", "external", "extern"})
"""A path with one of these segments is vendored code: never indexed."""

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
"""Files larger than this (generated netlists, dumps) are not indexed."""

SkipReason = Literal["nda", "vendor", "outside_root", "missing", "binary", "too_large"]


class SkippedDocument(BaseModel):
    """A candidate document that was not indexed, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="Repo-relative POSIX path (or the path as given).")
    reason: SkipReason = Field(description="Why it was not indexed.")


class DocumentIndexReport(BaseModel):
    """What `index_documents` indexed for `/ask`, and what it skipped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    indexed: tuple[str, ...] = Field(default=(), description="Indexed paths, sorted.")
    lines: int = Field(default=0, description="Total number of indexed lines.")
    skipped: tuple[SkippedDocument, ...] = Field(
        default=(), description="Candidates not indexed, sorted by path."
    )

    @property
    def nda_skipped(self) -> tuple[str, ...]:
        """The paths skipped because they are labelled `nda`."""
        return tuple(s.path for s in self.skipped if s.reason == "nda")


def document_candidates(root: Path, input_paths: Iterable[str]) -> list[str]:
    """Every candidate document path: ingest inputs plus the Markdown docs, sorted, unique."""
    candidates = {_normalise(p) for p in input_paths}
    for pattern in MARKDOWN_DOCS:
        for path in root.glob(pattern):
            if path.is_file():
                candidates.add(_relative(path, root))
    return sorted(candidates)


def index_documents(
    root: Path,
    store: ModelStore,
    input_paths: Iterable[str],
    labels: LabelRules,
) -> DocumentIndexReport:
    """Index the `/ask` document set of the project at `root` into `store`.

    Call it after `store.write()`, which drops earlier documents. Deterministic: the same
    files give the same rows, in the same order.
    """
    indexed: list[str] = []
    skipped: list[SkippedDocument] = []
    lines = 0
    for rel in document_candidates(root, input_paths):
        reason = _skip_reason(rel, root, labels)
        if reason is not None:
            skipped.append(SkippedDocument(path=rel, reason=reason))
            continue
        data = (root / rel).read_bytes()
        if len(data) > MAX_DOCUMENT_BYTES:
            skipped.append(SkippedDocument(path=rel, reason="too_large"))
            continue
        if b"\x00" in data:
            skipped.append(SkippedDocument(path=rel, reason="binary"))
            continue
        text = data.decode("utf-8", errors="replace")
        store.add_document(rel, text)
        indexed.append(rel)
        lines += len(text.splitlines())
    return DocumentIndexReport(indexed=tuple(indexed), lines=lines, skipped=tuple(skipped))


def is_vendor_path(rel: str) -> bool:
    """True when `rel` has a vendored-code path segment."""
    return any(part.lower() in VENDOR_SEGMENTS for part in PurePosixPath(rel).parts)


def _skip_reason(rel: str, root: Path, labels: LabelRules) -> SkipReason | None:
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts:
        return "outside_root"
    if labels.label_for(rel) == "nda":
        return "nda"
    if is_vendor_path(rel):
        return "vendor"
    path = root / rel
    if not path.is_file():
        return "missing"
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return "outside_root"  # a symlink out of the project
    return None


def _normalise(rel: str) -> str:
    text = rel.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "MARKDOWN_DOCS",
    "MAX_DOCUMENT_BYTES",
    "VENDOR_SEGMENTS",
    "DocumentIndexReport",
    "SkippedDocument",
    "document_candidates",
    "index_documents",
    "is_vendor_path",
]
