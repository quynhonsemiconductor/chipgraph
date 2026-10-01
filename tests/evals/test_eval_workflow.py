"""M1-17 accept: the real-model evals job stores the eval report as a CI artifact.

The job itself needs the evals secret and a model, so it is not run here; this checks
what it is wired to do: when it runs, how it skips, what it runs and what it uploads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "evals.yml"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict[str, Any]]:
    [job] = _workflow()["jobs"].values()
    return job["steps"]


def test_runs_by_hand_or_weekly_never_on_push_or_pr() -> None:
    data = _workflow()
    triggers = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` as true
    assert set(triggers) == {"workflow_dispatch", "schedule"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"suite", "main_model", "budget_usd"}
    assert inputs["main_model"]["default"] == "haiku"
    assert inputs["suite"]["options"] == ["all", "ask", "triage", "triage-holdout"]
    assert data["permissions"] == {"contents": "read"}


def test_every_action_is_pinned_by_sha() -> None:
    uses = [s["uses"] for s in _steps() if "uses" in s]
    assert uses and all(PINNED.match(u) for u in uses), uses
    assert any(u.startswith("actions/upload-artifact@") for u in uses)


def test_skips_cleanly_without_the_secret() -> None:
    steps = _steps()
    check = steps[0]
    assert check["id"] == "auth"
    assert "secrets.CHIPGRAPH_EVALS_CLAUDE_TOKEN" in check["env"]["TOKEN"]
    assert "::notice::" in check["run"] and "GITHUB_STEP_SUMMARY" in check["run"]
    # Every later step is gated on the check, so a missing secret leaves a green job.
    assert all("steps.auth.outputs.ok == 'true'" in s.get("if", "") for s in steps[1:])


def test_the_token_reaches_only_the_eval_step() -> None:
    with_token = [s["name"] for s in _steps() if "CLAUDE_CODE_OAUTH_TOKEN" in (s.get("env") or {})]
    assert with_token == ["Run the evals"]


def test_runs_chipgraph_eval_and_uploads_its_report_as_an_artifact() -> None:
    steps = {s.get("name") or s.get("uses", "").split("@")[0]: s for s in _steps()}
    run = steps["Run the evals"]
    script = run["run"]
    assert "uv run chipgraph eval" in script and "--runtime claude-code" in script
    assert '--main-model "$MAIN_MODEL"' in script and '--budget-usd "$BUDGET_USD"' in script
    assert '--out "$EVAL_OUT/$suite"' in script
    assert "ask triage triage-holdout" in script
    assert "GITHUB_STEP_SUMMARY" in script and "summary.md" in script
    out_dir = run["env"]["EVAL_OUT"]

    upload = steps["Upload the eval report"]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["path"] == out_dir  # the directory every --out is under
    assert upload["if"].startswith("always()")  # failed suites keep their report too
    assert "Claude Code" in str(steps) and "@anthropic-ai/claude-code@" in str(steps)
