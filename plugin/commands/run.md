---
description: Run the chipgraph build loop - hand each ready agent task to its role subagent, submit it, repeat until the build is done or waiting.
argument-hint: "[target, default *]"
allowed-tools: mcp__plugin_chipgraph_chipgraph__next_task, mcp__plugin_chipgraph_chipgraph__submit, Agent
---

You drive a chipgraph build. The chipgraph engine decides every task, the files each
task may write, and the checks it must pass. You only dispatch the work and hand it
back. Build target: `$ARGUMENTS` (use `*` if that is empty).

Repeat:

1. Call `mcp__plugin_chipgraph_chipgraph__next_task` with `target` set to the build
   target.
2. If the answer has `done: true`: report that the build finished and stop.
   If `tasks` and `in_progress` are both empty: report `waiting` and each `blocked`
   entry (a gate to approve, a person to answer, a failure) and stop.
3. For **every** entry in `tasks`, start one subagent with the Agent tool:
   `subagent_type` = the entry's `agent` (for example `chipgraph:author`), `model` = the
   entry's `model`, `description` = "chipgraph <task_id>", and `prompt` = the entry's
   `prompt`. Start all of them in one message, so they run in parallel, then wait
   until every one of them has finished.
   An `in_progress` entry was dispatched before and never submitted. If none of your
   subagents is working on it, start one for it the same way.
4. For every task a subagent finished, call `mcp__plugin_chipgraph_chipgraph__submit`
   with its `task_id` and `result` built from the subagent's report:
   `{"status": "done" or "needs_human", "files_written": [...], "assumptions": [...],
   "open_questions": [...]}`. Report whether it was accepted; if it was rejected, say
   why (the `reasons`).
5. Go back to step 1. A rejected task comes back from `next_task` with its reasons,
   until its tries are used up.

Rules:

- Do not write or edit any file yourself and do not run shell commands: only the role
  subagents write, and only their task's outputs. The chipgraph guard refuses anything
  else.
- Do not call `get_context` yourself; it is the subagent's first call.
- Do not change a task's outputs, role or model; use what `next_task` returns.

At the end, give a short summary: tasks accepted, tasks rejected (with reasons), and
what the build is waiting for, if anything.
