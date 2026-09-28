"""Tool defaults: the first configuration layer, before org, preset, project or path layers."""

from __future__ import annotations

from typing import Any

DEFAULT_PROFILE: dict[str, Any] = {
    "runtime": "claude-code",
    "data": {"default": "internal"},
    "state": {"backend": "local"},
    "decisions": {"store": "repo"},
    "autonomy": {"spec": "L2", "rtl": "L3", "verify": "L3"},
}
"""Raw (pre-validation) defaults merged under every other layer. See DESIGN 8.3."""
