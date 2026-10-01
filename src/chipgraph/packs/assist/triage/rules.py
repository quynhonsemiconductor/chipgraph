"""The deterministic triage rules: the first, free tier of `decide()` (DESIGN 5.4).

Each rule looks only at *generic* evidence in a parsed log: what the tools and shells
print when a tool or a file is missing, when time runs out, which kind of check failed,
and whether every error points into a testbench or into RTL sources. No rule names a
project, a file or a sample: `tests/packs/assist/test_triage_rules.py` fails if one does.

A rule answers only when its evidence is unambiguous and returns `None` otherwise; a
simulation mismatch never matches a rule (whether the RTL or the testbench is wrong can
only be told against the spec), so it goes to the model tiers.

Order matters: the environment is ruled out first (`infra`), then a failing spec cross
check (`spec`), then compile/lint errors that all point into testbenches (`tb`) or all
into RTL sources (`rtl`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch

from chipgraph.core.contracts import Decision, Issue
from chipgraph.core.engine.decide import Question, Rule, rule_decision
from chipgraph.packs.assist.triage.contract import TriageLabel
from chipgraph.packs.assist.triage.parse import CheckRun, ParsedLog

SPEC_CROSS_CHECKS = frozenset({"ports_diff", "spec_schema", "trace", "cross_chip"})
"""Checks that compare a spec with its users: when one fails, the spec side is at fault."""

HDL_SUFFIXES = (".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl")
TB_DIRS = frozenset(
    {"tb", "tbs", "dv", "verif", "verification", "testbench", "testbenches", "test", "tests"}
)
"""Directory names that hold testbenches by common convention."""

TB_LAYOUT_KEYS = ("tb", "dv", "tests")
"""Profile `layout` keys whose paths hold testbenches."""


@dataclass(frozen=True)
class TriageFacts:
    """What the rules look at: the parsed log, the failing check, the testbench layout."""

    parsed: ParsedLog
    check_id: str | None = None
    """The failing check id the caller named (`--check`), if any."""
    check_kinds: tuple[tuple[str, str], ...] = ()
    """`(check id, check kind)` pairs: the kind is the adapter the profile runs it with."""
    tb_patterns: tuple[str, ...] = ()
    """Glob patterns of testbench paths, from the profile's `layout`."""

    def kind_of(self, check_id: str) -> str:
        """The check's kind (its adapter), or its id when the profile does not say."""
        return dict(self.check_kinds).get(check_id, check_id)


@dataclass(frozen=True)
class RuleHit:
    """A rule's answer: the label, the rule's name, why, and where."""

    label: TriageLabel
    rule: str
    reason: str
    evidence: tuple[str, ...] = ()
    hint: str = ""
    """A specific next step (e.g. the missing tool), used in the suggestion."""


# --- paths ------------------------------------------------------------------------------


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_tb_path(path: str, patterns: tuple[str, ...] = ()) -> bool:
    """True for a testbench path: a profile `layout` match, a `tb`/`dv`/... directory,
    or a `tb_`/`test_` prefix or `_tb`/`_test` suffix on the file name."""
    path = _norm(path)
    if any(fnmatch(path, pattern) for pattern in patterns):
        return True
    parts = path.split("/")
    if any(part.lower() in TB_DIRS for part in parts[:-1]):
        return True
    stem = parts[-1].lower().split(".", 1)[0]
    return (
        stem == "tb"
        or stem.startswith(("tb_", "test_"))
        or stem.endswith(("_tb", "_test", "_tests", "_tb_top"))
    )


def is_rtl_path(path: str, patterns: tuple[str, ...] = ()) -> bool:
    """True for an HDL source that is not a testbench."""
    return _norm(path).lower().endswith(HDL_SUFFIXES) and not is_tb_path(path, patterns)


def tb_patterns(layout: dict[str, str | tuple[str, ...]]) -> tuple[str, ...]:
    """Glob patterns for the testbench paths a profile's `layout` names."""
    out: list[str] = []
    for key in TB_LAYOUT_KEYS:
        value = layout.get(key)
        templates = (value,) if isinstance(value, str) else (value or ())
        for template in templates:
            pattern = _norm(re.sub(r"\{[^}]*\}", "*", template)).rstrip("/")
            if pattern:
                out.extend((pattern, f"{pattern}/*"))
    return tuple(out)


