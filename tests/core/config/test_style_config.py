"""M2-04: the profile's `style` section: `style.enforce` and what `config check` says."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from chipgraph.core.config import STYLE_KEYS, StyleCfg
from chipgraph.core.config.loader import load


def test_enforce_takes_the_style_keys_only() -> None:
    cfg = StyleCfg.model_validate({"exemplars": ["rtl/a.sv"], "enforce": ["indent", "reset"]})
    assert cfg.enforce == ("indent", "reset")
    assert StyleCfg().enforce == ()
    assert set(STYLE_KEYS) >= {"indent", "begin", "ports", "reset", "suffix", "case"}
    with pytest.raises(ValidationError):
        StyleCfg.model_validate({"enforce": ["indentation"]})


def _check(tmp_path: Path, style: dict[str, object]) -> list[tuple[str, str, str]]:
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / ".chipgraph.yml").write_text(yaml.safe_dump({"project": "p", "style": style}))
    resolved = load(tmp_path, user_config=tmp_path / "none.yml")
    assert resolved is not None
    return [(i.severity, i.key, i.message) for i in resolved.check() if i.key.startswith("style")]


def test_config_check_on_the_style_section(tmp_path: Path) -> None:
    assert _check(tmp_path / "ok", {"exemplars": ["rtl/a.sv"], "enforce": ["indent"]}) == []
    [nothing] = _check(tmp_path / "empty", {"enforce": ["indent"]})
    assert nothing[:2] == ("warning", "style.enforce") and "nothing" in nothing[2]
    [twice] = _check(tmp_path / "twice", {"exemplars": ["rtl/a.sv", "rtl/a.sv"]})
    assert twice[:2] == ("warning", "style.exemplars") and "rtl/a.sv" in twice[2]
