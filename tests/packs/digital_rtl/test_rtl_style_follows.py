"""M2-04 accept: change the exemplars and the style follows, measured by the style check.

Two exemplar sets with clearly different styles (`docs/rtl-claude-code/exemplars/`):

- `a`: two-space indent, `begin` at the end of the line, asynchronous active-low reset,
  `_q`/`_d`, a comment header, ports padded so the types line up;
- `b`: four-space indent, `begin` on its own line, synchronous active-low reset,
  `_r`/`_next`, no header, unpadded ports, two blank lines between sections.

A scripted fake author takes the style profile of a set as its input and writes
`tiny_pwm` in that style. With every style key enforced, the style check passes the
module against its own set and flags it against the other one, in both directions; its
distance to its own set is smaller. (What a real model does is measured by the
acceptance script, `docs/rtl-claude-code/run.sh`.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from rtl_author_helpers import exemplars, profile_of, render_pwm

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks.style import StyleCheck, style_distance
from chipgraph.core.config.models import STYLE_KEYS
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.types import ToolContext

SETS = ("a", "b")


def _style_check(tmp_path: Path, module_text: str, exemplar_set: str) -> CheckResult:
    """Run the `style` check on `module_text` against `exemplar_set`, every key enforced."""
    root = tmp_path / f"against-{exemplar_set}"
    files = exemplars(exemplar_set)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    style = {"exemplars": list(files), "enforce": list(STYLE_KEYS)}
    (root / ".chipgraph.yml").write_text(yaml.safe_dump({"project": "p", "style": style}))
    (root / "rtl").mkdir()
    (root / "rtl" / "tiny_pwm.sv").write_text(module_text)
    spec = CheckSpec(id="style", capability="style", adapter="style", args={})
    ctx = ToolContext(
        repo_root=root, runner=LocalRunner(), params={"block": "pwm", "module": "tiny_pwm"}
    )
    plan = root / "plan" / "pwm.approved.json"
    plan.parent.mkdir()
    plan.write_text('{"modules": [{"name": "tiny_pwm", "writes": ["rtl/tiny_pwm.sv"]}]}')
    return asyncio.run(StyleCheck().run(spec, ctx))


@pytest.mark.parametrize("own", SETS)
def test_the_style_follows_the_exemplars(tmp_path: Path, own: str) -> None:
    other = next(s for s in SETS if s != own)
    module = render_pwm(profile_of(own))  # the fake author reads only the profile

    passed = _style_check(tmp_path, module, own)
    assert passed.status == "pass", [i.msg for i in passed.issues if i.severity == "error"]
    assert not [i for i in passed.issues if i.severity != "info"]

    flagged = _style_check(tmp_path, module, other)
    assert flagged.status == "fail"
    keys = {i.rule for i in flagged.issues if i.severity == "error"}
    assert {"style.indent", "style.begin", "style.reset", "style.suffix", "style.header"} <= keys

    assert style_distance(profile_of(own), module) < style_distance(profile_of(other), module)
    assert style_distance(profile_of(own), module) == 0.0


def test_the_two_modules_differ_only_by_the_exemplars() -> None:
    a, b = (render_pwm(profile_of(s)) for s in SETS)
    assert a != b
    assert "always_ff @(posedge clk or negedge rst_n) begin" in a and "count_q" in a
    assert "always_ff @(posedge clk)\n    begin" in b and "count_r" in b and "count_next" in b
