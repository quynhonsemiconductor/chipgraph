"""Tests for the ``agents-md`` generator (task M1-15): rendering and template overrides.

Snapshots live in ``agents_md_snapshots/``; regenerate them with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/packs/spec_core/test_agents_md.py
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from chipgraph.core.config import ResolvedProfile, load
from chipgraph.packs.spec_core.gen.agents_md import (
    BEGIN_MARKER,
    END_MARKER,
    NOTE,
    AgentsMdError,
    agents_md_content,
    render_agents_md,
)

_REPO = Path(__file__).resolve().parents[3]
_SNAPSHOTS = Path(__file__).resolve().parent / "agents_md_snapshots"
_TINYSOC = _REPO / "examples" / "tinysoc"
_QSOC_PROFILE = _REPO / "docs" / "examples" / "qsoc.chipgraph.yml"

# A profile with every kind of data the block has a section for: org naming rules
# (`extends: [org:qnsc]`), per-block layout/adapters/paths/requirements, path exceptions,
# NDA paths and generated files.
_FULL_PROFILE = """\
project: demo
extends: ["org:qnsc"]
packs: [spec_core]
target: { kind: asic, pdk: gf180mcu }
spec:
  chip: { path: chip.yml, format: chip-yaml }
  ip_dir: doc/ip
  requirements: { id_pattern: 'REQ-{BLOCK}-\\d+' }
layout:
  rtl: "design/{block}/rtl/{module}.sv"
  filelist: "design/{block}/{block}.f"
  generated: ["design/top/rtl/demo_pkg.sv", "doc/specs/docx/**"]
  vendor: "vendor/**"
adapters:
  lint: { use: cmd, cmd: ["make", "lint", "BLOCK={block}"] }
  naming: { use: naming, rules: "org:qnsc/naming-v1.yml", scope: ["design/**/*.sv"] }
  regs: { use: generated, files: ["design/*/rtl/*_regs.sv"] }
  trace: { use: trace, tests: ["dv/**/*.py"], severity: warning }
data:
  nda_paths: ["vendor/pdk/**", "doc/nda/**"]
  nda_model: local
paths:
  "hw/legacy/**": { checks: { naming: off }, reason: "old code from chip v1 | kept as is" }
  "vendor/**": { checks: { naming: off, header: off }, write: deny, reason: "vendor IP" }
blocks:
  uart:
    instances: [uart_0, uart_1]
    adapters: { lint: { use: cmd, cmd: "make lint-uart", severity: info } }
    paths: { "design/uart/rtl/old/**": { write: deny, reason: "frozen <v1> RTL" } }
  rom:
    layout: { spec: ["doc/specs/ROM_MAS.md", "doc/specs/BOOT.md"] }
    spec: { requirements: { id_pattern: '(?:ROM|BOOT)_\\d{3}', infer: verification } }
  top: {}
