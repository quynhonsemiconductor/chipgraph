# chipgraph plugin

A Claude Code plugin that runs the `chipgraph` MCP server inside your own Claude Code
session, with slash commands over it, and runs chipgraph's agent tasks there with your
own Claude plan: no separate API key, per D35 (`docs/DECISIONS.md`).

## Install

```
/plugin marketplace add quynhonsemiconductor/chipgraph
/plugin install chipgraph@chipgraph
```

The install id is `<plugin name>@<marketplace name>`; both are `chipgraph` here. Then
open Claude Code in your chip project (the repo root) and run `/chipgraph:status`.

## Requirements

- `uv` (and therefore `uvx`) on `PATH`. The plugin starts the server with
  `uvx chipgraph@0.1.0rc1 -C ${CLAUDE_PROJECT_DIR} mcp`, which downloads and caches
  `chipgraph` from PyPI on first use. Claude Code substitutes `${CLAUDE_PROJECT_DIR}`
  (the project root) before launch; without `-C` a plugin MCP server would run in the
  plugin's install directory, not your project.
- `python3` (3.9 or newer) on `PATH` for the write guard (`hooks/guard.py`, standard
  library only). If it is missing, Claude Code cannot start the hook and lets the call
  through: `submit` still rejects any change outside a task's outputs, but the write is
  not stopped when it happens. `chipgraph doctor` checks it.
- For `/chipgraph:trace` and `/chipgraph:ask`: a Design Model, built by
  `chipgraph ingest` in the project.

## Commands

Every command calls only chipgraph MCP tools (and, for the subagent loops, `Agent` with a
`chipgraph:*` subagent). Each command's frontmatter has `allowed-tools` (the tools it
uses, pre-approved for that turn) and `disallowed-tools`, which removes the file, shell,
skill and web tools while the command runs, so other plugins' skills and tools stay out
(`allowed-tools` alone does not restrict anything). `tests/plugin/test_plugin_commands.py`
checks both.

| Command | What it does | Example |
| --- | --- | --- |
| `/chipgraph:status [run]` | latest build run: rule counts, failed rules, gates waiting | `/chipgraph:status` |
| `/chipgraph:trace <REQ-ID>` | a requirement's spec lines (`path:line`), RTL and tests | `/chipgraph:trace REQ-TIM-001` |
| `/chipgraph:ask <question>` | an answer with checked citations, or "I don't know" | `/chipgraph:ask What is the reset value of CTRL?` |
| `/chipgraph:triage <log> [check]` | a failing log labelled infra, rtl, tb or spec | `/chipgraph:triage logs/lint.log lint` |
| `/chipgraph:init-chipgraph [options]` | preview, then write, a `.chipgraph.yml` | `/chipgraph:init-chipgraph --from-learn` |
| `/chipgraph:decide` | answer the questions `decide()` queued for a model | `/chipgraph:decide` |
| `/chipgraph:run [target]` | the build loop: role subagents do the agent tasks | `/chipgraph:run pulse/pulse_manifest` |

### `/chipgraph:status`

`config_show` (no `.chipgraph.yml`: it tells you to run `/chipgraph:init-chipgraph`),
then `status`: the run id and whether it finished, the count of rules per state,
each failed rule and each rule waiting at a gate (approve with `chipgraph approve`).
Open findings are not shown yet: no MCP tool lists them cheaply (`audit` reruns every
check); use `chipgraph findings`.

### `/chipgraph:trace REQ-TIM-001`

`model_find` (kind `requirement`) finds the ID; `model_trace` gives the entities that
implement it (RTL), verify it (tests) and that it derives from; `model_search` gives the
spec lines that name it, as `path:line` citations; the `trace` check (`check`, if the
project has one) says whether a test file names it. An unknown ID is reported as such,
with close IDs (`REQ-NOPE-*`, else `REQ-*`; `_` works like `-`, for IDs like `DMA_001`). The Design Model does not record
`implements`/`verifies` links yet, so RTL and tests usually show "none linked in the
Design Model"; the `trace` check line still covers the tests.

