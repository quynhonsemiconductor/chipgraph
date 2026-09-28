"""Shared path validation for artifact and rule-output paths.

Not part of the public API of this package (no leading re-export in ``__init__``).
"""

from __future__ import annotations


def validate_relative_path(path: str, *, field_name: str = "path") -> str:
    """Validate a POSIX path relative to a repo root; return it unchanged if valid.

    The path must not be empty, must not start with ``/`` (absolute), must not
    contain backslashes, and no segment may be ``.`` or ``..``. Placeholders such
    as ``{block}`` are allowed inside segments (used by rule output templates).
    """
    if path == "":
        raise ValueError(f"{field_name} must not be empty")
    if path.startswith("/"):
        raise ValueError(f"{field_name} must be relative, got absolute path {path!r}")
    if "\\" in path:
        raise ValueError(f"{field_name} must be a POSIX path, got {path!r}")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValueError(f"{field_name} must not contain '.', '..' or empty segments: {path!r}")
    return path
