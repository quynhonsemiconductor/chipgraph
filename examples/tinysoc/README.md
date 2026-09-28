# tinysoc — a chipgraph example project

A tiny, open-source SystemVerilog SoC used to exercise chipgraph end to end
(`tests/e2e/`). Not a real chip: three small blocks, wired together to show a
build graph, a `cmd` tool adapter and the Verilator log parser working together.

## Layout

- `rtl/` — `tiny_timer.sv` (counter + compare + IRQ), `tiny_gpio.sv` (in/out/dir
  registers), `tiny_top.sv` (instantiates both behind a 4-bit register bus).
- `filelists/<block>.f` — one Verilator filelist per block (`timer`, `gpio`, `top`),
  paths relative to this directory.
- `Makefile` — `make lint BLOCK=<block>` runs `verilator --lint-only -Wall`;
  `make lint-all` runs it for every block; `make clean` removes build output.
- `scripts/gen_manifest.py` — writes `build/<block>.manifest.json`: the block's
  filelist and the sha256 of each source file, deterministic given the sources.
- `.chipgraph.yml` — the project profile: the `tinysoc` pack, and the `lint`
  adapter (`cmd` + the `verilator` log parser).
- `.chipgraph/packs/tinysoc/` — the project's own pack: a `human` rule
  (`tinysoc/rtl`) that declares each block's RTL as an artifact, and a `gen` rule
  (`tinysoc/lint_manifest`) that writes the manifest and then runs the `lint`
  check against that block.

## Running it

Requires [Verilator](https://verilator.org/) and `make` on `PATH`.

This directory lives inside the chipgraph repository, whose git root would hold the
run state. Work on a copy with its own git repository:

```bash
cp -r examples/tinysoc /tmp/tinysoc && git -C /tmp/tinysoc init -q
cd /tmp/tinysoc
chipgraph config check                 # validate the profile
chipgraph doctor                       # confirm make and verilator are on PATH
chipgraph check                        # run `lint` directly, for every block
chipgraph build tinysoc/lint_manifest  # build and lint every block through the graph
chipgraph status                       # see what the last run did
```

`chipgraph build tinysoc/lint_manifest` builds one instance per block (`timer`,
`gpio`, `top`) plus the `tinysoc/rtl` instances it depends on. `chipgraph build '*'`
builds the whole graph, which is the same here. There is no `check:all` target
name yet.

Known limit: a block's rule lists only its own RTL file as input, so `top` is not
rebuilt when only `tiny_gpio.sv` changes, although its filelist includes it.
`chipgraph check` still lints every block. Dependencies read from filelists come
with the Design Model (M1).
