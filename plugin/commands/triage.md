---
description: Classify a failing lint/sim/check log as infra, rtl, tb or spec, with a summary, the evidence and what to do next. Deterministic rules first; otherwise a chipgraph:decider subagent answers (small model, then large when unsure).
argument-hint: "<log path in the project> [check id]"
allowed-tools: Agent, mcp__plugin_chipgraph_chipgraph__triage, mcp__plugin_chipgraph_chipgraph__pending_decisions, mcp__plugin_chipgraph_chipgraph__answer_decision
---

Triage this failing log: `$ARGUMENTS`. The first word is the log's path inside the
project; a second word, if any, is the id of the check that produced it.

You do not classify the log yourself: the chipgraph engine does, with its rules, and asks
a model through the decider subagent only when no rule decides.

1. Call `mcp__plugin_chipgraph_chipgraph__triage` with `path` = the log path and, if a
   check id was given, `check_id` = that id.
2. If the result's `status` is `deferred`, run the **decider loop** (the same loop as
   `/chipgraph:decide`), then call `triage` again with exactly the same arguments:
   1. Call `mcp__plugin_chipgraph_chipgraph__pending_decisions`.
   2. For **every** entry in `decisions`, start one subagent with the Agent tool:
      `subagent_type` = the entry's `agent` (`chipgraph:decider`), `model` = the entry's
      `model`, `description` = the entry's `description`, and `prompt` = the entry's
      `prompt`, word for word. Start all of them in one message, so they run in
      parallel, and wait for every one.
   3. For each, call `mcp__plugin_chipgraph_chipgraph__answer_decision` with the entry's
      `question_id` and the subagent's `value`, `confidence` and `reason`, unchanged.
      If a subagent returned no such JSON object, start it once more.
   4. Call `pending_decisions` again; repeat 2-3 until it lists nothing.

   A `deferred` result can come back once more (the small model was unsure, so the
   question now waits for the large model): run the loop again. Stop after three rounds.
3. Print the final result:
   - `decided`: `label` (and who decided: `backend`, `rule`, `confidence`; say "low
     confidence: advice only" when `low_confidence` is true), then `summary`,
     `suggestion` and each `evidence` line.
   - `undecided` or still `deferred`: the `summary` and the `message`.

Rules:

- Never pick or change the label yourself; print what `triage` returned.
- Do not read or write files, run commands, or call any other tool.
- A label is advice: it never blocks a build. `infra` means fix the environment and retry
  without counting a try; `spec` means ask the spec owner before changing anything.
