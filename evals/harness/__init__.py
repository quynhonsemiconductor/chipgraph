"""The chipgraph evals framework (task M1-17), on Inspect AI, behind `chipgraph eval`.

One Inspect `Task` per suite (`suites`): the dataset comes from the suite's YAML file
(`ask/tinysoc.yml`, `triage/faults.yml`, `triage/holdout.yml`, `review/defects.yml`), the
solver from the runtime, and the scorer is the suite's deterministic grader
(`ask/grade.py`, `triage/grade.py`, `review/grade.py`), unchanged: a sample's score is
the grader's verdict on its answer, and the run's verdict is the grader's report on all
of them.

Runtimes (`runtimes`):

- `fake`: deterministic, no network, used by the tests and every CI run. The ask suite
  answers each question through the real API-runtime answerer (retrieval, `ask_check`,
  retry) with a scripted fake LLM that replies with the question's reference answer; the
  triage suites triage each committed log with the real engine (rules, then `decide()`)
  over a fake LLM that answers the sample's known label.
- `claude-code`: one headless `claude -p "/chipgraph:<cmd> ..."` per sample, on a fresh
  tinysoc copy with the dev plugin (`claude_code`); a per-sample budget flag, a suite
  budget on the summed `total_cost_usd` and a suite time limit.

The `review` suite (task M2-09) has its own runtimes (`review`): each sample is its own
tinysoc copy with a planted change, reviewed by the rule `digital-rtl/review`.

`run_suite` runs one suite and writes, in its output directory, the Inspect log
(`logs/`), the answers in the graders' JSONL format (`answers.jsonl`) and a short summary
(`summary.json`, `summary.md`; `report`).

`evals/` is not part of the installed package: `chipgraph eval` loads this directory, in
a chipgraph checkout, as the package `chipgraph_evals`.
"""

from __future__ import annotations

from .run import EvalError, EvalOptions, SuiteResult, run_suite
from .suites import SUITES, Suite

__all__ = ["SUITES", "EvalError", "EvalOptions", "Suite", "SuiteResult", "run_suite"]
