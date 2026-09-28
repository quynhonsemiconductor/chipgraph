# Contributing

Work is organized as tasks in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md),
each with an ID, `depends`, `writes` and `accept` criteria. The working rules are in
[`AGENTS.md`](AGENTS.md) — read it before opening a PR.

In short:

- Pick a task whose `depends` are already merged. Only touch files in its `writes`.
- One task, one PR. Branch name: `feat/<task-id>-<slug>` (e.g. `feat/m0-06-graph`).
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Run `make check` (lint, type, test) before pushing.
- Do not add AI attribution lines ("Co-Authored-By", "Generated with …") to commits or
  PR descriptions.
- A PR description names the task ID, what changed, how it was verified, and remaining
  risks.
