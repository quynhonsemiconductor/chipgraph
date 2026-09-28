"""`ConfigError`: raised for unreadable or invalid configuration, naming file/key/line."""

from __future__ import annotations

from pathlib import Path


class ConfigError(Exception):
    """An error loading or validating configuration.

    Always names the offending file; names the dotted key and, when known from a YAML
    node mark, the line, so a person can find and fix the problem without guessing.
    """

    def __init__(
        self,
        message: str,
        *,
        file: str | Path | None = None,
        key: str | None = None,
        line: int | None = None,
    ) -> None:
        self.file = str(file) if file is not None else None
        self.key = key
        self.line = line
        parts: list[str] = []
        if self.file is not None:
            parts.append(f"{self.file}:{line}" if line is not None else self.file)
        if key is not None:
            parts.append(f"key '{key}'")
        parts.append(message)
        super().__init__(": ".join(parts))