"""


def _load(profile: Path, root: Path, tmp_path: Path) -> ResolvedProfile:
    resolved = load(root, profile_path=profile, user_config=tmp_path / "no-user.yml")
    assert resolved is not None
    return resolved


def _tinysoc(tmp_path: Path) -> tuple[ResolvedProfile, Path]:
    return _load(_TINYSOC / ".chipgraph.yml", _TINYSOC, tmp_path), _TINYSOC


def _qsoc(tmp_path: Path) -> tuple[ResolvedProfile, Path]:
    return _load(_QSOC_PROFILE, tmp_path, tmp_path), tmp_path


def _full(tmp_path: Path, extra: str = "") -> tuple[ResolvedProfile, Path]:
    root = tmp_path / "full"
    root.mkdir(exist_ok=True)
    profile = root / ".chipgraph.yml"
    profile.write_text(_FULL_PROFILE + extra, encoding="utf-8")
    return _load(profile, root, tmp_path), root


_SOURCES = {"tinysoc": _tinysoc, "qsoc": _qsoc, "full": _full}


def _sections(block: str) -> dict[str, str]:
    """The block split at its `### ` headings: {heading or '' for the preamble: text}."""
    parts = re.split(r"(?m)^(?=### )", block)
    out: dict[str, str] = {}
    for part in parts:
        heading = part.splitlines()[0] if part.startswith("### ") else ""
        out[heading] = part
    return out


# --------------------------------------------------------------------------------------
# snapshots and determinism
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_snapshot_byte_identical(source: str, tmp_path: Path) -> None:
    """tinysoc, the QSoC sample profile and a full profile render their snapshot exactly."""
    resolved, root = _SOURCES[source](tmp_path)
    block = render_agents_md(resolved, root)
    snapshot = _SNAPSHOTS / f"{source}.md"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(block.encode("utf-8"))
    assert block.encode("utf-8") == snapshot.read_bytes(), f"{source} drifted from its snapshot"


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_regenerating_gives_no_diff(source: str, tmp_path: Path) -> None:
    """Rendering again, and regenerating a file already generated, changes no byte."""
    resolved, root = _SOURCES[source](tmp_path)
    first = agents_md_content(None, resolved, root)
    assert render_agents_md(resolved, root) == render_agents_md(resolved, root)
    assert agents_md_content(first, resolved, root) == first
    again, _ = _SOURCES[source](tmp_path)
    assert agents_md_content(first, again, root) == first


@pytest.mark.parametrize("source", sorted(_SOURCES))
def test_block_is_clean_text(source: str, tmp_path: Path) -> None:
    """`\\n` endings, no trailing spaces, no absolute path, no date, the note first."""
    resolved, root = _SOURCES[source](tmp_path)
    block = render_agents_md(resolved, root)
    assert block.startswith(NOTE + "\n\n")
    assert block.endswith("\n") and not block.endswith("\n\n")
    assert "\r" not in block
    assert BEGIN_MARKER not in block and END_MARKER not in block
    assert all(line == line.rstrip() for line in block.split("\n"))
    assert str(_REPO) not in block and str(tmp_path) not in block
    assert re.search(r"\b20\d\d-\d\d-\d\d\b", block) is None
    assert "\n\n\n" not in block


def test_order_of_profile_keys_does_not_matter(tmp_path: Path) -> None:
    """Blocks, adapters, layout and paths are sorted: reordering the YAML changes nothing."""
    resolved, root = _full(tmp_path)
    reordered = tmp_path / "reordered"
    reordered.mkdir()
    profile = (root / ".chipgraph.yml").read_text(encoding="utf-8")
    # Move the `uart` block after `top`: a pure reordering of a mapping.
    head, blocks = profile.split("blocks:\n")
    uart, rest = blocks.split("  rom:\n")
    (reordered / ".chipgraph.yml").write_text(
        f"{head}blocks:\n  rom:\n{rest}{uart}", encoding="utf-8"
    )
    other = _load(reordered / ".chipgraph.yml", reordered, tmp_path)
    assert render_agents_md(other, reordered) == render_agents_md(resolved, root)


# --------------------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------------------


def test_full_profile_has_every_section(tmp_path: Path) -> None:
    resolved, root = _full(tmp_path)
    sections = _sections(render_agents_md(resolved, root))
    assert sorted(h for h in sections if h) == sorted(
        [
            "### Project and blocks",
            "### Where files go",
            "### Naming",
            "### Before you open a PR",
            "### Requirement IDs",
            "### Path exceptions",
            "### Data labels",
            "### Generated files",
        ]
    )
    naming = sections["### Naming"]
    # From the org rules file, resolved with `resolve_data_ref`: kind, pattern, id, message.
    assert "From `org:qnsc/naming-v1.yml` (QNSC_RTL_Design_Naming_Rule v1.1)" in naming
    assert "| port | `^(?:i_\\|o_\\|io_)[a-z0-9_]+$` | 1.2 port prefix | must start with" in naming
    assert "m_qnsc_\\<function>" in naming  # `<` escaped, not read as an HTML tag
    checks = sections["### Before you open a PR"]
    for command in ("chipgraph ingest", "chipgraph check", "chipgraph audit"):
        assert command in checks
    assert "| `trace` | trace | warning | - |" in checks
    assert "| `lint` | cmd | - | `make lint BLOCK={block}` |" in checks
    assert "| `uart` | `lint` | cmd | info | `make lint-uart` |" in checks
    blocks = sections["### Project and blocks"]
    assert "| `uart` | `uart_0`, `uart_1` |" in blocks
    assert "PDK `gf180mcu`" in blocks
    reqs = sections["### Requirement IDs"]
    assert "`REQ-{BLOCK}-\\d+`" in reqs
    assert "| `rom` | `(?:ROM\\|BOOT)_\\d{3}` | yes |" in reqs
    paths = sections["### Path exceptions"]
    assert "old code from chip v1 \\| kept as is" in paths
    assert "| `vendor/**` | - | header off, naming off | deny | vendor IP |" in paths
    assert "| `design/uart/rtl/old/**` | `uart` | - | deny | frozen \\<v1> RTL |" in paths
    data = sections["### Data labels"]
    assert "cloud model" in data and "`nda_model: local`" in data
    assert data.index("`doc/nda/**`") < data.index("`vendor/pdk/**`")
    generated = sections["### Generated files"]
    for glob in ("design/*/rtl/*_regs.sv", "design/top/rtl/demo_pkg.sv", "doc/specs/docx/**"):
        assert f"- `{glob}`" in generated


def test_sections_without_data_are_left_out(tmp_path: Path) -> None:
    """tinysoc has no naming rules, paths, NDA paths or generated files: no such section."""
    resolved, root = _tinysoc(tmp_path)
    headings = [h for h in _sections(render_agents_md(resolved, root)) if h]
    assert headings == [
        "### Project and blocks",
        "### Where files go",
        "### Before you open a PR",
        "### Requirement IDs",
    ]


def test_minimal_profile_renders(tmp_path: Path) -> None:
    (tmp_path / ".chipgraph.yml").write_text("project: bare\n", encoding="utf-8")
    resolved = _load(tmp_path / ".chipgraph.yml", tmp_path, tmp_path)
    block = render_agents_md(resolved, tmp_path)
    assert "- Project: `bare`" in block
    assert "| Block |" not in block and "Configured checks" not in block


def test_missing_naming_rules_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / ".chipgraph.yml").write_text(
        "project: p\nnaming: { rules: rules/missing.yml }\n", encoding="utf-8"
    )
    resolved = _load(tmp_path / ".chipgraph.yml", tmp_path, tmp_path)
    with pytest.raises(AgentsMdError, match=r"naming rules 'rules/missing.yml'"):
        render_agents_md(resolved, tmp_path)


def test_project_naming_rules_resolve_against_root(tmp_path: Path) -> None:
    rules = (_REPO / "src" / "chipgraph" / "orgs" / "qnsc" / "naming-v1.yml").read_text()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "naming.yml").write_text(
        rules.replace("QNSC_RTL_Design_Naming_Rule", "Team_Rule"), encoding="utf-8"
    )
    (tmp_path / ".chipgraph.yml").write_text(
        "project: p\nnaming: { rules: rules/naming.yml }\n", encoding="utf-8"
    )
    resolved = _load(tmp_path / ".chipgraph.yml", tmp_path, tmp_path)
    assert "From `rules/naming.yml` (Team_Rule v1.1)" in render_agents_md(resolved, tmp_path)


# --------------------------------------------------------------------------------------
# template overrides (DESIGN 8.6 V2)
# --------------------------------------------------------------------------------------


def test_block_override_changes_only_that_section(tmp_path: Path) -> None:
    base, root = _full(tmp_path)
    before = render_agents_md(base, root)

    (root / "templates").mkdir()
    (root / "templates" / "checks.j2").write_text(
        "{% block agents_md_checks %}\n### Before you open a PR\n\n"
        "Run `make check` ({{ checks | length }} checks).\n{% endblock %}\n",
        encoding="utf-8",
    )
    overridden, _ = _full(
        tmp_path, "templates: { override: { agents_md_checks: templates/checks.j2 } }\n"
    )
    after = render_agents_md(overridden, root)

    old, new = _sections(before), _sections(after)
    assert list(old) == list(new)
    heading = "### Before you open a PR"
    assert new[heading] == f"{heading}\n\nRun `make check` (4 checks).\n\n"
    for name in old:
        if name != heading:
            assert new[name] == old[name], name


def test_whole_file_override_and_unrelated_overrides(tmp_path: Path) -> None:
    """A whole-file override replaces the pack template; other templates' overrides
    (e.g. an RTL header that does not exist here) are not read."""
    root = tmp_path / "full"
    root.mkdir()
    (root / "my_agents.j2").write_text("## Ours\n\nProject {{ project.name }}.\n")
    resolved, _ = _full(
        tmp_path,
        "templates:\n  override:\n    agents_md.md.j2: my_agents.j2\n"
        "    rtl_header: templates/missing_header.j2\n",
    )
    assert render_agents_md(resolved, root) == f"{NOTE}\n\n## Ours\n\nProject demo.\n"


def test_broken_override_is_a_clear_error(tmp_path: Path) -> None:
    resolved, root = _full(
        tmp_path, "templates: { override: { agents_md_layout: templates/nope.j2 } }\n"
    )
    with pytest.raises(AgentsMdError, match=r"cannot render the AGENTS\.md block"):
        render_agents_md(resolved, root)


def test_override_emitting_markers_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "full"
    root.mkdir()
    (root / "bad.j2").write_text(
        "{% block agents_md_intro %}\n<!-- chipgraph:end -->\n{% endblock %}\n"
    )
    resolved, _ = _full(tmp_path, "templates: { override: { agents_md_intro: bad.j2 } }\n")
    with pytest.raises(AgentsMdError, match="markers"):
        render_agents_md(resolved, root)


def test_tinysoc_copy_matches_in_place(tmp_path: Path) -> None:
    """The block does not depend on where the project lives on disk."""
    copy = tmp_path / "tinysoc"
    shutil.copytree(_TINYSOC, copy)
    moved = _load(copy / ".chipgraph.yml", copy, tmp_path)
    resolved, root = _tinysoc(tmp_path)
    assert render_agents_md(moved, copy) == render_agents_md(resolved, root)
