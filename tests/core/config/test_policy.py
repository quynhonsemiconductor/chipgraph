"""Tests for `policy.local_plugins`: default, and the tighten-only rule across layers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import load


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _init_git(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


def test_policy_defaults_to_allow(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".chipgraph.yml", "project: demo\n")
    resolved = load(tmp_path)
    assert resolved is not None
    assert resolved.profile.policy.local_plugins == "allow"


def test_project_may_deny(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: demo
        policy: { local_plugins: deny }
        """,
    )
    resolved = load(tmp_path)
    assert resolved is not None
    assert resolved.profile.policy.local_plugins == "deny"


def test_org_deny_propagates_to_project(tmp_path: Path) -> None:
    _init_git(tmp_path)
    org = _write(
        tmp_path / "rules" / "org.yml",
        """\
        project: org-rules
        policy: { local_plugins: deny }
        """,
    )
    _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: demo
        extends: [path:rules/org.yml]
        """,
    )
    resolved = load(tmp_path)
    assert resolved is not None
    assert resolved.profile.policy.local_plugins == "deny"
    assert resolved.provenance["policy.local_plugins"].location == str(org)


def test_project_reallow_after_org_deny_is_error(tmp_path: Path) -> None:
    _init_git(tmp_path)
    org = _write(
        tmp_path / "rules" / "org.yml",
        """\
        project: org-rules
        policy: { local_plugins: deny }
        """,
    )
    project = _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: demo
        extends: [path:rules/org.yml]
        policy: { local_plugins: allow }
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load(tmp_path)
    message = str(exc.value)
    assert "tighten-only" in message
    assert str(org) in message
    assert exc.value.file == str(project)
    assert exc.value.key == "policy.local_plugins"


def test_deny_then_deny_is_fine(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "rules" / "org.yml",
        """\
        project: org-rules
        policy: { local_plugins: deny }
        """,
    )
    _write(
        tmp_path / ".chipgraph.yml",
        """\
        project: demo
        extends: [path:rules/org.yml]
        policy: { local_plugins: deny }
        """,
    )
    resolved = load(tmp_path)
    assert resolved is not None
    assert resolved.profile.policy.local_plugins == "deny"
