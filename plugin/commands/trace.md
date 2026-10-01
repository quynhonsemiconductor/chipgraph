---
description: Trace one requirement (REQ-ID) through the Design Model - its spec lines, the RTL that implements it and the tests that verify it - with path:line citations. Says so when the ID is unknown and lists close IDs.
argument-hint: "<REQ-ID>"
allowed-tools: mcp__plugin_chipgraph_chipgraph__model_find, mcp__plugin_chipgraph_chipgraph__model_trace, mcp__plugin_chipgraph_chipgraph__model_search, mcp__plugin_chipgraph_chipgraph__check
disallowed-tools: Agent, Bash, Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep, Skill, WebFetch, WebSearch
---

Trace this requirement: `$ARGUMENTS`. The ID is its first word; drop a leading
`requirement:` if it has one (`requirement:REQ-TIM-001` and `REQ-TIM-001` are the same).
Below, `<ID>` is that ID.

1. Find it: call `mcp__plugin_chipgraph_chipgraph__model_find` with `kind` =
   `requirement` and `name` = `<ID>`.
   - If the call fails (no Design Model yet), print "no Design Model yet: run
     `chipgraph ingest`" and stop.
   - If `results` is empty, the ID is unknown: go to step 5.
   - Otherwise keep the result's `key` (for example `requirement:REQ-TIM-001`).
2. Call `mcp__plugin_chipgraph_chipgraph__model_trace` with `key` = that key. It gives
   `implements` (the RTL entities), `verifies` (the tests), `derives_from` and the flags
   `no_implementation` and `no_test`.
3. Call `mcp__plugin_chipgraph_chipgraph__model_search` with `text` = the ID inside
   double quotes (`"<ID>"`, so the dashes are not read as operators) and `limit` = 20.
   Each result whose `citation` has the form `path:line` is a line of a project document
   (the spec) that names the requirement.
4. Call `mcp__plugin_chipgraph_chipgraph__check` with `only` = `["trace"]`. If it fails
   (no `trace` check in this project), say "tests: no trace check configured". Otherwise:
   if any issue has `rule` `req.no_test` and names `<ID>` in its `msg`, no test file
   names the requirement; if none does, a test file names it.
   Then print, in plain text:

   ```
   <ID> (<key>)
   spec:
     <path:line>  <text>          (one line per path:line result of step 3)
   rtl (implements): <entities, or "none linked in the Design Model">
   tests (verifies): <entities, or "none linked in the Design Model">
   trace check: <a test file names it | no test names it | not configured>
   derives from: <requirements, or "none">
   ```

   and stop.
5. Unknown ID: print "`<ID>` is not a requirement in the Design Model." Then look for
   close IDs: call `model_find` with `kind` = `requirement` and `name` = the ID with what
   follows its last `-` or `_` replaced by `*` (`REQ-NOPE-999` gives `REQ-NOPE-*`,
   `DMA_999` gives `DMA_*`). If that finds none, call it once more with only the part
   before the first `-` or `_`, then `*` (`REQ-*`), and `limit` = 10. Print the `name`
   of each result found as "close IDs: ...", or "no close requirement IDs in the Design
   Model" when both are empty.

Rules:

- Cite only what the tools returned: every `path:line` comes from a `citation`, every
  entity from a result. Never invent a link, a file or a line.
- Do not read or write files, run commands, or call any other tool.
