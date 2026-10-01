# evals

Evaluation data, graders and the evals framework for chipgraph's agent features. CI never
calls a real model: the tests check that the data is true to the example project, that the
graders grade correctly, and that the framework runs end to end with a fake model.
Real-model runs use `chipgraph eval --runtime claude-code`, by hand or in the `evals` job.

## `chipgraph eval`: the framework (task M1-17)

```bash
chipgraph eval SUITE [--runtime fake|claude-code] [--main-model haiku]
               [--small-model haiku] [--large-model opus] [--budget-usd 3]
               [--time-limit 3600] [--out DIR] [--only ID,ID...]
```

Suites: `ask` (`ask/tinysoc.yml`), `triage` (`triage/faults.yml` and `triage/logs/`),
`triage-holdout` (`triage/holdout.yml` and `triage/logs-holdout/`) and `triage-holdout2`
(`triage/holdout2.yml` and `triage/logs-holdout2/`). The holdouts have the schema of
`faults.yml`; a suite whose file does not exist reports `NO DATA` and exits 0.

It is built on [Inspect AI](https://inspect.aisi.org.uk/) (`inspect-ai`, pinned in the
`evals` extra and the dev group): `harness/` makes one Inspect `Task` per suite, its
dataset from the suite's YAML file, its solver from the runtime, and its scorer the
suite's own grader (`ask/grade.py`, `triage/grade.py`; the run's verdict is the grader's
report). `--out` (default: a new temporary directory) receives:

- `logs/*.json`: the Inspect log (`uv run inspect view --log-dir DIR/logs` to browse it);
- `answers.jsonl`: the answers, in the grader's input format (`grade.py` reads it);
- `summary.json`, `summary.md`: the verdict against the thresholds (ask: >= 90 % correct
  citations and 0 invented answers; triage: >= 80 % correct), accuracy by class and by
  `decide()` backend (triage), cost, samples not run, forbidden tool calls.

Exit code: 0 pass (or no data), 1 fail, 2 error. Runtimes:

- `fake` (default): no model, no network; the tests and every CI run use it. `ask`
  answers each question with the real `/ask` answerer (retrieval, `ask_check`, retry) on
  an ingested tinysoc copy, over a scripted fake LLM that replies with the question's
  `reference` answer; `triage` triages each log with the real engine (rules, then
  `decide()`) over a fake LLM that answers the sample's label. It checks the path
  dataset -> solver -> scorer -> report, not a model.

  ```bash
  uv run chipgraph eval ask
  uv run chipgraph eval triage --out /tmp/cg-eval-triage
  ```

- `claude-code`: one headless `claude -p "/chipgraph:ask <question>"` or
  `"/chipgraph:triage logs/<id>.log [check]"` per sample, in a fresh tinysoc copy (its own
  git repo, ingested, the triage logs in `logs/`) with the dev plugin (`plugin/` with a
  `.mcp.json` that runs this checkout). The main session runs `--main-model` (default
  `haiku`); the asker subagent runs haiku (its frontmatter); the decider tiers are
  `--small-model`/`--large-model`, written into the copy's profile. The tools allowed are
  those of the acceptance scripts (`docs/ask-claude-code/`, `docs/triage-claude-code/`).
  Each sample's stream is kept in `DIR/streams/`.

  ```bash
  uv run chipgraph eval triage --runtime claude-code --out /tmp/cg-eval-triage-haiku
  uv run chipgraph eval ask --runtime claude-code --only q01,q16
  ```

  Auth: `CLAUDE_CODE_OAUTH_TOKEN` when set (CI); otherwise the logged-in Claude Code
  account (your plan). `ANTHROPIC_API_KEY` is removed from `claude`'s environment unless
  `CHIPGRAPH_EVAL_USE_API_KEY=1`, so a stray key never pays for a run.

  Cost cap: each `claude -p` gets `--max-budget-usd` (the suite budget left); once the
  summed `total_cost_usd` of the runs reaches `--budget-usd` (default $3) the remaining
  samples are not run (`not run (budget)`, graded as failed, listed in the summary).
  `--time-limit` (default 3600 s) does the same for time; one sample may take 900 s at
  most. With a plan token, `total_cost_usd` is the list-price equivalent, not a bill.

  Expected cost with haiku: triage about $1.1 for the 22 samples (the M1-14 acceptance run:
  haiku main session, haiku/opus deciders; most samples are decided by rules). The two
  holdouts (12 samples each) have not been measured; holdout2 has more simulation failures,
  which go to the deciders, so expect more per sample than the 22. Ask with a haiku main
  session has not been measured yet. The $3 default caps each suite.