def where(issue: Issue) -> str:
    """`file:line` (or just `file`) of an issue."""
    if issue.file is None:
        return ""
    return f"{issue.file}:{issue.line}" if issue.line is not None else issue.file


def _locations(issues: tuple[Issue, ...] | list[Issue], limit: int = 10) -> tuple[str, ...]:
    out: list[str] = []
    for issue in issues:
        loc = where(issue)
        if loc and loc not in out:
            out.append(loc)
        if len(out) >= limit:
            break
    return tuple(out)


# --- infra evidence -----------------------------------------------------------------------

_TOOL_MISSING = (
    re.compile(
        r"^(?:\S*/)?(?:ba|z|da|k)?sh(?:\[\d+\])?:\s*(?:line \d+:\s*)?(?:\d+:\s*)?"
        r"(?P<tool>[^\s:]+): (?:command )?not found",
        re.MULTILINE,
    ),
    re.compile(
        r"^(?:\S*/)?g?make(?:\[\d+\])?: (?P<tool>[^\s:*]+): "
        r"(?:No such file or directory|command not found)",
        re.MULTILINE,
    ),
    re.compile(r"\bcommand not found: (?P<tool>\S+)"),
    re.compile(r"(?P<tool>[^\s:]+): command not found"),
    re.compile(r"\] Error 127\b"),
)

_INPUT_MISSING = (
    re.compile(
        r"Cannot find file containing module: "
        r"'(?P<path>[^']*/[^']*|[^']*\.(?:sv|svh|v|vh|vhd|vhdl))'"
    ),
    re.compile(r"\bFile not found: (?P<path>\S+)"),
    re.compile(r"Cannot find include file: ['\"]?(?P<path>[^'\"\s]+)"),
    re.compile(
        r"\b[Cc]an(?:no|')t open (?:file |input file |include file )?['\"]?(?P<path>[^'\"\s:]+)"
    ),
    re.compile(r"(?P<path>[^\s:]+): Permission denied"),
    re.compile(r"\bPermission denied\b"),
    re.compile(r"(?P<path>[^\s:'\"]+): No such file or directory"),
    re.compile(r"\bNo such file or directory\b"),
)

_MAKE_TARGET = re.compile(r"No rule to make target [`'\"]?(?P<target>[^'`\"\s,]+)")
_NO_TARGETS = re.compile(r"make(?:\[\d+\])?: \*\*\* No targets\b")

_TIME_LIMIT = re.compile(
    r"\btimed out\b|\btime ?limit (?:exceeded|reached)\b"
    r"|\btimeout (?:after|expired|exceeded|reached)\b"
    r"|^\s*(?:Killed|Terminated)\b|\bKilled: 9\b|\bSIG(?:KILL|TERM|XCPU)\b"
    r"|\] Error 1(?:24|37)\b",
    re.IGNORECASE | re.MULTILINE,
)

_TOOL_FAILURE = re.compile(
    r"\binternal error\b|\bsegmentation fault\b|\bcore dumped\b|std::bad_alloc"
    r"|\bout of memory\b|\blicen[cs]e\b.*\b(?:fail\w*|unavailable|denied|checkout|"
    r"not available|expired)\b",
    re.IGNORECASE,
)


def _scan_text(facts: TriageFacts) -> str:
    """The log lines that may carry environment evidence: not a testbench's own failure
    lines (a testbench printing 'timeout waiting for irq' is not an infra problem), plus
    the messages of issues that name no file (a check that could not run)."""
    failures = set(facts.parsed.sim_failures)
    lines = [line for line in facts.parsed.lines if line.strip() not in failures]
    lines.extend(i.msg for i in facts.parsed.issues if i.file is None)
    for check in facts.parsed.failing_checks:
        lines.extend(i.msg for i in check.issues if i.file is None)
    return "\n".join(lines)


