"""`VendorGuardCheck` (`vendor_guard`): vendored IP is never edited in place.

Third-party IP is copied into the repository so that a tape-out has a frozen, auditable
source; that holds only while the files match what was recorded. The check reads a
manifest in `sha256sum` format (`<sha256>  <path>` per line, paths relative to the
manifest's directory, `#` comments), so `shasum -a 256 -c MANIFEST.sha256` run in that
directory verifies the same thing, and fails when, under the manifest's directory:

- a listed file's content differs from its recorded hash (`vendor_guard.modified`);
- a listed file is gone (`vendor_guard.missing`);
- a file is not listed (`vendor_guard.unlisted`), except the manifest itself and the
  `allow` globs (e.g. a `patches/**` directory for reviewed changes kept apart).

An IP update is then a deliberate change: new files and a new manifest in the same
commit, reviewed as such. A project change to how an IP is used goes in its wrapper.

Args (all optional): `manifest` (default `vendor/MANIFEST.sha256`), `allow` (globs,
relative to the manifest's directory). Chip-wide: it runs once, not per block.

Profile example (`.chipgraph.yml`)::

    adapters:
      vendor_guard: { use: vendor_guard }
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    optional_list_of_str,
    optional_str,
    sha256_file,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

DEFAULT_MANIFEST = "vendor/MANIFEST.sha256"
_LINE_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")


class ManifestError(Exception):
    """The manifest cannot be read."""


def read_manifest(path: Path) -> dict[str, str]:
    """The manifest's entries: path (relative to its directory) -> sha256."""
    entries: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise ManifestError(f"{path.name}:{number}: not `<sha256>  <path>`: {raw!r}")
        rel = match.group(2).strip().removeprefix("./")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ManifestError(f"{path.name}:{number}: {rel!r} leaves the manifest's directory")
        if rel in entries:
            raise ManifestError(f"{path.name}:{number}: {rel} is listed twice")
        entries[rel] = match.group(1).lower()
    return entries


def write_manifest(directory: Path, manifest: str = "MANIFEST.sha256") -> str:
    """The manifest text for every file under `directory` (for a person updating an IP)."""
    lines = [
        f"{sha256_file(directory / rel)}  {rel}" for rel in _files(directory) if rel != manifest
    ]
    return "\n".join(lines) + "\n"


def _files(directory: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        here = Path(dirpath)
        out.extend((here / name).relative_to(directory).as_posix() for name in filenames)
    return sorted(out)


class VendorGuardCheck:
    """Fails when a file under the vendor manifest's directory differs from the manifest."""

    id = "vendor_guard"
    name = "Vendor guard"
    per_block = False

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        try:
            manifest_rel = optional_str(spec.args, "manifest", DEFAULT_MANIFEST)
            allow = [glob_to_regex(g) for g in optional_list_of_str(spec.args, "allow")]
        except ArgError as exc:
            return error_result(spec, str(exc), start)
        manifest = ctx.repo_root / manifest_rel
        base = manifest.parent
        base_rel = Path(manifest_rel).parent.as_posix()
        prefix = "" if base_rel in ("", ".") else base_rel + "/"
        if not manifest.is_file():
            issue = Issue(
                file=manifest_rel,
                rule="vendor_guard.no_manifest",
                msg=f"no vendor manifest at {manifest_rel}",
            )
            return self._result(spec, "error", [issue], start, [])
        try:
            entries = read_manifest(manifest)
        except (ManifestError, OSError, UnicodeDecodeError) as exc:
            issue = Issue(file=manifest_rel, rule="vendor_guard.manifest", msg=str(exc))
            return self._result(spec, "error", [issue], start, [])

        issues: list[Issue] = []
        hashes: list[tuple[str, str]] = [(manifest_rel, sha256_file(manifest))]
        for rel, recorded in sorted(entries.items()):
            path = base / rel
            if not path.is_file():
                issues.append(
                    Issue(
                        file=prefix + rel,
                        rule="vendor_guard.missing",
                        msg=f"{prefix + rel} is in {manifest_rel} but does not exist",
                    )
                )
                continue
            actual = sha256_file(path)
            hashes.append((prefix + rel, actual))
            if actual != recorded:
                issues.append(
                    Issue(
                        file=prefix + rel,
                        rule="vendor_guard.modified",
                        msg=(
                            f"{prefix + rel} differs from {manifest_rel}: vendored IP is never "
                            "edited in place. Revert it, and adapt the IP in its wrapper; an "
                            "IP update replaces its files and the manifest in one reviewed "
                            "commit"
                        ),
                    )
                )
        name = manifest.name
        for rel in _files(base):
            if rel == name or rel in entries or any(r.fullmatch(rel) for r in allow):
                continue
            issues.append(
                Issue(
                    file=prefix + rel,
                    rule="vendor_guard.unlisted",
                    msg=f"{prefix + rel} is not in {manifest_rel}: record it, or remove it",
                )
            )
        status: CheckStatus = "fail" if issues else "pass"
        return self._result(spec, status, issues, start, hashes)

    @staticmethod
    def _result(
        spec: CheckSpec,
        status: CheckStatus,
        issues: list[Issue],
        start: float,
        hashes: list[tuple[str, str]],
    ) -> CheckResult:
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=compute_idempotency_key(spec.id, spec.args, hashes),
        )


__all__ = [
    "DEFAULT_MANIFEST",
    "ManifestError",
    "VendorGuardCheck",
    "read_manifest",
    "write_manifest",
]
