# AGENTS.md — working on the chipgraph repository

`chipgraph` is an AI agent system for chip (IC) design. It treats a chip project as a
**build graph**: specs, RTL, tests and reports are artifacts; rules produce them (code,
AI agents or humans); deterministic EDA checks verify every step; humans approve at gates.
All of this runs over a typed **Design Model** of the chip.

This file tells coding agents (and humans) how to work on this repository.

## Read first, in this order

1. [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md): find your task ID, its `depends`, `writes` and `accept`.
2. [`DESIGN.md`](DESIGN.md): the section your task refers to. It is written in Vietnamese; identifiers are English.
3. [`docs/DECISIONS.md`](docs/DECISIONS.md): the reasons behind the design. Do not re-litigate a decision in code; propose an ADR instead.

## Hard rules

1. **One task, one PR.** Branch `feat/<task-id>-<slug>`, for example `feat/m0-06-graph`. Take a task only when every `depends` is merged.
2. **Stay inside `writes`.** If you need to change another file, open an issue and stop.
3. **Contracts are frozen.** Do not change `src/chipgraph/core/contracts/` or `src/chipgraph/core/plugin_api/` unless your task says so. Other agents build against them.
4. **Core knows nothing about chips or tools.** `chipgraph.core` must not import `chipgraph.adapters` or any pack, and must not contain project, bus, PDK or tool names (QSoC, APB, GF180, Verilator). `import-linter` enforces the import rule in CI.
5. **Tests for every `accept` criterion.** CI never calls a real model: use `adapters/llm/fake.py`. Real-model evals run only in the nightly `evals` job.
6. **Public repository.** Never commit secrets, API keys, internal or NDA data, or files from private projects. Examples and fixtures use open-source IP and PDKs only (`examples/tinysoc`, GF180, Sky130).
7. **Nothing from the teacher's flow.** Do not copy files, prompts or code from `VLSIT_RTL_Generator_AI_Model`. chipgraph is independent.
8. **Commits and PRs** use Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Do not add AI attribution lines ("Co-Authored-By", "Generated with …") to commits or PR descriptions.

## Commands

```bash
uv sync                       # install (Python 3.14; 3.13 supported)
uv run chipgraph --help       # CLI
uv run pytest                 # all tests
uv run pytest tests/e2e       # end-to-end on examples/tinysoc (needs the EDA tools or the chipgraph-eda image)
uv run ruff check . && uv run ruff format --check .
uv run mypy src/chipgraph/core
uv run lint-imports           # dependency rules
make schemas                  # regenerate schemas/ from the pydantic contracts
```

## Layout

```
src/chipgraph/core/        contracts, engine, model, runtime, state, config, plugin_api
src/chipgraph/adapters/    tool, parser, format, llm, runtime, runner, vcs, review
src/chipgraph/checks/      built-in checks
src/chipgraph/cli/  mcp/   user surfaces
packs/<name>/pack.yml      domain packs: rules, roles, skills, schemas, templates
interfaces/ presets/ orgs/ data, not code
plugin/                    Claude Code plugin
examples/tinysoc/          open-source sample project for end-to-end tests
evals/                     Inspect AI evals
docs/                      plan, decisions, research, spikes, pilots
```

## Code style

- Modern typing (PEP 604 unions, PEP 695 generics). `mypy --strict` for `core/`.
- Data models are pydantic v2 with a `schema_version` field. Data files (rules, packs, profiles) have a JSON Schema in `schemas/`.
- Async I/O with `asyncio`. Subprocesses go through a `Runner`, never direct `subprocess` calls in core.
- Every adapter returns the shared result schema (`CheckResult`, `AgentResult` …), never raw logs.
- Names: packs, rules, checks, roles and skills are `lower_snake_case` with a namespace, `<pack>/<name>`.
- Code, comments and docstrings in English.

## Definition of done

See `docs/IMPLEMENTATION_PLAN.md`, section 10. In short: only `writes` touched, tests for
every `accept`, CI green, docs updated, no secrets, and a PR description with the task ID,
what changed, how it was verified and remaining risks.
