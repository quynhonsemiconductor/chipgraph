---
description: Answer every question chipgraph's decide() queued for a model - one chipgraph:decider subagent per question, on its tier's model, in parallel - until none is pending.
allowed-tools: Agent, mcp__plugin_chipgraph_chipgraph__pending_decisions, mcp__plugin_chipgraph_chipgraph__answer_decision
---

You run chipgraph's **decider loop**. The engine's `decide()` asks multiple-choice
questions (classify a failing log, ...); in Claude Code it queues them for a model in this
session instead of calling one itself. You only dispatch them and hand the answers back.

Repeat:

1. Call `mcp__plugin_chipgraph_chipgraph__pending_decisions`. If `decisions` is empty,
   stop: nothing is waiting.
2. For **every** entry in `decisions`, start one subagent with the Agent tool:
   `subagent_type` = the entry's `agent` (`chipgraph:decider`), `model` = the entry's
   `model`, `description` = the entry's `description`, and `prompt` = the entry's
   `prompt`, word for word. Start all of them in one message, so they run in parallel,
   then wait until every one has finished.
3. Each subagent ends with one JSON object `{"value": ..., "confidence": ..., "reason":
   ...}`. For each, call `mcp__plugin_chipgraph_chipgraph__answer_decision` with the
   entry's `question_id` and those three values, unchanged. If a subagent gave no such
   object, or `answer_decision` refuses the value, start that entry's subagent once more
   and record its answer; if it fails again, leave the question pending and say so.
4. Go back to step 1. A question answered by the `small` tier with low confidence is not
   pending any more; it comes back (for the `large` tier, with its own model) only when
   the command that asked it runs `decide()` again.

Rules:

- Never answer a question yourself, and never change a value, a confidence or a model.
- Do not read or write files, run commands, or call any other tool.

At the end, list each question answered: its id, tier, model and value.
