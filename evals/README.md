# evals

Evaluation data and graders for chipgraph's agent features. CI never calls a real model:
the tests only check that the data is true to the example project and that the graders
grade correctly. Real-model runs are started by hand (and later by the nightly `evals`
job, task M1-17, which reuses these files).

## `ask/`: `/ask` on tinysoc (task M1-13)

- `ask/tinysoc.yml`: 20 questions about `examples/tinysoc` (registers, ports, widths,
  reset values, requirements, ownership, interrupt line, clock and reset, open items).
  15 have `expected` citations (`path:line`, `path:start-end` or `model:<key>`, each file
  citation with a `has` text that must be on those lines) and optional `facts`; 5 have
  `unknown: true`: tinysoc has no source for them, so the only correct answer is
  "I don't know". `tests/evals/test_ask_grader.py` checks every citation still exists
  and every `has` is still there.
- `ask/grade.py ANSWERS.jsonl [--json]`: the deterministic grader. One answer per line,
  `{"id", "answer", "citations", "unknown", "checked"}`. It prints a verdict per question
  and the two metrics: the correct-citation rate on answerable questions (needs at least
  90 %) and the number of invented answers to unanswerable ones (needs 0). Exit 0 when
  both hold.

A real run in Claude Code (your own plan, no API key):

```bash
MAIN_MODEL=opus docs/ask-claude-code/run.sh /tmp/cg-ask-opus
```

It asks every question with `/chipgraph:ask` (answers by the haiku `chipgraph:asker`
subagent), writes `/tmp/cg-ask-opus/answers.jsonl` and grades it.
