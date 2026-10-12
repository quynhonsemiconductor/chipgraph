---
description: Plan a block with the chipgraph Planner, show you the plan, and ask you to approve it at gate plan:<block>. Never approves without your answer.
argument-hint: "<block>"
allowed-tools: mcp__plugin_chipgraph_chipgraph__next_task, mcp__plugin_chipgraph_chipgraph__submit, mcp__plugin_chipgraph_chipgraph__plan_show, mcp__plugin_chipgraph_chipgraph__approve, mcp__plugin_chipgraph_chipgraph__status, Agent, AskUserQuestion
disallowed-tools: Bash, NotebookEdit, Skill, WebFetch, WebSearch
---

You plan one block with chipgraph, then a person decides at the gate `plan:<block>`.
Block: `$ARGUMENTS`. If it is empty, print `usage: /chipgraph:plan <block>` and stop.
The block is the only argument: no argument approves anything.

## A. The planner loop (target `plan:<block>`)

Repeat, at most 12 rounds (one round = one `next_task` call):

1. Call `mcp__plugin_chipgraph_chipgraph__next_task` with `target` = `plan:<block>`.
2. Check the stop conditions below. If one holds, stop the loop.
3. For **every** entry in `tasks` (and every `in_progress` entry no subagent of yours
   works on), start one subagent with the Agent tool: `subagent_type` = the entry's
   `agent` (`chipgraph:planner`), `model` = the entry's `model`, `description` =
   "chipgraph <task_id>", `prompt` = the entry's `prompt`, unchanged. Wait for it.
4. Call `mcp__plugin_chipgraph_chipgraph__submit` with its `task_id` and `result` =
   `{"status": "done" or "needs_human", "files_written": [...], "assumptions": [...],
   "open_questions": [...]}` from the subagent's report. Answer `status`:
   - `accepted`: done. `rejected`: the engine hands it out again next round (model
     `next_model`); start it as in step 3, with the `task_id` prompt only, never the
     `redo` text.
   - `budget_exhausted` or `needs_human`: the task is stopped. Never start it again.
5. Go back to step 1.

Stop conditions (check them after every `next_task`):

- `done: true`: the gate is approved already. Print "The plan of <block> is approved:
  run /chipgraph:rtl <block>." and end.
- A task ended `budget_exhausted` or `needs_human`: print its `label`, `stop_reason` and
  `handoff` (HANDOFF.md) and end.
- No `tasks` and no `in_progress` (`stopped: true`): go to part B.
- 12 rounds are done: print what is left and the `handoff` path, and end.

## B. The human gate `plan:<block>`

6. Call `mcp__plugin_chipgraph_chipgraph__plan_show` with `block`. If `exists` is false
   or `ok` is false, print its `errors`, every `blocked` entry of the last `next_task`
   (instance, reason, label) and its `handoff` path, and end: never approve a plan that
   fails `plan_check`. If `gate.status` is `approved`, print "The plan of <block> is
   approved: run /chipgraph:rtl <block>." and end.
7. Print the summary exactly, from that answer:
   - `path`, and `gate.status`;
   - every module in `modules` (build order): `name`, `summary`, `reqs`, `depends_on`,
     and each `writes` path;
   - REQ coverage: each `coverage` row, `req` -> its `modules`, or "unassigned:
     <reason>", or "NOT COVERED";
   - `warnings` (the `plan_check` warnings), `assumptions` and `open_questions`, or
     "none".
8. Ask the person with `AskUserQuestion`: question "Approve the plan of <block> at gate
   plan:<block>?", options "Approve", "Revise (the planner redoes it with your note)",
   "Stop".
9. Act on the answer, and only on the answer:
   - "Approve": call `mcp__plugin_chipgraph_chipgraph__approve` with `gate_id` =
     `gate.id`, `instance` = `gate.instance`, `note` = "approved in /chipgraph:plan".
     Do not pass `by`: the server records the local user. Print who approved (`by`)
     and "Next: /chipgraph:rtl <block>". End.
   - "Revise", or any text the person typed instead of an option: the note is that
     text; if they picked "Revise" without text, ask once more with `AskUserQuestion`
     "What should the planner change?" and use that answer. Call
     `mcp__plugin_chipgraph_chipgraph__approve` with `gate_id` = `gate.id`, `instance` =
     `gate.instance`, `reject` = true, `note` = the note. Then go back to part A, step 1:
     the engine hands the planner task out again with the note in its context. At most
     3 revisions; then end as for "Stop".
   - "Stop", or no answer: print the approve command `gate.approve` and end.
10. Headless (`claude -p`) or when `AskUserQuestion` is unavailable, errors or returns no
    answer: print "Not approved: no person answered." and the commands to run,
    `chipgraph plan show <block>` and `gate.approve`, then end. **Never approve without
    a person's answer.**

Rules:

- You never approve on your own: `approve` (without `reject`) is called only right after
  the person answered "Approve" to the question in step 8, in this session.
- Never "try once more" by hand: a task that is `budget_exhausted`, `needs_human` or
  `blocked` is never started again here.
- Do not write or edit any file and do not run shell commands: only the planner
  subagent writes its plan file. Do not call `get_context` yourself.
- Do not change a task's outputs, model or prompt; use what `next_task` returns.