def _first_line(text: str, start: int) -> str:
    begin = text.rfind("\n", 0, start) + 1
    end = text.find("\n", start)
    return text[begin : end if end >= 0 else len(text)].strip()


def check_could_not_run(facts: TriageFacts) -> RuleHit | None:
    """A check reported `error` (it could not run: missing model, tool, adapter, time)."""
    failing = facts.parsed.failing_checks
    if not failing or any(c.status != "error" for c in failing):
        return None
    first = failing[0]
    msgs = [i.msg for c in failing for i in c.issues]
    reason = f"check {first.check_id!r} could not run" + (f": {msgs[0]}" if msgs else "")
    return RuleHit("infra", "check_could_not_run", reason, hint=msgs[0] if msgs else "")


def tool_missing(facts: TriageFacts) -> RuleHit | None:
    """A shell or make could not find the program to run (exit 127)."""
    text = _scan_text(facts)
    for pattern in _TOOL_MISSING:
        match = pattern.search(text)
        if match is None:
            continue
        tool = match.groupdict().get("tool")
        hint = (
            f"install `{tool}` or put it on PATH (or load its module: `env.modules`)"
            if tool
            else "install the missing program or put it on PATH"
        )
        return RuleHit(
            "infra",
            "tool_missing",
            f"a program is missing: {_first_line(text, match.start())}",
            hint=hint,
        )
    return None


def input_missing(facts: TriageFacts) -> RuleHit | None:
    """A file the command needs does not exist or cannot be read."""
    text = _scan_text(facts)
    for pattern in _INPUT_MISSING:
        match = pattern.search(text)
        if match is None:
            continue
        path = match.groupdict().get("path")
        line = _first_line(text, match.start())
        if "Permission denied" in line:
            hint = f"make `{path}` readable" if path else "fix the file permissions"
        elif path:
            hint = f"fix the path `{path}` in the filelist or the command, or restore the file"
        else:
            hint = "fix the path in the filelist or the command, or restore the file"
        return RuleHit(
            "infra",
            "input_missing",
            f"an input file is missing: {line}",
            evidence=(path,) if path else (),
            hint=hint,
        )
    return None


def make_target_missing(facts: TriageFacts) -> RuleHit | None:
    """`make` has no rule for the target the flow runs."""
    text = _scan_text(facts)
    match = _MAKE_TARGET.search(text) or _NO_TARGETS.search(text)
    if match is None:
        return None
    target = match.groupdict().get("target")
    hint = (
        f"add the make target `{target}` or fix the command the profile runs"
        if target
        else "add the make target or fix the command the profile runs"
    )
    line = _first_line(text, match.start())
    return RuleHit("infra", "make_target_missing", f"make has no such target: {line}", hint=hint)


def time_limit(facts: TriageFacts) -> RuleHit | None:
    """The command ran out of time or was killed."""
    text = _scan_text(facts)
    match = _TIME_LIMIT.search(text)
    if match is None:
        return None
    line = _first_line(text, match.start())
    return RuleHit(
        "infra",
        "time_limit",
        f"the command ran out of time or was killed: {line}",
        hint="find what it waits on (a licence, a lock, a hung tool) or raise its time limit "
        "(`adapters.<check>.timeout_s`)",
    )


def tool_failure(facts: TriageFacts) -> RuleHit | None:
    """The tool itself crashed, ran out of memory, or found no licence."""
    text = _scan_text(facts)
    match = _TOOL_FAILURE.search(text)
    if match is None:
        return None
    line = _first_line(text, match.start())
    return RuleHit(
        "infra",
        "tool_failure",
        f"the tool failed: {line}",
        hint="check the tool's installation, memory and licence, then rerun",
    )


# --- design evidence ---------------------------------------------------------------------


def _failing_spec_checks(facts: TriageFacts) -> tuple[CheckRun, ...] | None:
    """The failing checks, when every one of them is a spec cross check that ran."""
    failing = facts.parsed.failing_checks
    if failing:
        if all(
            c.status == "fail" and facts.kind_of(c.check_id) in SPEC_CROSS_CHECKS for c in failing
        ):
            return failing
        return None
    if facts.check_id is not None and facts.kind_of(facts.check_id) in SPEC_CROSS_CHECKS:
        issues = facts.parsed.tool_issues
        if issues:
            return (CheckRun(facts.check_id, None, "fail", issues),)
    return None


