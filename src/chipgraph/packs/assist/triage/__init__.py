"""`/triage` (task M1-14): classify a failing lint/sim/check log as infra, rtl, tb or spec.

- `parse` reads the log with the existing log parsers: issues (file, line, rule, message),
  `chipgraph check` results, a simulation's self-check failures, the first error, an
  excerpt.
- `rules` are the deterministic first tier of `decide()`: generic evidence only (a missing
  tool or file, a time limit, a failing spec cross check, errors that all point into
  testbenches or all into RTL). A simulation mismatch never matches a rule.
- `question` builds the `decide()` question (choices `infra|rtl|tb|spec`) with the
  excerpt, the issues and, for a simulation failure, the spec lines `/ask` retrieves.
- `report` writes the deterministic summary, the suggestion per label and the evidence.
- `run` (`run_triage`, `triage_log`) ties them together behind `chipgraph triage`, the
  `triage` MCP tool and `/chipgraph:triage`.
"""

from chipgraph.packs.assist.triage.contract import (
    LABELS,
    SpecLine,
    TriageLabel,
    TriageReport,
    TriageStatus,
)
from chipgraph.packs.assist.triage.parse import CheckRun, ParsedLog, parse_log
from chipgraph.packs.assist.triage.question import PROMPT, build_question, question_id
from chipgraph.packs.assist.triage.rules import (
    RULES,
    SPEC_CROSS_CHECKS,
    RuleHit,
    TriageFacts,
    evaluate,
    is_rtl_path,
    is_tb_path,
)
from chipgraph.packs.assist.triage.run import (
    BackendChoice,
    default_backend,
    run_triage,
    triage_log,
)

__all__ = [
    "LABELS",
    "PROMPT",
    "RULES",
    "SPEC_CROSS_CHECKS",
    "BackendChoice",
    "CheckRun",
    "ParsedLog",
    "RuleHit",
    "SpecLine",
    "TriageFacts",
    "TriageLabel",
    "TriageReport",
    "TriageStatus",
    "build_question",
    "default_backend",
    "evaluate",
    "is_rtl_path",
    "is_tb_path",
    "parse_log",
    "question_id",
    "run_triage",
    "triage_log",
]
