"""Shared helpers for built-in checks: template/glob matching, file walking, hashing, timing.

Nothing here knows about a specific check; `layout.py`, `filelist.py` and `generated.py`
build on top of this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipgraph.core.contracts import CheckResult, CheckSpec, Issue

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class ArgError(Exception):
    """Raised for invalid `CheckSpec.args`; the caller turns this into an `error` result."""


def require_str(args: Mapping[str, Any], key: str) -> str:
    """Return `args[key]` as a string, or raise `ArgError`."""
    if key not in args:
        raise ArgError(f"args[{key!r}] is required")
    val = args[key]
    if not isinstance(val, str):
        raise ArgError(f"args[{key!r}] must be a string")
    return val


def require_dict(args: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return `args[key]` as a dict, or raise `ArgError`."""
    if key not in args:
        raise ArgError(f"args[{key!r}] is required")
    val = args[key]
    if not isinstance(val, dict):
        raise ArgError(f"args[{key!r}] must be a mapping")
    return val


def optional_bool(args: Mapping[str, Any], key: str, default: bool) -> bool:
    """Return `args[key]` as a bool, defaulting to `default`, or raise `ArgError`."""
    val = args.get(key, default)
    if not isinstance(val, bool):
        raise ArgError(f"args[{key!r}] must be a boolean")
    return val


def optional_str(args: Mapping[str, Any], key: str, default: str) -> str:
    """Return `args[key]` as a string, defaulting to `default`, or raise `ArgError`."""
    val = args.get(key, default)
    if not isinstance(val, str):
        raise ArgError(f"args[{key!r}] must be a string")
    return val


def require_list_of_str(args: Mapping[str, Any], key: str) -> list[str]:
    """Return `args[key]` as a list of strings, or raise `ArgError`."""
    if key not in args:
        raise ArgError(f"args[{key!r}] is required")
    return _as_list_of_str(args[key], key)


def optional_list_of_str(args: Mapping[str, Any], key: str) -> list[str]:
    """Return `args[key]` as a list of strings, defaulting to `[]`, or raise `ArgError`."""
    if key not in args:
        return []
    return _as_list_of_str(args[key], key)


def _as_list_of_str(val: Any, key: str) -> list[str]:
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ArgError(f"args[{key!r}] must be a list of strings")
    return list(val)


def str_or_list_of_str(val: Any, where: str) -> list[str]:
    """Normalize a `str | list[str]` value into a list of strings, or raise `ArgError`."""
    if isinstance(val, str):
        return [val]
    if isinstance(val, list) and all(isinstance(v, str) for v in val):
        return list(val)
    raise ArgError(f"{where} must be a string or a list of strings")


# ---------------------------------------------------------------------------
# Template and glob matching
#
# A template is a repo-relative path pattern with two kinds of wildcards:
#   - `{name}` matches exactly one path segment (or part of one); repeating the same
#     name later in the template requires the same value (a backreference).
#   - `**` (as a whole path segment, e.g. `a/**/b.sv` or `vendor/**`) matches any depth,
#     including zero.
#   - `*` and `?` behave as usual within a single path segment.
# ---------------------------------------------------------------------------

_PLACEHOLDER_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
_GLOB_SPECIAL = re.compile(r"(\*\*/|/\*\*|\*\*|\*|\?)")


def _sanitize(name: str) -> str:
    """A valid, collision-resistant Python regex group name for placeholder `name`."""
    return "p_" + name.replace(".", "_")