def spec_cross_check(facts: TriageFacts) -> RuleHit | None:
    """A spec cross check (`ports_diff`, `spec_schema`, `trace`, `cross_chip`) failed."""
    if facts.parsed.sim_ran:
        return None
    checks = _failing_spec_checks(facts)
    if checks is None:
        return None
    issues = [i for c in checks for i in c.issues]
    if not issues or any(i.file is None for i in issues):
        return None
    names = ", ".join(sorted({c.check_id for c in checks}))
    first = issues[0]
    return RuleHit(
        "spec",
        "spec_cross_check",
        f"the spec cross check {names} failed: {where(first)} [{first.rule}] {first.msg}",
        evidence=_locations(issues),
    )


def _design_issues(facts: TriageFacts) -> list[Issue] | None:
    """The compile/lint issues behind the failure, when they say where (else None)."""
    parsed = facts.parsed
    if parsed.sim_ran or parsed.unlocated_errors:
        return None
    if any(c.status == "error" for c in parsed.failing_checks):
        return None
    if any(facts.kind_of(c.check_id) in SPEC_CROSS_CHECKS for c in parsed.failing_checks):
        return None
    issues = [i for i in parsed.tool_issues if i.severity in ("error", "warning")]
    for check in parsed.failing_checks:
        issues.extend(i for i in check.issues if i.severity in ("error", "warning"))
    if not issues or any(i.file is None for i in issues):
        return None
    return issues


def testbench_compile(facts: TriageFacts) -> RuleHit | None:
    """Every compile/lint error points into a testbench, and no simulation ran."""
    issues = _design_issues(facts)
    if issues is None or not all(is_tb_path(i.file or "", facts.tb_patterns) for i in issues):
        return None
    first = issues[0]
    return RuleHit(
        "tb",
        "testbench_compile",
        f"every error is in a testbench: {where(first)} [{first.rule}] {first.msg}",
        evidence=_locations(issues),
    )


def rtl_lint(facts: TriageFacts) -> RuleHit | None:
    """Every compile/lint error points into an RTL source, and no simulation ran."""
    issues = _design_issues(facts)
    if issues is None or not all(is_rtl_path(i.file or "", facts.tb_patterns) for i in issues):
        return None
    first = issues[0]
    return RuleHit(
        "rtl",
        "rtl_lint",
        f"every error is in an RTL source: {where(first)} [{first.rule}] {first.msg}",
        evidence=_locations(issues),
    )


RULES: tuple[Callable[[TriageFacts], RuleHit | None], ...] = (
    check_could_not_run,
    tool_missing,
    input_missing,
    make_target_missing,
    time_limit,
    tool_failure,
    spec_cross_check,
    testbench_compile,
    rtl_lint,
)
"""Every triage rule, in the order `decide()` tries them."""


def evaluate(facts: TriageFacts) -> RuleHit | None:
    """The first rule that answers, or None: what `decide()`'s rule tier will say."""
    for rule in RULES:
        hit = rule(facts)
        if hit is not None:
            return hit
    return None


def decide_rules(facts: TriageFacts, fired: list[RuleHit]) -> list[Rule]:
    """The rules as `decide()` rules over `facts`; the hit that answered goes to `fired`."""

    def make(rule: Callable[[TriageFacts], RuleHit | None]) -> Rule:
        def decide_rule(question: Question) -> Decision | None:
            hit = rule(facts)
            if hit is None:
                return None
            fired.append(hit)
            return rule_decision(question, hit.label)

        decide_rule.__name__ = rule.__name__
        return decide_rule

    return [make(rule) for rule in RULES]


__all__ = [
    "HDL_SUFFIXES",
    "RULES",
    "SPEC_CROSS_CHECKS",
    "TB_DIRS",
    "RuleHit",
    "TriageFacts",
    "decide_rules",
    "evaluate",
    "is_rtl_path",
    "is_tb_path",
    "tb_patterns",
    "where",
]
