"""M2-11a: where a block's RTL and testbench go comes from the profile's layout.

- `layout_dir(profile, block, kind)`: the directory part of the block's first `kind`
  template (QSoC's `design/{block}/rtl/*.sv` gives `design/pwm/rtl`), a clear error when
  the layout lacks the key; the built-in rules fall back to `rtl/` and `dv/{block}/`
  for a project with no key;
- used by `plan_check` (the paths a plan may write), the planner's context, the
  `plan.modules` resolver (the entries' `writes`), `rtl_module` and `tb_module` (their
  outputs): on a QSoC-style layout a plan for a block passes `plan_check` with writes under
  `design/<block>/rtl/`, and the nodes get those outputs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from plan_helpers import approve_plan
from rtl_target_helpers import (
    EXPAND,
    TB,
    ChainRuntime,
    module_id,
    planned_timer,
    register,
    target_project,
)

from chipgraph.app.build import load_rules, make_scheduler, project_graph
from chipgraph.app.context import AppContext
from chipgraph.checks.plan_check import load_env
from chipgraph.core.config.models import Profile
from chipgraph.packs.digital_rtl.layout import (
    LayoutError,
    dir_or_default,
    layout_dir,
    layout_vars,
    template_dir,
    write_globs,
)
from chipgraph.packs.digital_rtl.plan.context import planner_context

QSOC = Path(__file__).resolve().parents[3] / "docs" / "examples" / "qsoc.chipgraph.yml"


def _profile(layout: dict[str, Any], **extra: Any) -> Profile:
    return Profile.model_validate({"project": "demo", "layout": layout, **extra})


# --- the helper --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "directory"),
    [
        ("design/{block}/rtl/*.sv", "design/pwm/rtl"),
        ("rtl/*.sv", "rtl"),
        ("dv/{block}/**", "dv/pwm"),
        ("dv/**", "dv"),
        ("rtl/tiny_{block}*.sv", "rtl"),
        ("src/{BLOCK}/hdl/*.sv", "src/PWM/hdl"),
        ("a/b/c.sv", "a/b"),
        ("rtl/{module}/*.sv", "rtl"),  # another placeholder ends the directory
        ("*.sv", ""),
        ("**/*.sv", ""),
    ],
)
def test_the_directory_is_the_part_before_the_first_glob_character(
    template: str, directory: str
) -> None:
    filled = template.replace("{block}", "pwm").replace("{BLOCK}", "PWM")
    assert template_dir(filled) == directory


def test_layout_dir_for_every_kind_and_the_first_of_a_list() -> None:
    profile = _profile(
        {"rtl": "design/{block}/rtl/*.sv", "tb": ["dv/{block}/**", "design/{block}/dv/**"]}
    )
    assert layout_dir(profile, "pwm", "rtl") == "design/pwm/rtl"
    assert layout_dir(profile, "pwm", "tb") == "dv/pwm"
    assert layout_vars(profile, "pwm") == {"layout.rtl": "design/pwm/rtl", "layout.tb": "dv/pwm"}
    assert write_globs(profile, "pwm") == (
        "design/pwm/rtl/*.sv",
        "dv/pwm/**",
        "design/pwm/dv/**",
    )


def test_a_block_override_wins() -> None:
    profile = _profile(
        {"rtl": "rtl/*.sv", "tb": "dv/**"},
        blocks={"pwm": {"layout": {"rtl": "ip/pwm/hdl/*.sv"}}, "gpio": {}},
    )
    assert layout_dir(profile, "pwm", "rtl") == "ip/pwm/hdl"
    assert layout_dir(profile, "gpio", "rtl") == "rtl"


def test_a_missing_key_is_a_clear_error_and_the_default_is_a_fallback() -> None:
    profile = _profile({"filelist": "filelists/{block}.f"})
    with pytest.raises(LayoutError, match=r"block 'pwm' has no layout\.rtl: say where its rtl"):
        layout_dir(profile, "pwm", "rtl")
    with pytest.raises(LayoutError, match=r"layout\.tb"):
        layout_dir(profile, "pwm", "tb")
    # The built-in rules keep working: today's directories.
    assert dir_or_default(profile, "pwm", "rtl") == "rtl"
    assert dir_or_default(profile, "pwm", "tb") == "dv/pwm"
    assert layout_vars(profile, "pwm") == {"layout.rtl": "rtl", "layout.tb": "dv/pwm"}
    assert write_globs(profile, "pwm") == ("rtl/**", "dv/pwm/**")


def test_a_template_with_no_directory_part_is_refused() -> None:
    profile = _profile({"rtl": "*.sv"})
    with pytest.raises(LayoutError, match="no directory part"):
        layout_dir(profile, "pwm", "rtl")
    with pytest.raises(LayoutError, match="no directory part"):
        dir_or_default(profile, "pwm", "rtl")
    with pytest.raises(LayoutError, match="no directory part"):
        layout_dir(_profile({"rtl": "../up/*.sv"}), "pwm", "rtl")


# --- a project with no layout key: today's directories ---------------------------------------


def test_a_project_with_no_rtl_or_tb_layout_keeps_the_default_directories(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["layout"]["rtl"], data["layout"]["tb"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    graph = project_graph(AppContext.load(root))
    [tb] = graph.instances[TB].outputs
    assert tb.path == "dv/timer/test_timer.py"
    # The plan's paths: the default directories.
    env = load_env(root, "timer", rules=load_rules(AppContext.load(root)))
    assert env.layout == ("rtl/**", "dv/timer/**")
    assert ("digital-rtl/rtl_module", "rtl/{module}.sv") in env.module_outputs


# --- a QSoC-style layout ---------------------------------------------------------------------


def _qsoc_layout(root: Path) -> None:
    """The layout section of the QSoC example profile, in the project at `root`."""
    qsoc = yaml.safe_load(QSOC.read_text(encoding="utf-8"))["layout"]
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["layout"] = {**data["layout"], "rtl": qsoc["rtl"], "tb": qsoc["tb"]}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _qsoc_plan(prefix: str = "design/timer/rtl") -> str:
    text = planned_timer(Path())
    text = text.replace("path: rtl/", f"path: {prefix}/")
    return text.replace(", replaces: true", "")


def _plan_with(root: Path, plan_text: str) -> tuple[ChainRuntime, Any]:
    runtime = ChainRuntime(root, [plan_text])
    ctx = register(AppContext.load(root), runtime)
    summary = asyncio.run(make_scheduler(ctx, EXPAND).run(EXPAND))
    return runtime, summary


def test_a_qsoc_plan_passes_plan_check_with_writes_in_the_blocks_rtl_directory(
    tmp_path: Path,
) -> None:
    assert yaml.safe_load(QSOC.read_text(encoding="utf-8"))["layout"]["rtl"] == (
        "design/{block}/rtl/*.sv"
    )
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    _qsoc_layout(root)

    runtime, summary = _plan_with(root, _qsoc_plan())
    assert summary.waiting_gate == (EXPAND,), summary  # passed plan_check at the first try
    assert len(runtime.tasks) == 1 and "feedback" not in runtime.tasks[0].context

    # The planner is told where it may write.
    context = planner_context(
        root,
        AppContext.load(root).require_profile(),
        load_rules(AppContext.load(root)),
        "timer",
        plan_path="plan/timer.plan.yml",
    )
    assert "design/timer/rtl/*.sv" in context["layout"]
    assert {
        "rule": "digital-rtl/rtl_module",
        "template": "design/timer/rtl/{module}.sv",
    } in context["module_outputs"]

    # Approved, the nodes get the layout's directory as their outputs and write sets.
    approve_plan(root, "timer")
    graph = project_graph(AppContext.load(root))
    for name in ("tiny_timer_counter", "tiny_timer_irq", "tiny_timer"):
        [out] = graph.instances[module_id(name)].outputs
        assert out.path == f"design/timer/rtl/{name}.sv"
    [tb] = graph.instances[TB].outputs
    assert tb.path == "dv/timer/test_timer.py"  # the first `layout.tb` template


def test_a_plan_writing_the_default_directory_is_refused_on_a_qsoc_layout(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    _qsoc_layout(root)
    runtime, summary = _plan_with(root, _qsoc_plan("rtl"))
    # Every try is refused: the files are outside the block's layout, and the modules do
    # not write the files the rtl_module rule will write for them.
    assert not summary.ok and summary.waiting_gate == ()
    feedback = runtime.tasks[1].context["feedback"]
    assert "plan.write_layout" in feedback and "design/timer/rtl/*.sv" in feedback
    assert "plan.write_missing" in feedback and "design/timer/rtl/tiny_timer_counter.sv" in feedback
