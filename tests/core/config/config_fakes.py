"""Test doubles for `chipgraph.core.config`."""

from __future__ import annotations

from pathlib import Path


class FakeFetcher:
    """A `SourceFetcher` that serves pre-arranged local directories instead of network git."""

    def __init__(self, repos: dict[str, Path]) -> None:
        self._repos = repos
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, ref: str) -> tuple[Path, str]:
        self.calls.append((url, ref))
        if url not in self._repos:
            raise KeyError(f"no fake repo registered for {url!r}")
        return self._repos[url], f"commit-for-{ref}"
