"""The `file` review adapter: one small YAML decision file per approval.

Implements `chipgraph.core.plugin_api.protocols.ReviewAdapter`. Every recorded
decision (approve, reject, baseline, waive) is written as its own file under a
`decisions_dir` (typically `.chipgraph/decisions/`), so two people approving at the
same time never conflict on the same file (DESIGN.md 6.1).
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from chipgraph.core.contracts import Approval

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_NAME_ATTEMPTS = 8


class ReviewError(Exception):
    """Raised when a decision file cannot be recorded or a decision file is malformed."""


def _safe_id(gate_id: str) -> str:
    """Replace any character outside `[A-Za-z0-9._-]` in `gate_id` with `_`."""
    return _UNSAFE_ID_CHARS.sub("_", gate_id)


class FileReview:
    """Records approvals as one YAML file per decision under `decisions_dir`."""

    name = "file"

    def __init__(self, decisions_dir: Path) -> None:
        self.decisions_dir = decisions_dir

    def record(self, approval: Approval) -> None:
        """Write `approval` to a new file in `decisions_dir`.

        The file name is `<safe gate id>--<UTC YYYYmmddTHHMMSSZ>-<6 hex>.yml`. The
        write is atomic (a temp file is written first, then moved into place with
        `os.replace`), and an existing file is never overwritten: on the vanishingly
        unlikely chance of a name collision, a fresh random suffix is tried again.
        """
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _safe_id(approval.gate_id)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        content = yaml.safe_dump(approval.model_dump(mode="json"), sort_keys=True)

        for _ in range(_MAX_NAME_ATTEMPTS):
            suffix = secrets.token_hex(3)
            target = self.decisions_dir / f"{safe_id}--{ts}-{suffix}.yml"
            if target.exists():
                continue

            fd, tmp_name = tempfile.mkstemp(
                dir=self.decisions_dir, prefix=".tmp-decision-", suffix=".yml"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(content)
                if target.exists():
                    tmp_path.unlink(missing_ok=True)
                    continue
                os.replace(tmp_path, target)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            return

        raise ReviewError(
            f"could not allocate a unique decision file name for gate {approval.gate_id!r} "
            f"in {self.decisions_dir}"
        )

    def approvals(self, gate_id: str) -> tuple[Approval, ...]:
        """Return every approval recorded for `gate_id`, sorted by `at` then file name.

        Every `*.yml`/`*.yaml` file in `decisions_dir` (except temp files left behind
        by an interrupted write, which start with `.`) is parsed. A file that is not
        valid YAML, not a mapping, or does not validate as an `Approval` raises
        `ReviewError` naming that file; it is never silently skipped.
        """
        if not self.decisions_dir.is_dir():
            return ()

        matches: list[tuple[Approval, str]] = []
        paths = sorted({*self.decisions_dir.glob("*.yml"), *self.decisions_dir.glob("*.yaml")})
        for path in paths:
            if path.name.startswith("."):
                continue
            try:
                raw = yaml.safe_load(path.read_text())
            except yaml.YAMLError as exc:
                raise ReviewError(f"malformed decision file {path}: invalid YAML: {exc}") from exc

            if not isinstance(raw, dict):
                raise ReviewError(f"malformed decision file {path}: not a YAML mapping")
            if raw.get("gate_id") != gate_id:
                continue

            try:
                approval = Approval.model_validate(raw)
            except ValidationError as exc:
                raise ReviewError(f"malformed decision file {path}: {exc}") from exc
            matches.append((approval, path.name))

        matches.sort(key=lambda pair: (pair[0].at, pair[1]))
        return tuple(approval for approval, _ in matches)