### `/chipgraph:ask <question>` (M1-13)

Answers from the Design Model and the project's documents, only with citations the
engine has checked:

- `ask_context(question, limit)` returns the sources: typed model lookups and FTS5 hits
  in the documents `chipgraph ingest` indexed (its inputs plus `README.md`, `AGENTS.md`,
  `doc/**/*.md`, `docs/**/*.md`; never an `nda` file), each with the citation to use,
  `model:<key>` or `path:line`.
- `ask_check(answer, citations, unknown)` verifies every citation; an answer that is not
  `unknown` needs at least one valid citation.

The command starts the `chipgraph:asker` subagent (haiku; only those two tools, no file
tools, DESIGN 4.5); the main session checks the final answer again and prints only a
verified one, or "I don't know". Outside Claude Code, `chipgraph ask "<question>"` does
the same with the profile's `models.providers`. Acceptance run (20 questions, graded):
`MAIN_MODEL=opus docs/ask-claude-code/run.sh /tmp/cg-ask-opus`.

### `/chipgraph:triage logs/lint.log lint` (M1-14)

Says which side has to change to fix a failing lint, simulation or check run: `infra`
(fix the environment, retry without counting a try), `rtl` or `tb` (the file:line), or
`spec` (ask the spec owner). A label is advice: it never blocks a build.

- `triage(path | log, check_id)` runs deterministic rules first (a missing tool or file,
  a time limit, a failing spec cross check, errors that all point into a testbench or all
  into RTL). When none decides, the question goes to `decide()`'s model tiers and the
  result is `deferred`.
- The command then runs the decider loop (also `/chipgraph:decide` on its own):
  `pending_decisions` lists each queued question with its tier's model; one
  `chipgraph:decider` subagent per question answers it; `answer_decision` records the
  answer; then `triage` runs again and picks it up. A small-model answer under
  `decide.small_min_confidence` queues the question once more for the large model.

The tiers come from the profile's `models.tiers` (default `haiku` and `opus`). Outside
Claude Code: `chipgraph triage LOG [--check ID] [--json]`. Acceptance run (22 labelled
logs, graded): `docs/triage-claude-code/run.sh /tmp/cg-triage-haiku`.

### `/chipgraph:init-chipgraph [--from-learn] [--project NAME] [--preset NAME] [--force] [--yes]`

Sets chipgraph up in a project that has no `.chipgraph.yml`. The MCP tool `init` wraps
`chipgraph init`: called with `confirm=false` it only returns the file it would write (a
stub, or, with `from_learn`, a profile and naming rules inferred from the repo like
`chipgraph init --from-learn`); with `confirm=true` it writes; it never overwrites an
existing `.chipgraph.yml` without `force=true`. The command prints the preview, asks you
(`AskUserQuestion`), and writes only on "Write them". `--yes` skips the question (for
`claude -p`, which cannot ask). Then: `chipgraph config check`, `chipgraph doctor`,
`chipgraph ingest`.

### `/chipgraph:run [target]` (M1-11)

The build loop. `next_task` → one role subagent per task, all in parallel → `submit` each
→ repeat until `done` or `waiting`. The main session never writes files itself.

- `next_task(target)`: runs the build; every agent rule it reaches becomes a task; it
  hands out the ready ones (each with `task_id`, `agent`, `model`, `outputs`), or says
  `done`, or `waiting` (a gate to approve, a question for a person, a failure).
- `get_context(task_id)`: the task's inputs as text, its outputs (the only files it may
  write), role, skills (with their text, `skill_texts`) and checks. Refused when an input
  is labelled `nda` (every model here is a cloud model), or is of a kind the task's role
  must not see (`rtl` for `tb-author`).