### The `evals` CI job

`.github/workflows/evals.yml` runs `chipgraph eval --runtime claude-code` on `ask`,
`triage`, `triage-holdout` and `triage-holdout2` with haiku and the budget: by hand (`workflow_dispatch`,
inputs `suite`, `main_model`, `budget_usd`, the cap per suite) and weekly; never on a push
or a PR. It installs Claude Code (pinned) and uv, writes each `summary.md` to the job
summary, uploads the whole report directory as the artifact `eval-report-<run id>`, and
fails when a suite fails. It needs the repository secret `CHIPGRAPH_EVALS_CLAUDE_TOKEN`:
a Claude Code OAuth token of a separate account (`claude setup-token` on that account),
passed to `claude` as `CLAUDE_CODE_OAUTH_TOKEN` in the eval step only. Without the secret
the job skips with a notice and stays green. At most `budget_usd` x 4 per run (four
suites).

The same run on your machine: `evals/run-claude-code.sh /tmp/cg-eval-<name>` (env
`SUITES`, `MAIN_MODEL`, `BUDGET_USD`).

## `ask/`: `/ask` on tinysoc (task M1-13)

- `ask/tinysoc.yml`: 20 questions about `examples/tinysoc` (registers, ports, widths,
  reset values, requirements, ownership, interrupt line, clock and reset, open items).
  15 have `expected` citations (`path:line`, `path:start-end` or `model:<key>`, each file
  citation with a `has` text that must be on those lines), `facts` and a `reference`
  answer; 5 have `unknown: true`: tinysoc has no source for them, so the only correct
  answer is "I don't know". `tests/evals/test_ask_grader.py` checks every citation still
  exists, every `has` is still there, and every reference states its facts. A fact is
  text matched on word boundaries (`0x2` is not found in `0x20`) or a `re:` regex, never
  a bare digit: `8 bits` or `[7:0]`, `reset ... 0`, `interrupt line 0`.
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

### The second holdout set (holdout2)

Holdout 1 has been used: its score exposed a flaw in `decide()` and in triage's spec
retrieval, and both were changed because of it. It is now spent as a clean measure (a
score on it is still worth reporting, but no longer independent). `triage/holdout2.yml`
(`h2-01` ... `h2-12`, three per class) is the fresh one, for measuring that change
honestly, made by an author who read neither the triage rules, `decide()`, their tests nor
any committed log. Every fault is new against the 34 of `faults.yml` and `holdout.yml`.

It is aimed at the hard case, RTL against testbench in a simulation failure: each sample
says where it fails (`stage`: lint, build, sim, check) and how wide (`scope`: block, top,
chip), and six are `stage: sim` (three `rtl`, three `tb`; the RTL lints clean), three of
them through `tiny_top`. Its four testbenches (`triage/tb/holdout2/`) report in different
styles: a watchdog timeout with `$fatal`, scoreboard `MISMATCH` lines ending in `$stop`,
concurrent assertions with `$error`, and `$fatal` at the first failed check. One fault
kind is added, `env` (extra environment variables for the command).

The holdout rule above applies unchanged: grading only, no holdout2 id or file under
`src/` (`tests/evals/test_triage_holdout2.py`), and once anything is changed because of
it, it is spent too.

- `triage/gen_logs.py --set holdout2 [--check]` regenerates (or compares) its logs.
- `triage/grade.py ANSWERS.jsonl --set holdout2` grades answers against it.
- `docs/triage-claude-code/rules_only_holdout2.py` runs `triage/rules_only.py`, unchanged
  and as a black box, on it (as `rules_only_holdout.py` does for holdout 1; `--set
  holdout` also works) and grades it. Report its result with the run; never commit it.
- `SET=holdout2 docs/triage-claude-code/run.sh /tmp/cg-triage-holdout2` and
  `chipgraph eval triage-holdout2 --runtime claude-code` run it with a real model.
