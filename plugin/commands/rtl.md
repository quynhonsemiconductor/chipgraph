---
description: Build a block's RTL from its approved plan to a pull request - one RTL author per module, the testbench author, the Critic, then the branch or PR waiting for a reviewer.
argument-hint: "<block>"
allowed-tools: mcp__plugin_chipgraph_chipgraph__next_task, mcp__plugin_chipgraph_chipgraph__submit, mcp__plugin_chipgraph_chipgraph__plan_show, mcp__plugin_chipgraph_chipgraph__status, Agent
disallowed-tools: Bash, NotebookEdit, Skill, WebFetch, WebSearch
---

You drive the chipgraph target `rtl:<block>`: the engine decides every task, its files,
its checks and its model; you dispatch the work and hand it back. Block: `$ARGUMENTS`.
If it is empty, print `usage: /chipgraph:rtl <block>` and stop.

1. Call `mcp__plugin_chipgraph_chipgraph__plan_show` with `block`. If `gate.status` is
   not `approved`, print "The plan of <block> is not approved (gate plan:<block> is
   <status>): run /chipgraph:plan <block> first." and end.

2. Repeat, at most 20 rounds (one round = one `next_task` call):
   a. Call `mcp__plugin_chipgraph_chipgraph__next_task` with `target` = `rtl:<block>`.
   b. Check the stop conditions below. If one holds, go to step 3.
   c. For **every** entry in `tasks` (and every `in_progress` entry no subagent of yours
      works on), start one subagent with the Agent tool: `subagent_type` = the entry's
      `agent` (`chipgraph:rtl-author` for a module, `chipgraph:tb-author` for the
      testbench, `chipgraph:critic` for the review), `model` = the entry's `model`,
      `description` = "chipgraph <task_id>", `prompt` = the entry's `prompt`, unchanged.
      Start all of them in one message so they run in parallel; wait for every one.
   d. For every finished subagent call `mcp__plugin_chipgraph_chipgraph__submit` with
      its `task_id` and `result` = `{"status": "done" or "needs_human",
      "files_written": [...], "assumptions": [...], "open_questions": [...]}` from its
      report. For an entry with `"reply": "review"` (the Critic) pass its JSON review
      unchanged: `result` = `{"status": "done", "review": <that JSON object>}`. Answer
      `status`:
      - `accepted`: done.
      - `rejected`: the engine hands it out again next round (model `next_model`):
        start it as in c, with the entry's `prompt` only, never the `redo` text.
      - `budget_exhausted` or `needs_human`: the task is stopped. Never start it again.
   e. Go back to a.

   The steps `open_pr` (the branch and pull request) and `pr_gate` are the engine's:
   they run inside `next_task`, never as a subagent. They show up only in `blocked`.

   Stop conditions (check them after every `next_task`):
   - `done: true`: the pull request gate is approved already. Go to step 3.
   - No `tasks` and no `in_progress` (`stopped: true`): go to step 3.
   - A task ended `budget_exhausted` or `needs_human`: go to step 3.
   - 20 rounds are done: go to step 3.

3. Call `mcp__plugin_chipgraph_chipgraph__status` with `run_id` = the `run_id` of the
   last `next_task` answer, and print, from its `blocks.<block>`:
   - each entry of `tasks`: `instance`, `status`, `files`, and each of its `checks`
     (`check_id`: `status`);
   - the Critic: `review.verdict`, `review.summary` and every `review.comments` entry
     (`severity` `file`:`line` `claim`), or "no review";
   - the pull request: `pr.url`, or else the local branch `pr.branch` at `pr.head_sha`;
     or "no pull request yet";
   - then every `blocked` entry of the last `next_task` (instance, reason, label) and
     its `handoff` path. When the only one is the gate `pr:<block>`, print "Waiting for
     a reviewer at gate pr:<block>: approve the pull request on the platform, or with
     the file adapter `chipgraph approve pr:<block>`; merging is yours."

Rules:

- Never approve a gate, never merge: the gates are a person's.
- Never "try once more" by hand: a task that is `budget_exhausted`, `needs_human` or
  `blocked` is never started again in this run.
- Never start more subagents than the engine hands out: one per `tasks` entry (or
  stalled `in_progress` entry) per round.
- Do not write or edit any file and do not run shell commands: only the role subagents
  write, and only their task's outputs. Do not call `get_context` yourself.
- Do not change a task's outputs, role, model or prompt; use what `next_task` returns.