- `submit(task_id, result)`: the engine checks that only the task's outputs changed
  since it was handed out, that they exist, and runs the rule's checks; then accepts,
  or rejects with reasons and counts a try (`budget.tries`).
  A review task (`"reply": "review"`, agent `chipgraph:critic`, M2-09) writes no file:
  the main session passes the critic's JSON review as `result.review`; the engine
  validates it against the diff the critic was shown and writes the report itself.

Role subagents (`agents/`), one per role (DESIGN 5.1), generated from the role data in
`src/chipgraph/core/runtime/roles/data/` (`python -m
chipgraph.adapters.runtime.claude_code.agents --write`; a test keeps them equal). None
gets a shell or web tools:

| Agent | Tools | Writes | Model (tier, then after a rejection) |
|---|---|---|---|
| `chipgraph:author` | `Read, Glob, Grep, Write, Edit, MultiEdit`, `get_context` | the task's outputs | medium, then large |
| `chipgraph:tb-author` | `Write, Edit, MultiEdit`, `get_context` (no read tools) | the task's outputs | medium, then large |
| `chipgraph:critic` | `Read, Glob, Grep`, `get_context` | nothing (the engine writes its JSON review) | large |
| `chipgraph:planner` | `Read, Glob, Grep, Write, Edit, MultiEdit`, `get_context` | its plan file | large |
| `chipgraph:researcher` | `Read, Glob, Grep, Write, Edit, MultiEdit`, `get_context` | its proposal file | medium, then large |

`chipgraph:asker` and `chipgraph:decider` (which serves the `triage` role through
`decide()`) are hand-written. `next_task` picks the model per attempt from the role's tier
(or the rule's own `budget.tier`/`escalate`): `small` → haiku, `medium` → sonnet, `large` →
opus, or the profile's `models.tiers`. `tb-author` never sees RTL: `get_context` gives it
the spec and interface only, and the guard refuses it any read (below).
`/chipgraph:run` removes only `Bash, NotebookEdit, Skill, WebFetch, WebSearch`: its
subagents need the file tools, and the write guard bounds them. Acceptance runs on
tinysoc: `MAIN_MODEL=opus docs/runtime-claude-code/run.sh /tmp/cg-cc-opus`, and for the
roles `docs/roles-claude-code/run.sh /tmp/cg-roles-haiku`.

## MCP server

`chipgraph` (`.mcp.json`) has these tools: `status`, `build`, `check`, `approve`,
`config_show`, `audit`, `init`; the Design Model queries `model_block`, `model_module`,
`model_find`, `model_trace`, `model_impact`, `model_neighbors`, `model_search`;
`ask_context` and `ask_check`; `triage`; `pending_decisions` and `answer_decision`; and
`next_task`, `get_context`, `submit`. The server reloads the project on every call, so it
starts in a project with no `.chipgraph.yml` and sees one as soon as `init` writes it.
In Claude Code the tools are named `mcp__plugin_chipgraph_chipgraph__<tool>`.

## Write guard

`hooks/hooks.json` → `hooks/guard.py`, a `PreToolUse` hook on every tool. It lives in the
plugin's hooks, not in agent frontmatter, because frontmatter hooks do not run under
`claude -p` (spike S7). While chipgraph tasks are dispatched:

- a subagent is bound to a task when it calls `get_context` (one subagent, one task);
- a bound subagent may write only its own task's `outputs`; any other path, another
  task's outputs, or anything outside the project is refused;
- the main session may not write at all; subagents get no shell;
- each task has a tool-call budget (80 per dispatch): `--max-turns` counts only the main
  session, so the engine counts subagent calls itself;
- a task's `denied_reads` (its role's read policy over the project; for `tb-author`, every
  `rtl` artifact and its directory) is refused to its subagent: `Read` of a denied path,
  `Glob`/`Grep` rooted at it or at any directory above it (the project root included), and
  any other non-chipgraph tool whose input names one. `chipgraph:tb-author` gets no
  read-type tool at all, whatever the path, and no chipgraph tool but `get_context`;
