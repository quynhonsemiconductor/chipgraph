"""The deterministic parts of a triage report: the summary, the suggestion, the evidence.

The summary is a template over the parsed log (what failed, where), never model text. The
suggestion follows DESIGN 5.2 per label: `infra` -> fix the environment, then retry
without counting a try; `rtl`/`tb` -> the file:line to fix; `spec` -> the spec line, and
ask the spec owner (a person decides a spec change).
"""

from __future__ import annotations

from chipgraph.core.contracts import Issue
from chipgraph.packs.assist.triage.contract import SpecLine, TriageLabel
from chipgraph.packs.assist.triage.parse import ParsedLog
from chipgraph.packs.assist.triage.rules import (
    RuleHit,
    TriageFacts,
    is_rtl_path,
    is_tb_path,
    where,
)

MAX_EVIDENCE = 10


def _issue_text(issue: Issue) -> str:
    loc = where(issue)
    rule = f" [{issue.rule}]" if issue.rule else ""
    return f"{loc or '(no file)'}{rule} {issue.msg}".strip()


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def summary(parsed: ParsedLog) -> str:
    """What failed and where, in one or two sentences."""
    checks = parsed.failing_checks
    if checks:
        first = checks[0]
        scope = f" for block {first.block}" if first.block else ""
        verb = "could not run" if first.status == "error" else "failed"
        text = f"check {first.check_id}{scope} {verb} with {_count(len(first.issues), 'issue')}"
        if first.issues:
            text += f"; first: {_issue_text(first.issues[0])}"
        if len(checks) > 1:
            text += f" (and {_count(len(checks) - 1, 'other failing check')})"
        return text
    if parsed.sim_failures:
        text = (
            f"simulation failed {_count(len(parsed.sim_failures), 'self-check')}; first: "
            f"{parsed.sim_failures[0]}"
        )
        stop = next((i for i in parsed.runtime_issues if i.file), None)
        if stop is not None:
            text += f" (stopped at {where(stop)})"
        return text
    errors = [i for i in parsed.tool_issues if i.severity == "error"]
    warnings = [i for i in parsed.tool_issues if i.severity == "warning"]
    if parsed.tool_issues:
        top = (errors or warnings or list(parsed.tool_issues))[0]
        return (
            f"{_count(len(errors), 'error')} and {_count(len(warnings), 'warning')} from the "
            f"tool; first: {_issue_text(top)}"
        )
    if parsed.unlocated_errors:
        return f"the tool stopped: {parsed.unlocated_errors[0]}"
    if parsed.first_error:
        return f"the run failed: {parsed.first_error.splitlines()[0].strip()}"
    return "no error message found in the log"


def evidence(
    label: TriageLabel | None,
    parsed: ParsedLog,
    facts: TriageFacts,
    hit: RuleHit | None,
    specs: tuple[SpecLine, ...] = (),
) -> tuple[str, ...]:
    """The `file:line` locations behind the label: the rule's, else the parsed ones."""
    out: list[str] = list(hit.evidence) if hit is not None else []
    if not out and label != "infra":
        issues = list(parsed.issues)
        if label == "tb":
            issues = [
                i for i in issues if i.file and is_tb_path(i.file, facts.tb_patterns)
            ] or issues
        elif label == "rtl":
            issues = [
                i for i in issues if i.file and is_rtl_path(i.file, facts.tb_patterns)
            ] or issues
        for issue in issues:
            loc = where(issue)
            if loc and loc not in out:
                out.append(loc)
    if label == "spec" or (label in ("rtl", "tb") and parsed.sim_failures):
        out.extend(s.location for s in specs[:3] if s.location not in out)
    return tuple(out[:MAX_EVIDENCE])


def _first_spec_location(parsed: ParsedLog, facts: TriageFacts, specs: tuple[SpecLine, ...]) -> str:
    for issue in parsed.issues:
        if (
            issue.file
            and not is_rtl_path(issue.file, facts.tb_patterns)
            and not is_tb_path(issue.file, facts.tb_patterns)
        ):
            return where(issue)
    if specs:
        return specs[0].location
    return next((where(i) for i in parsed.issues if i.file), "")


def _first_location(issues: tuple[Issue, ...], wanted: str, facts: TriageFacts) -> str:
    # Errors first: a warning next to an error is rarely what has to change.
    for issue in sorted(issues, key=lambda i: i.severity != "error"):
        if not issue.file:
            continue
        if wanted == "tb" and is_tb_path(issue.file, facts.tb_patterns):
            return where(issue)
        if wanted == "rtl" and is_rtl_path(issue.file, facts.tb_patterns):
            return where(issue)
    return ""


def suggestion(
    label: TriageLabel,
    parsed: ParsedLog,
    facts: TriageFacts,
    hit: RuleHit | None,
    specs: tuple[SpecLine, ...] = (),
) -> str:
    """What to do next for `label`."""
    rerun = f"rerun `{facts.check_id}`" if facts.check_id else "rerun it"
    if label == "infra":
        fix = hit.hint if hit is not None and hit.hint else "fix the environment the run needs"
        return (
            f"Not a design problem: {fix}. Then {rerun}; the retry does not count a try "
            "(DESIGN 5.2)."
        )
    if label == "spec":
        loc = _first_spec_location(parsed, facts, specs)
        at = f" at {loc}" if loc else ""
        return (
            f"The spec is the side to change{at}. Ask the spec owner to confirm before "
            "anything is changed: a spec change is a person's decision."
        )
    side = "testbench" if label == "tb" else "RTL"
    if parsed.sim_failures:
        against = f" against {specs[0].location}" if specs else " against the spec"
        return (
            f"Fix the {side} behind the failing self-check ({parsed.sim_failures[0]}); "
            f"check it{against}, then {rerun}."
        )
    loc = _first_location(parsed.issues, label, facts)
    if loc:
        return f"Fix the {side} at {loc}, then {rerun}."
    return f"Fix the {side} named in the log, then {rerun}."


__all__ = ["evidence", "suggestion", "summary"]
