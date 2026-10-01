---
description: Set chipgraph up in this project - preview the .chipgraph.yml that `chipgraph init` would write (a stub, or inferred from the repo with --from-learn), ask, then write it. Works with no .chipgraph.yml; never overwrites one without --force.
argument-hint: "[--from-learn] [--project NAME] [--preset NAME] [--force] [--yes]"
allowed-tools: mcp__plugin_chipgraph_chipgraph__init, AskUserQuestion
disallowed-tools: Agent, Bash, Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep, Skill, WebFetch, WebSearch
---

Set chipgraph up in this project. Options: `$ARGUMENTS`.

Read the options: `--from-learn` (infer the profile from the repo instead of a stub),
`--project NAME`, `--preset NAME`, `--force` (overwrite an existing `.chipgraph.yml`),
`--yes` (the user already agreed: write without asking). Pass on only the options that
were given: `from_learn` true, `project`, `preset`, `force` true. Ignore any other word
and say so.

1. Call `mcp__plugin_chipgraph_chipgraph__init` with those options and `confirm` =
   false. This only previews: it writes nothing.
2. Print the preview: `.chipgraph.yml` and its `content` in a `yaml` code block, then
   each entry of `other_files` (its `path`, then its `content` in a `yaml` code block).
   Print the content exactly as returned.
3. If `exists` is true and `--force` was not given: print the `message` (the file is
   kept; `--force` overwrites it) and stop.
4. If `--yes` was given, go to step 5. Otherwise ask the user with `AskUserQuestion`:
   "Write these files?" with the choices "Write them" and "Cancel". If the user does not
   pick "Write them", or the question cannot be asked, print "Nothing was written. To
   write it, run /chipgraph:init-chipgraph with the same options and --yes." and stop.
5. Call `init` again with exactly the same options and `confirm` = true. Print the
   `files_written`, then "Next steps:" and each entry of `next_steps`.

Rules:

- Write only through the `init` tool, and only after step 4 allows it. Never call it
  with `confirm` = true or `force` = true unless the options above say so.
- Do not read or write files yourself, run commands, or call any other tool.