- **fail closed**: any error (bad input, broken state) denies, with exit code 2.

**Limit:** the guard sees tool calls, not prompts. It cannot stop the *main session* from
reading RTL itself and pasting it into the prompt of a `tb-author` subagent: that text reaches
the subagent without any tool call. The tb-author independence rule therefore holds for the
subagent's own tools and for the context the engine builds (`get_context`), not for what the
main session writes into the prompt. The role's instructions tell the main session to pass
only the `task_id`; the acceptance report (`docs/roles-claude-code/report.py`) flags RTL text
in a tb-author prompt after the fact.

Outside a chipgraph run the guard makes no decision, except that `chipgraph:*` role
subagents never write or use a shell. Its state and log are under
`.chipgraph/state/runtime/` (gitignored). Tokens and cost come from the final `result`
event of `claude -p` (`usage`, `total_cost_usd`, `modelUsage`).

## Try a release candidate from TestPyPI

A tag `vX.Y.ZrcN` publishes to TestPyPI only (`docs/RELEASING.md`). To try it before it is
on PyPI, run a copy of the plugin whose server comes from TestPyPI:

```bash
cp -R plugin /tmp/chipgraph-plugin-rc
cat > /tmp/chipgraph-plugin-rc/.mcp.json <<'JSON'
{"mcpServers": {"chipgraph": {"command": "uvx",
  "args": ["--index", "https://test.pypi.org/simple/", "--index-strategy", "unsafe-best-match",
           "chipgraph@0.1.0rc1", "-C", "${CLAUDE_PROJECT_DIR}", "mcp"]}}}
JSON
cd /path/to/your/project
claude --plugin-dir /tmp/chipgraph-plugin-rc
```

`--index` puts TestPyPI before PyPI. uv's default strategy would then take every package
that TestPyPI also has from TestPyPI alone, and TestPyPI holds stray uploads of common
packages, so resolution fails; `unsafe-best-match` picks the best version across both.
TestPyPI is not vetted: use this only to try an RC, never as your normal setup. Check the
server first with `uvx --index https://test.pypi.org/simple/ --index-strategy
unsafe-best-match chipgraph@0.1.0rc1 --version`.

## Run the current checkout (dev plugin)

The plugin's commands and agents call the server by its plugin-scoped name
(`mcp__plugin_chipgraph_chipgraph__*`), so the server must come from the plugin itself:
make a copy of `plugin/` whose `.mcp.json` runs the checkout, and load that copy.

```bash
git clone https://github.com/quynhonsemiconductor/chipgraph.git ~/src/chipgraph
cd ~/src/chipgraph && uv sync
cp -R plugin /tmp/chipgraph-plugin-dev
cat > /tmp/chipgraph-plugin-dev/.mcp.json <<JSON
{"mcpServers": {"chipgraph": {"command": "uv",
  "args": ["run", "--project", "$HOME/src/chipgraph", "chipgraph", "-C", "\${CLAUDE_PROJECT_DIR}", "mcp"]}}}
JSON
cd /path/to/your/project
claude --plugin-dir /tmp/chipgraph-plugin-dev
```

Run `chipgraph doctor` in the project first. If the marketplace plugin is also installed,
disable it for this session (`/plugin`), so the two copies do not both run. See
`docs/spikes/S5.md` for how the MCP server is started and tested.

The acceptance run of this plugin (M1-16) does exactly this on a tinysoc copy and runs
each command once, headless: `docs/plugin-claude-code/run.sh /tmp/cg-plugin-haiku`.

## Version

The plugin (`.claude-plugin/plugin.json`), its `.mcp.json` pin and `pyproject.toml` are
kept at the same version (currently `0.1.0rc1`); `tests/test_release_metadata.py` checks
they agree. See `docs/RELEASING.md` for how a release is cut.
