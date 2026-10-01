---
description: Show this chip project's chipgraph status - the latest build run (rules done, failed, waiting at a gate) - in a few plain lines.
argument-hint: "[run id, default the latest]"
allowed-tools: mcp__plugin_chipgraph_chipgraph__config_show, mcp__plugin_chipgraph_chipgraph__status
disallowed-tools: Agent, Bash, Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep, Skill, WebFetch, WebSearch
---

Show the chipgraph status of this project. Run id: `$ARGUMENTS` (empty means the latest
run).

1. Call `mcp__plugin_chipgraph_chipgraph__config_show` (no arguments). If it fails
   because there is no `.chipgraph.yml`, print "chipgraph is not set up in this project:
   run /chipgraph:init-chipgraph" and stop. Otherwise keep the profile's `project` and
   the number of `blocks`.
2. Call `mcp__plugin_chipgraph_chipgraph__status`, with `run_id` set to the run id if
   one was given, and no arguments otherwise.
3. Print, in plain text and nothing more:
   - `project: <project> (<n> blocks)`;
   - if the result has `runs` and it is empty: `no build has run yet` (and that
     `/chipgraph:run` or `chipgraph build` starts one), then stop;
   - `run: <run_id>`, then `(finished)` when `stopped` is true, `(still running)`
     otherwise;
   - one line with every entry of `counts` (`<state>: <n>`);
   - each rule instance in `rules` whose state is `failed`, one per line;
   - each entry of `waiting_gates` (`<instance> waits for gate <gate>`; approve with
     `chipgraph approve`).

Rules:

- Print only what the tools returned; do not guess a state or a cause.
- Do not read or write files, run commands, or call any other tool.
