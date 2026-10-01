---
description: Answer a question about this chip project from its Design Model and documents, with checked citations (file:line or model key); says "I don't know" when there is no source.
argument-hint: "<question>"
allowed-tools: Agent, mcp__plugin_chipgraph_chipgraph__ask_check
disallowed-tools: Bash, Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep, Skill, WebFetch, WebSearch
---

Answer this question about the project: `$ARGUMENTS`

You do not answer it yourself: the `chipgraph:asker` subagent does, from the sources the
chipgraph engine returns, and the engine checks every citation.

1. Start one subagent with the Agent tool: `subagent_type` = `chipgraph:asker`, `model` =
   `haiku`, `description` = "chipgraph ask", and `prompt` = the question above, word for
   word.
2. The subagent ends with one JSON object: `{"answer": ..., "citations": [...],
   "unknown": ...}`. Call `mcp__plugin_chipgraph_chipgraph__ask_check` with exactly those
   three values.
3. If `ok` is true, print the verified `answer`'s text, then `Sources:` and one line per
   verified citation: the citation, then its `text` from the check (first line only).
   If the verified answer has `unknown: true`, print that you do not know and why.
4. If `ok` is false, or the subagent returned no such object, do not print its answer:
   say "I don't know" and list the check's `reasons`.

Rules:

- Never answer from your own knowledge and never add a fact that is not in the
  verified answer.
- Do not read or write files, run commands, or call any other tool.
