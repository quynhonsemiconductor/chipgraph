"""Shared fixtures for `chipgraph.mcp` tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


def init_git(root: Path) -> None:
    """Initialize a git repo at `root` (needed so `AppContext` finds a project root)."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def write_profile(root: Path, content: str) -> Path:
    """Write `content` as `root/.chipgraph.yml`."""
    path = root / ".chipgraph.yml"
    path.write_text(content, encoding="utf-8")
    return path


_PACK_MANIFEST = {"name": "demo", "version": "0.1.0", "provides": {"rules": ["rules"]}}

_GEN_CMD = [
    "python3",
    "-c",
    "import pathlib; pathlib.Path('out').mkdir(exist_ok=True); "
    "pathlib.Path('out/{block}.txt').write_text('hi')",
]


def write_gen_pack(root: Path, *, gated: bool = False) -> None:
    """A tiny `demo` pack with one `gen` rule (`gen_out`), foreach `blocks`.

    Writes `out/{block}.txt` for each block in the profile. With `gated=True`, the
    rule also gates on `spec:{block}` over a `spec/{block}.md` input.
    """
    pack_dir = root / ".chipgraph" / "packs" / "demo"
    (pack_dir / "rules").mkdir(parents=True)
    (pack_dir / "pack.yml").write_text(yaml.safe_dump(_PACK_MANIFEST))
    body: dict[str, object] = {
        "rule": "gen_out",
        "kind": "gen",
        "foreach": "blocks",
        "outputs": ["out/{block}.txt"],
        "run": {"use": "cmd", "args": {"cmd": _GEN_CMD}},
    }
    if gated:
        body["inputs"] = [{"path": "spec/{block}.md"}]
        body["gate"] = "spec:{block}"
    (pack_dir / "rules" / "gen_out.yml").write_text(yaml.safe_dump(body))
