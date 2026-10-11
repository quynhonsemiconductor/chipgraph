"""The image workflow always runs (so its job can be a required check) and skips its steps
when nothing the image is built from changed; publishing needs a relevant change too.
"""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "image.yml"


def _workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return data


def _triggers() -> dict[str, Any]:
    wf = _workflow()
    triggers: dict[str, Any] = wf.get("on") or wf[True]  # YAML 1.1 reads `on` as True
    return triggers


def test_no_path_filter_on_any_trigger() -> None:
    for name, config in _triggers().items():
        assert not (isinstance(config, dict) and "paths" in config), name


def test_every_step_after_the_decision_is_gated() -> None:
    job = _workflow()["jobs"]["build-test"]
    steps = job["steps"]
    index = next(i for i, s in enumerate(steps) if s.get("id") == "changes")
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert index == 1, "the decision comes right after the checkout"
    for step in steps[index + 1 :]:
        assert step.get("if") == "steps.changes.outputs.relevant == 'true'", step
    assert job["outputs"]["relevant"] == "${{ steps.changes.outputs.relevant }}"


def test_relevant_paths_cover_what_the_image_is_built_and_tested_from() -> None:
    import re

    steps = _workflow()["jobs"]["build-test"]["steps"]
    pattern = re.compile(next(s for s in steps if s.get("id") == "changes")["env"]["RELEVANT"])
    for path in (
        "docker/Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "src/chipgraph/cli/__init__.py",
        "tests/e2e/test_sim_edalize.py",
        "examples/tinysoc/rtl/tiny_gpio.sv",
        "docs/tb-claude-code/fixture.py",
        ".dockerignore",
        ".github/workflows/image.yml",
    ):
        assert pattern.search(path), path
    for path in ("README.md", "docs/RELEASING.md", "tests/core/engine/test_fanout.py"):
        assert not pattern.search(path), path


def test_publish_needs_a_relevant_change() -> None:
    cond = " ".join(_workflow()["jobs"]["publish"]["if"].split())
    assert cond.startswith("needs.build-test.outputs.relevant == 'true' &&")
