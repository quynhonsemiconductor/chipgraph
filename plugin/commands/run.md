---
description: Run the chipgraph build loop - hand each ready agent task to its role subagent, submit it, repeat until the build is done or the engine stops it.
argument-hint: "[target, default *]"
allowed-tools: mcp__plugin_chipgraph_chipgraph__next_task, mcp__plugin_chipgraph_chipgraph__submit, Agent
disallowed-tools: Bash, NotebookEdit, Skill, WebFetch, WebSearch
---

You drive a chipgraph build. The chipgraph engine decides every task, the files each
task may write, the checks it must pass, the model of every attempt, and when a task
has used its budget. You only dispatch the work and hand it back. Build target:
`$ARGUMENTS` (use `*` if that is empty).

Repeat, at most 12 rounds (one round = one `next_task` call):

1. Call `mcp__plugin_chipgraph_chipgraph__next_task` with `target` set to the build
   target.
2. Check the stop conditions below. If one holds, stop.
3. For **every** entry in `tasks`, start one subagent with the Agent tool:
   `subagent_type` = the entry's `agent` (for example `chipgraph:author`), `model` = the
   entry's `model`, `description` = "chipgraph <task_id>", and `prompt` = the entry's
   `prompt`, unchanged. Start all of them in one message, so they run in parallel, then
   wait until every one of them has finished.
   An `in_progress` entry was dispatched before and never submitted. If none of your
   subagents is working on it, start one for it the same way.
4. For every task a subagent finished, call `mcp__plugin_chipgraph_chipgraph__submit`
   with its `task_id` and `result` built from the subagent's report:
   `{"status": "done" or "needs_human", "files_written": [...], "assumptions": [...],
   "open_questions": [...]}`. For a task whose entry has `"reply": "review"` (agent
   `chipgraph:critic`), the subagent writes no file and ends with one JSON review
   object: pass it unchanged as `result` = `{"status": "done", "review": <that JSON
   object>}`; the engine checks it and writes the review report itself.
   The answer's `status` is one of:
   - `accepted`: done.
   - `rejected`: the engine hands the task out again in the next round, with model
     `next_model`. Start it exactly as in step 3, with the entry's `prompt` and
     `model`: pass the `task_id` only, never the `redo` text, the `label` or the
     failures yourself. The subagent's context (`get_context`) carries the redo
     instruction.
   - `budget_exhausted` or `needs_human`: the task is stopped. Never start it again.
     Note its `label`, `stop_reason` and `handoff`.
5. Go back to step 1.

Stop conditions (check them after every `next_task`):

- `done: true`: the build finished. Stop.
- No `tasks` and no `in_progress` (the answer then has `stopped: true`): stop.
- Only `blocked` entries are left, nothing to start: stop.
- 12 rounds are done: stop, even if tasks are left.

When you stop, print every `blocked` entry (instance, reason, label) and the `handoff`
path (HANDOFF.md, the summary for the person who takes over), then end.

Rules:

- Never start more subagents than the engine hands out: one per `tasks` entry (or
  stalled `in_progress` entry) per round.
- Never "try once more" by hand: a task with status `budget_exhausted` or
  `needs_human`, or one listed under `blocked`, is never started again in this run.
  Only the engine decides whether a task runs again.
- Do not write or edit any file yourself and do not run shell commands: only the role
  subagents write, and only their task's outputs. The chipgraph guard refuses anything
  else.
- Do not call `get_context` yourself; it is the subagent's first call.
- Do not change a task's outputs, role, model or prompt; use what `next_task` returns.

At the end, give a short summary: tasks accepted, tasks rejected and retried (with
their labels), tasks stopped (status, label, reason), what the build is waiting for,
and the HANDOFF path.
