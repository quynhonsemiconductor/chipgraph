# chipgraph

chipgraph is an AI agent system for chip (IC) design. It treats a chip project as a
build graph: specs, RTL and tests are artifacts; rules produce them, and a rule can be
code, an AI agent or a human; deterministic EDA checks verify every step; humans approve
at gates. All of this runs over a typed Design Model of the chip.

Pre-alpha: the design is agreed (DESIGN.md) and implementation follows
docs/IMPLEMENTATION_PLAN.md.

## Development

```bash
uv sync
make check
```

## Learn more

- [`DESIGN.md`](DESIGN.md) — the design
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the reasons behind the design
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — tasks, dependencies, acceptance criteria
- [`AGENTS.md`](AGENTS.md) — how to work on this repository

## License

Apache-2.0