def _glob_literal_to_regex(text: str) -> str:
    """Translate glob wildcards (`**`, `*`, `?`) in a literal chunk of text to a regex body."""
    parts = _GLOB_SPECIAL.split(text)
    out: list[str] = []
    for part in parts:
        if part == "**/":
            out.append("(?:.*/)?")
        elif part == "/**":
            out.append("(?:/.*)?")
        elif part == "**":
            out.append(".*")
        elif part == "*":
            out.append("[^/]*")
        elif part == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(part))
    return "".join(out)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern (`**`, `*`, `?`) into a regex matching a whole repo-relative path."""
    return re.compile("^" + _glob_literal_to_regex(pattern) + "$")


@dataclass(frozen=True)
class CompiledTemplate:
    """A path template compiled to a regex, with its placeholder names in first-seen order."""

    regex: re.Pattern[str]
    names: tuple[str, ...]

    def match(self, path: str) -> dict[str, str] | None:
        """Match `path` (a repo-relative POSIX path) against this template.

        Returns a mapping of placeholder name to captured value, or `None` if `path`
        does not match.
        """
        m = self.regex.fullmatch(path)
        if m is None:
            return None
        groups = m.groupdict()
        return {name: groups[_sanitize(name)] for name in self.names}


def compile_template(template: str) -> CompiledTemplate:
    """Compile a path template (placeholders, `**`, `*`, `?`) into a `CompiledTemplate`."""
    tokens = _PLACEHOLDER_TOKEN.split(template)
    seen: set[str] = set()
    order: list[str] = []
    parts: list[str] = []
    is_placeholder = False
    for tok in tokens:
        if is_placeholder:
            sanitized = _sanitize(tok)
            if tok in seen:
                parts.append(f"(?P={sanitized})")
            else:
                seen.add(tok)
                order.append(tok)
                parts.append(f"(?P<{sanitized}>[^/]+)")
        else:
            parts.append(_glob_literal_to_regex(tok))
        is_placeholder = not is_placeholder
    regex = re.compile("^" + "".join(parts) + "$")
    return CompiledTemplate(regex=regex, names=tuple(order))


# ---------------------------------------------------------------------------
# Repo file walking
# ---------------------------------------------------------------------------

_ALWAYS_SKIP_DIR = ".git"
_STATE_DIR = (".chipgraph", "state")


def iter_repo_files(root: Path) -> list[str]:
    """List every file under `root` as a repo-relative POSIX path, sorted.

    Always skips `.git` and `.chipgraph/state`, regardless of check configuration.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        rel_parts = () if rel_dir == Path() else rel_dir.parts
        dirnames[:] = [
            d for d in dirnames if d != _ALWAYS_SKIP_DIR and (*rel_parts, d) != _STATE_DIR
        ]
        for filename in filenames:
            rel = "/".join((*rel_parts, filename)) if rel_parts else filename
            out.append(rel)
    return sorted(out)


def norm_path(path: Path) -> Path:
    """Normalize `path` (collapse `..`/`.`) without resolving symlinks."""
    return Path(os.path.normpath(str(path)))


def to_repo_rel(path: Path, root: Path) -> str | None:
    """Return `path` as a repo-relative POSIX string, or `None` if it is outside `root`."""
    try:
        return norm_path(path).relative_to(norm_path(root)).as_posix()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Hashing and idempotency
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the hex sha256 digest of the content of the file at `path`."""
    return sha256_bytes(path.read_bytes())


def canonical_json(obj: Any) -> str:
    """Render `obj` as canonical JSON (sorted keys, no extra whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_idempotency_key(
    check_id: str,
    args: Mapping[str, Any],
    file_hashes: Iterable[tuple[str, str]],
) -> str:
    """A stable idempotency key over the check id, its args, and the files it read.

    `file_hashes` is an iterable of `(repo_relative_path, content_sha256)` pairs; it is
    sorted here so caller order does not matter.
    """
    payload = {
        "check_id": check_id,
        "args": args,
        "files": sorted(file_hashes),
    }
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def error_result(spec: CheckSpec, msg: str, start: float, *, rule: str = "args") -> CheckResult:
    """Build an `error` `CheckResult` for a bad-args failure, timed from `start`."""
    return CheckResult(
        check_id=spec.id,
        status="error",
        issues=(Issue(msg=msg, severity="error", rule=rule),),
        duration_s=time.monotonic() - start,
        idempotency_key=compute_idempotency_key(spec.id, spec.args, ()),
    )
