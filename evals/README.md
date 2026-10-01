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

## `triage/`: `/triage` on labelled failing logs (task M1-14)

The labels are never a model's: every sample log was made by injecting a known fault into
a fresh copy of `examples/tinysoc` and running the real tool, so the fault fixes the label.

- `triage/faults.yml`: one entry per sample (`log-01` ... `log-22`; the ids say nothing
  about the label): the label (`infra`, `rtl`, `tb`, `spec`), a one-line description, the
  fault as re-appliable edits (`find`/`replace` in one file, a removal, a `chmod`, a
  reduced `PATH`), and the command that was run. `rtl` faults break RTL that the spec and
  the testbench agree on (lint, and two simulation-only bugs); `tb` faults break only a
  testbench; `spec` faults edit only a MAS or `chip.yml` and are caught by chipgraph's own
  cross checks; `infra` faults break the environment (a missing tool, a filelist path, a
  make target, a time limit, an unreadable file, a missing Design Model).
- `triage/tb/`: two small self-checking SystemVerilog testbenches for tinysoc (`verilator
  --binary --timing`), printing `PASS` or `FAIL <what>: expected X got Y`. They are not
  part of tinysoc.
- `triage/gen_logs.py`: applies each fault to a tmp copy, runs the command, and writes
  `triage/logs/<id>.log` (stdout and stderr; tmp paths made repo-relative, Verilator's
  install path `$VERILATOR_ROOT`, times `N`) and `<id>.json` (command, exit code, check
  id, Verilator version). Running it twice gives identical files (`--check` compares).
  It needs Verilator, so CI does not run it: `tests/evals/test_triage_samples.py` checks
  the committed logs against `faults.yml` (ids, labels, at least three per class, no
  machine path, every fault still applies) and regenerates three of them when the same
  Verilator is installed.
- `triage/grade.py ANSWERS.jsonl [--json]`: the deterministic grader. One answer per line,
  `{"id", "label", "backend", ...}`. It prints a verdict per sample, the confusion matrix
  (true label x answered label) and the accuracy by `decide()` backend (rule, small,
  large); it passes at 80 % or more.
- `triage/rules_only.py`: triages every sample with the rules only (no model) and grades
  it. Today the rules decide 17 of 22, all correctly (every `infra` and `spec` sample);
  the five simulation mismatches (`log-05`, `log-07`, `log-12`, `log-16`, `log-18`: RTL or
  testbench, only decidable against the spec) go to the model.

A real run in Claude Code (your own plan, no API key):

```bash
docs/triage-claude-code/run.sh /tmp/cg-triage-haiku
```

It triages every sample with `/chipgraph:triage` (main session `MAIN_MODEL=haiku`; the
decider subagents on the profile's tiers, small `haiku`, large `LARGE_MODEL=opus`, or
`sonnet`), writes `/tmp/cg-triage-haiku/answers.jsonl` and grades it.

Real QSoC CI logs may be added later; their labels are then confirmed by a person.

### The holdout set (task M1-17)

The 22 samples were seen by whoever wrote the triage rules, so a score on them may reflect
rules fitted to their exact error strings. `triage/holdout.yml` is a second, smaller set
(`hold-01` ... `hold-12`, at least two per class) made the same way, by an author who did
not read the rules or the 22 logs, from other files, tool messages and constructs (a
signal-killed job, a file-size limit, a removed Verilator option, a combinational loop, a
bad top-level decode, a wrong reset value, a testbench that samples too early, an
address overlap in `chip.yml`, overlapping MAS fields, ...). Its sample logs are in
`triage/logs-holdout/`, and its one extra testbench, a check of `tiny_top` through the
register bus, is `triage/tb/holdout/tb_soc_regs.sv`.

**The rule: never tune triage rules on the holdout.** Do not read its logs to write or
change a rule, and never name a holdout id or file under `src/`
(`tests/evals/test_triage_holdout.py` checks that). It is only for grading: the score on
the 22 is trusted only when the holdout also scores at least 80 %. If the holdout is ever
used to fix a rule, it is spent: make a new one.

- `triage/gen_logs.py --set holdout [--check]` regenerates (or compares) its logs; it adds
  one fault kind, `ulimit` (the command runs under that shell limit).
- `triage/grade.py ANSWERS.jsonl --set holdout` grades answers against it.
- `docs/triage-claude-code/rules_only_holdout.py` runs `triage/rules_only.py`, unchanged
  and as a black box, on the holdout (in a temporary copy of the repo where the holdout
  stands in for the 22) and grades it. Its result is reported with a run, never written
  into the repository.
- `SET=holdout docs/triage-claude-code/run.sh /tmp/cg-triage-holdout` runs it in real
  Claude Code; the default is still the 22.
