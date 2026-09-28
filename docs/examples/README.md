# Example profiles

Profiles for real projects, kept as documentation. CI does not run them; the
end-to-end tests use `examples/tinysoc`.

| File | Project | What it shows |
|---|---|---|
| [`qsoc.chipgraph.yml`](qsoc.chipgraph.yml) | QSoC, a training SoC | Wrapping an existing `make` flow: one `cmd` adapter per check, `verilator` and `regex` parsers, 19 blocks |

## Trying the QSoC profile

On a clone of the QSoC repository:

```bash
cp <chipgraph>/docs/examples/qsoc.chipgraph.yml .chipgraph.yml
chipgraph config check
chipgraph check                       # every check, every block
chipgraph check --block pwm --only lint
make check                            # should agree on the per-block checks
```

Tried on 2026-09-28 with Verilator 5.052: 95 results (19 blocks × 5 checks), all
PASS where `make check` passes. An undeclared signal in a wrapper is reported by
both with the same `file:line`.
