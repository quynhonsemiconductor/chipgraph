"""`VendorGuardCheck` (`vendor_guard`): vendored IP is never edited in place.

Third-party IP is copied into the repository so that a tape-out has a frozen, auditable
source; that holds only while the files match what was recorded. The check reads a
manifest in `sha256sum` format (`<sha256>  <path>` per line, paths relative to the
manifest's directory, `#` comments), so `shasum -a 256 -c MANIFEST.sha256` run in that
directory verifies the same thing, and fails when, under the manifest's directory:

- a listed file's content differs from its recorded hash (`vendor_guard.modified`);
- a listed file is gone, or is not a regular file (`vendor_guard.missing`);
- a file is not listed (`vendor_guard.unlisted`), except the manifest itself and the
  `allow` globs (e.g. a `patches/**` directory for reviewed changes kept apart);
- a file or directory is a symbolic link, or a listed path resolves outside the
  manifest's directory (`vendor_guard.symlink`): a link would let the guarded tree
  point at files the manifest does not cover. The manifest itself, and every directory
  on the way to it, must not be a link either.

It never passes on what it could not read: an unreadable file or directory is an
`error` (`vendor_guard.unreadable`). Only the `.git` directory directly under the
manifest's directory is skipped; one deeper is walked like any other directory. Two
manifest entries that differ only in case (one file on a case-insensitive file system)
are refused.

An IP update is then a deliberate change: new files and a new manifest in the same
commit, reviewed as such. A project change to how an IP is used goes in its wrapper.

Args (all optional): `manifest` (default `vendor/MANIFEST.sha256`; a repo-relative
path, no `..`, inside the repository), `allow` (globs, relative to the manifest's
directory). Chip-wide: it runs once, not per block.

Profile example (`.chipgraph.yml`)::

    adapters:
      vendor_guard: { use: vendor_guard }
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    optional_list_of_str,
    optional_str,
)
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

DEFAULT_MANIFEST = "vendor/MANIFEST.sha256"
_LINE_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CHUNK = 1 << 20


class ManifestError(Exception):
    """The manifest cannot be read."""


def read_manifest(path: Path) -> dict[str, str]:
    """The manifest's entries: path (relative to its directory) -> sha256.

    Errors name the line number only, never quote the line."""
    return parse_manifest(_read_regular(path).decode("utf-8"), path.name)


def parse_manifest(text: str, name: str) -> dict[str, str]:
    """The entries of the manifest text `text` (`name` is its file name, for errors)."""
    entries: dict[str, str] = {}
    folded: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise ManifestError(f"{name}:{number}: not `<sha256>  <path>`")
        rel = match.group(2).strip().removeprefix("./")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise ManifestError(f"{name}:{number}: the path leaves the manifest's directory")
        if "\x00" in rel:
            raise ManifestError(f"{name}:{number}: the path holds a NUL byte")
        if rel in entries:
            raise ManifestError(f"{name}:{number}: {rel!r} is listed twice")
        other = folded.get(rel.casefold())
        if other is not None:
            raise ManifestError(
                f"{name}:{number}: {rel!r} and {other!r} differ only in case (one file on a "
                "case-insensitive file system): list it once"
            )
        entries[rel] = match.group(1).lower()
        folded[rel.casefold()] = rel
    return entries


def _why(exc: OSError) -> str:
    return exc.strerror or str(exc)


def write_manifest(directory: Path, manifest: str = "MANIFEST.sha256") -> str:
    """The manifest text for every file under `directory` (for a person updating an IP).

    Raises `ManifestError` when the tree holds a link or something unreadable."""
    tree = _walk(directory)
    if tree.links or tree.unreadable:
        bad = ", ".join([*tree.links, *tree.unreadable])
        raise ManifestError(f"cannot record {directory}: links or unreadable entries: {bad}")
    lines = [f"{_sha256_regular(directory / rel)}  {rel}" for rel in tree.files if rel != manifest]
    return "\n".join(lines) + "\n"


@dataclass
class _Tree:
    files: list[str] = field(default_factory=list)
    """Regular files (and other non-directory, non-link entries), relative, sorted."""
    links: list[str] = field(default_factory=list)
    """Symbolic links, to files or directories (never followed)."""
    unreadable: list[str] = field(default_factory=list)
    """Directories that could not be listed."""


def _walk(directory: Path) -> _Tree:
    tree = _Tree()

    def _error(exc: OSError) -> None:
        where = Path(exc.filename) if exc.filename else directory
        try:
            rel = where.relative_to(directory).as_posix()
        except ValueError:
            rel = where.as_posix()
        tree.unreadable.append(rel or ".")

    for dirpath, dirnames, filenames in os.walk(directory, onerror=_error):
        here = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if here == directory and name == ".git":
                continue
            rel = (here / name).relative_to(directory).as_posix()
            if (here / name).is_symlink():
                tree.links.append(rel)
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = (here / name).relative_to(directory).as_posix()
            (tree.links if (here / name).is_symlink() else tree.files).append(rel)
    tree.files.sort()
    tree.links.sort()
    tree.unreadable.sort()
    return tree


def _read_regular(path: Path) -> bytes:
    """The bytes of the regular file `path`, never through a link (`OSError` otherwise)."""
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        chunks = []
        while chunk := os.read(fd, _CHUNK):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha256_regular(path: Path) -> str:
    return hashlib.sha256(_read_regular(path)).hexdigest()


def _manifest_rel(value: str) -> str:
    """`value` as a normalised repo-relative POSIX path (`ArgError` if it is not one)."""
    text = value.replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or ".." in parts or (len(text) > 1 and text[1] == ":"):
        raise ArgError("args['manifest'] must be a repo-relative path without '..'")
    return "/".join(parts)


def _linked_part(root: Path, rel: str) -> str | None:
    """The first component of `rel` under `root` that is a symbolic link, if any."""
    current = root
    for part in rel.split("/"):
        current = current / part
        if current.is_symlink():
            return current.relative_to(root).as_posix()
    return None


class VendorGuardCheck:
    """Fails when a file under the vendor manifest's directory differs from the manifest."""

    id = "vendor_guard"
    name = "Vendor guard"
    per_block = False

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        try:
            manifest_rel = _manifest_rel(optional_str(spec.args, "manifest", DEFAULT_MANIFEST))
            allow = [glob_to_regex(g) for g in optional_list_of_str(spec.args, "allow")]
        except ArgError as exc:
            return error_result(spec, str(exc), start)
        root = ctx.repo_root
        manifest = root / manifest_rel
        base = manifest.parent
        base_rel = Path(manifest_rel).parent.as_posix()
        prefix = "" if base_rel in ("", ".") else base_rel + "/"
        linked = _linked_part(root, manifest_rel)
        if linked is not None:
            issue = Issue(
                file=linked,
                rule="vendor_guard.symlink",
                msg=f"{linked} is a symbolic link: the manifest and its directory must be real",
            )
            return self._result(spec, "error", [issue], start, [])
        if not Path(os.path.realpath(manifest)).is_relative_to(Path(os.path.realpath(root))):
            issue = Issue(rule="vendor_guard.manifest", msg="the manifest is outside the repo")
            return self._result(spec, "error", [issue], start, [])
        if not manifest.is_file():
            issue = Issue(
                file=manifest_rel,
                rule="vendor_guard.no_manifest",
                msg=f"no vendor manifest at {manifest_rel}",
            )
            return self._result(spec, "error", [issue], start, [])
        try:
            manifest_bytes = _read_regular(manifest)
            entries = parse_manifest(manifest_bytes.decode("utf-8"), manifest.name)
        except ManifestError as exc:
            issue = Issue(file=manifest_rel, rule="vendor_guard.manifest", msg=str(exc))
            return self._result(spec, "error", [issue], start, [])
        except (OSError, UnicodeDecodeError) as exc:
            why = "not UTF-8" if isinstance(exc, UnicodeDecodeError) else _why(exc)
            issue = Issue(
                file=manifest_rel,
                rule="vendor_guard.manifest",
                msg=f"cannot read {manifest_rel}: {why}",
            )
            return self._result(spec, "error", [issue], start, [])

        tree = _walk(base)
        issues: list[Issue] = []
        infra = False
        hashes: list[tuple[str, str]] = [(manifest_rel, hashlib.sha256(manifest_bytes).hexdigest())]
        for rel in tree.links:
            hashes.append((prefix + rel, "symlink"))
            issues.append(
                Issue(
                    file=prefix + rel,
                    rule="vendor_guard.symlink",
                    msg=(
                        f"{prefix + rel} is a symbolic link: vendored IP is real files, so "
                        "the manifest covers what is built; replace the link by the files"
                    ),
                )
            )
        for rel in tree.unreadable:
            infra = True
            hashes.append((prefix + rel, "unreadable"))
            issues.append(
                Issue(
                    file=prefix + rel,
                    rule="vendor_guard.unreadable",
                    msg=f"cannot list {prefix + rel}: its files cannot be verified",
                )
            )
        real_base = Path(os.path.realpath(base))
        for rel, recorded in sorted(entries.items()):
            path = base / rel
            link = _linked_part(base, rel)
            if link is not None or not Path(os.path.realpath(path)).is_relative_to(real_base):
                if link is None or link not in tree.links:
                    issues.append(
                        Issue(
                            file=prefix + rel,
                            rule="vendor_guard.symlink",
                            msg=f"{prefix + rel} resolves outside {base_rel or '.'}",
                        )
                    )
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise FileNotFoundError(rel)  # a FIFO, a directory...: not the file
                actual = _sha256_regular(path)
            except (FileNotFoundError, ValueError):
                issues.append(
                    Issue(
                        file=prefix + rel,
                        rule="vendor_guard.missing",
                        msg=f"{prefix + rel} is in {manifest_rel} but does not exist",
                    )
                )
                continue
            except OSError as exc:
                infra = True
                hashes.append((prefix + rel, "unreadable"))
                issues.append(
                    Issue(
                        file=prefix + rel,
                        rule="vendor_guard.unreadable",
                        msg=f"cannot read {prefix + rel} ({_why(exc)}): it cannot be verified",
                    )
                )
                continue
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
        for rel in tree.files:
            if rel == name or rel in entries or any(r.fullmatch(rel) for r in allow):
                continue
            hashes.append((prefix + rel, "unlisted"))
            issues.append(
                Issue(
                    file=prefix + rel,
                    rule="vendor_guard.unlisted",
                    msg=f"{prefix + rel} is not in {manifest_rel}: record it, or remove it",
                )
            )
        status: CheckStatus = "error" if infra else "fail" if issues else "pass"
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
    "parse_manifest",
    "read_manifest",
    "write_manifest",
]
