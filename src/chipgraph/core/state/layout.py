"""Where run state lives on disk (DESIGN.md 6.1): ``<repo>/.chipgraph/state/``.

This is machine-local, gitignored state: journals, run manifests, locks, cache and
idempotency results. It is distinct from ``.chipgraph/decisions/``, which holds
human-auditable, per-decision files that a team may choose to commit (DESIGN.md 6.1).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_RUN_ID_RANDOM_HEX_CHARS = 6


@dataclass(frozen=True, slots=True)
class StateLayout:
    """Paths for a project's local run state, rooted at the project repo root."""

    root: Path

    @property
    def state_dir(self) -> Path:
        """The root of all machine-local run state: ``<root>/.chipgraph/state``."""
        return self.root / ".chipgraph" / "state"

    @property
    def runs_dir(self) -> Path:
        """Where each run gets its own subdirectory."""
        return self.state_dir / "runs"

    def run_dir(self, run_id: str) -> Path:
        """The directory for a single run's journal, manifest and results."""
        return self.runs_dir / run_id

    def journal(self, run_id: str) -> Path:
        """The append-only JSONL journal file for a run."""
        return self.run_dir(run_id) / "journal.jsonl"

    def manifest(self, run_id: str) -> Path:
        """The manifest file for a run."""
        return self.run_dir(run_id) / "manifest.json"

    @property
    def locks_dir(self) -> Path:
        """Where per-block lock files live."""
        return self.state_dir / "locks"

    @property
    def cache_dir(self) -> Path:
        """Where cached, regenerable data lives (not the idempotency store)."""
        return self.state_dir / "cache"

    @property
    def tmp_dir(self) -> Path:
        """Scratch space for disposable work (e.g. fan-out workspaces): ``<state>/tmp``.

        Under the local backend this is inside the repo's working tree, so callers that
        must never write there (fan-out) use the system temp directory instead.
        """
        return self.state_dir / "tmp"

    @property
    def decisions_dir(self) -> Path:
        """Human-auditable gate/waiver decisions: ``<root>/.chipgraph/decisions``.

        This is *not* part of run state: it is meant to be readable, hash-pinned, and
        optionally committed to the project repo (DESIGN.md 6.1).
        """
        return self.root / ".chipgraph" / "decisions"


def new_run_id() -> str:
    """A sortable, unique run id: UTC timestamp plus a short random suffix.

    Format: ``YYYYMMDDTHHMMSSZ-<6 hex chars>``, e.g. ``20260928T120000Z-a1b2c3``.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(_RUN_ID_RANDOM_HEX_CHARS // 2)
    return f"{ts}-{suffix}"
