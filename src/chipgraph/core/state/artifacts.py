"""Content hashing, data labels, and the file-based artifact store.

Pure Python: no subprocess calls and no knowledge of git or any other VCS. The store
only ever hashes bytes it reads itself; `chipgraph.adapters.vcs` is what talks to git.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from chipgraph.core.contracts.artifact import Artifact, ArtifactRef, ProducedBy
from chipgraph.core.contracts.types import ArtifactKind, DataLabel, Sha256

_CHUNK_SIZE = 1024 * 1024


def hash_bytes(data: bytes) -> str:
    """Return the lowercase hex sha256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    """Return the lowercase hex sha256 digest of the file at `path`, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(hashes: Iterable[tuple[str, str]]) -> str:
    """Return a stable sha256 over `(key, hash)` pairs, independent of iteration order.

    Each pair is typically `("<repo>:<path or model_key>", content_hash)`. Pairs are
    sorted before hashing so the same set of inputs always yields the same digest,
    regardless of the order they were produced or discovered in.
    """
    digest = hashlib.sha256()
    for key, value in sorted(hashes):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class LabelRules:
    """Maps a repo-relative path to a `DataLabel` by matching POSIX globs.

    Built from an ordered mapping of `glob -> DataLabel` plus a default label used
    when nothing matches. The most specific matching glob wins: the longest pattern
    string; ties are broken by preferring the entry that appears later in `globs`.
    """

    def __init__(
        self,
        globs: Mapping[str, DataLabel] | None = None,
        *,
        default: DataLabel = "internal",
    ) -> None:
        self._globs: tuple[tuple[str, DataLabel], ...] = tuple((globs or {}).items())
        self._default = default

    @property
    def default(self) -> DataLabel:
        """The label used when no glob matches."""
        return self._default

    def label_for(self, rel_path: str) -> DataLabel:
        """Return the label for `rel_path`, applying the most specific matching glob."""
        path = PurePosixPath(rel_path)
        best_pattern: str | None = None
        best_label: DataLabel = self._default
        for pattern, label in self._globs:
            if not _glob_matches(path, pattern):
                continue
            if best_pattern is None or len(pattern) >= len(best_pattern):
                best_pattern = pattern
                best_label = label
        return best_label


def _glob_matches(path: PurePosixPath, pattern: str) -> bool:
    """Match `path` against a POSIX glob `pattern`, where `**` spans directories."""
    return path.full_match(pattern)


class ArtifactStore:
    """Reads and hashes file-based artifacts under `root`, applying `labels`."""

    def __init__(self, root: Path, labels: LabelRules, repo: str = ".") -> None:
        self.root = root
        self.labels = labels
        self.repo = repo

    def ref(self, rel_path: str, kind: ArtifactKind) -> ArtifactRef:
        """Build an `ArtifactRef` for `rel_path`, applying the configured data label."""
        return ArtifactRef(
            repo=self.repo,
            kind=kind,
            path=rel_path,
            label=self.labels.label_for(rel_path),
        )

    def artifact(
        self,
        rel_path: str,
        kind: ArtifactKind,
        produced_by: ProducedBy | None = None,
        inputs_hash: Sha256 | None = None,
    ) -> Artifact:
        """Build an `Artifact` for `rel_path`, hashing its current content.

        Raises `FileNotFoundError` with a clear message if the file does not exist
        under `root`.
        """
        ref = self.ref(rel_path, kind)
        full_path = self.root / rel_path
        if not full_path.is_file():
            raise FileNotFoundError(
                f"cannot hash artifact {rel_path!r}: no such file under {self.root}"
            )
        return Artifact(
            ref=ref,
            content_hash=hash_file(full_path),
            produced_by=produced_by,
            inputs_hash=inputs_hash,
        )

    def exists(self, ref: ArtifactRef) -> bool:
        """Return whether `ref` currently exists on disk (path-based refs only)."""
        rel_path = self._require_path(ref)
        return (self.root / rel_path).is_file()

    def current_hashes(self, refs: Iterable[ArtifactRef]) -> dict[str, str]:
        """Return `{"<repo>:<path>": content_hash}` for every `ref` that exists.

        Refs whose file is missing are skipped (not included in the result), rather
        than mapped to an empty-string hash.
        """
        result: dict[str, str] = {}
        for ref in refs:
            rel_path = self._require_path(ref)
            full_path = self.root / rel_path
            if not full_path.is_file():
                continue
            result[f"{ref.repo}:{rel_path}"] = hash_file(full_path)
        return result

    @staticmethod
    def _require_path(ref: ArtifactRef) -> str:
        if ref.path is None:
            raise ValueError("model artifacts are hashed by the Design Model")
        return ref.path
