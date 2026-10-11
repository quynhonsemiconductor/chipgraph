"""The body of a block's pull request: what a reviewer needs, factual and short.

Four parts, each from data the build already has:

- **Plan**: the approved plan's path and hash, its top and modules in build order (what
  each does, its requirements, its files);
- **Requirement coverage**: each requirement of the block and the modules covering it,
  or why it is unassigned (`chipgraph plan show`'s table);
- **Checks**: the last result of each check of the block's `rtl_module` and `tb_module`
  instances, from the run journals;
- **Critic review**: the verdict and summary of `reports/review/<block>.json`, with its
  blocker and major comments (`file:line`, the claim); minor ones are counted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from chipgraph.app.context import AppContext
from chipgraph.core.runtime.roles import ReviewReport
from chipgraph.core.state import journal as journal_mod

MAX_COMMENTS = 20
"""Most blocker/major comments listed; the rest are counted."""
MAX_CHARS = 60_000
"""Longest body (GitHub's limit is 65 536 characters)."""

CHECKED_RULES = ("digital-rtl/rtl_module", "dv/tb_module")
"""The rules whose instances' check results the body lists."""


def last_checks(ctx: AppContext, block: str) -> dict[str, dict[str, str]]:
    """Instance id -> check id -> status: the last result of each, over every run."""
    runs = ctx.layout.runs_dir
    found: dict[str, dict[str, str]] = {}
    if not runs.is_dir():
        return found
    for run in sorted(p.name for p in runs.iterdir() if p.is_dir()):
        try:
            events = journal_mod.read(ctx.layout.journal(run)).events
        except (OSError, ValueError):
            continue
        for event in events:
            iid = event.rule_instance
            if event.type != "check_result" or iid is None or not _of_block(iid, block):
                continue
            check = event.payload.get("check_id")
            status = event.payload.get("status")
            if isinstance(check, str) and isinstance(status, str):
                found.setdefault(iid, {})[check] = status
    return found


def _of_block(instance_id: str, block: str) -> bool:
    rule, _, rest = instance_id.partition("[")
    params = dict(p.partition("=")[::2] for p in rest.removesuffix("]").split(",") if "=" in p)
    return rule in CHECKED_RULES and params.get("block") == block


def read_review(path: Path) -> ReviewReport | None:
    """The Critic's review report at `path`, or None if it is missing or not one."""
    try:
        return ReviewReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _plan_lines(summary: Mapping[str, Any]) -> list[str]:
    gate = summary.get("gate", {})
    sha = str(summary.get("sha256") or "")[:12]
    lines = [
        "### Plan",
        f"`{summary.get('path')}` (sha256 `{sha}`), gate `{gate.get('id')}`: "
        f"{gate.get('status')}. Top: `{summary.get('top')}`.",
        "",
    ]
    for module in summary.get("modules", []):
        reqs = ", ".join(module.get("reqs", [])) or "-"
        files = ", ".join(f"`{w['path']}`" for w in module.get("writes", []))
        lines.append(f"- `{module['name']}`: {module['summary']} Requirements: {reqs}. {files}")
    questions = summary.get("open_questions") or []
    if questions:
        lines.append("")
        lines.append("Open questions in the plan: " + "; ".join(questions))
    return lines


def _coverage_lines(summary: Mapping[str, Any]) -> list[str]:
    rows = summary.get("coverage") or []
    lines = ["### Requirement coverage"]
    if not rows:
        return [*lines, "The block has no requirements in the Design Model."]
    covered = sum(1 for r in rows if r.get("modules"))
    lines.append(f"{covered} of {len(rows)} requirements assigned to a module.")
    lines.append("")
    for row in rows:
        if row.get("modules"):
            lines.append(f"- {row['req']}: {', '.join(row['modules'])}")
        elif row.get("unassigned"):
            lines.append(f"- {row['req']}: unassigned ({row['unassigned']})")
        else:
            lines.append(f"- {row['req']}: not covered")
    return lines


def _check_lines(checks: Mapping[str, Mapping[str, str]]) -> list[str]:
    lines = ["### Checks"]
    if not checks:
        return [*lines, "No check results recorded for this block."]
    for iid in sorted(checks):
        results = ", ".join(f"{cid} {status}" for cid, status in checks[iid].items())
        lines.append(f"- `{iid}`: {results}")
    return lines


def _review_lines(review: ReviewReport | None, path: str) -> list[str]:
    lines = ["### Critic review"]
    if review is None:
        return [*lines, f"No valid review report at `{path}`."]
    lines.append(f"Verdict: **{review.verdict}**. {review.summary}")
    serious = [c for c in review.comments if c.severity in ("blocker", "major")]
    others = len(review.comments) - len(serious)
    for comment in serious[:MAX_COMMENTS]:
        req = f" ({comment.req_id})" if comment.req_id else ""
        lines.append(f"- {comment.severity} `{comment.file}:{comment.line}`{req}: {comment.claim}")
    if len(serious) > MAX_COMMENTS:
        lines.append(f"- and {len(serious) - MAX_COMMENTS} more blocker/major comments")
    if others:
        lines.append(f"{others} minor or nit comment(s) are in `{path}`.")
    if not review.comments:
        lines.append("No findings.")
    return lines


def render_body(
    *,
    block: str,
    summary: Mapping[str, Any],
    checks: Mapping[str, Mapping[str, str]],
    review: ReviewReport | None,
    review_path: str,
    files: Sequence[str],
) -> str:
    """The PR body (Markdown), at most `MAX_CHARS` characters."""
    parts: Iterable[list[str]] = (
        [
            f"Block `{block}`: RTL and testbench built by chipgraph from the approved plan.",
            "Merging is a person's decision: chipgraph never merges.",
        ],
        _plan_lines(summary),
        _coverage_lines(summary),
        _check_lines(checks),
        _review_lines(review, review_path),
        ["### Files", *(f"- `{f}`" for f in files)],
    )
    text = "\n\n".join("\n".join(part) for part in parts) + "\n"
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 40] + "\n\n(cut: the body is too long)\n"
    return text


__all__ = ["CHECKED_RULES", "last_checks", "read_review", "render_body"]
