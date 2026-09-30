# tinysoc — a chipgraph example project

A tiny, open-source SystemVerilog SoC used to exercise chipgraph end to end
(`tests/e2e/`). Not a real chip: three small blocks, wired together to show a
build graph, a `cmd` tool adapter and the Verilator log parser working together.

## Layout

- `rtl/` — `tiny_timer.sv` (counter + compare + IRQ), `tiny_gpio.sv` (in/out/dir
  registers), `tiny_top.sv` (instantiates both behind a 4-bit register bus).
- `chip.yml` — the chip-level spec (chipgraph's own `chip-yaml` format): the `clk`/`rst_n`
  domain, the three blocks, and `timer`'s one interrupt. Read by `chipgraph ingest`.
- `doc/specs/<BLOCK>_MAS.md` — a short MAS per block (`TINY_TIMER`, `TINY_GPIO`),
  the seed for the `spec-core` `mas-markdown` extractor; kept truthful to `rtl/`.
- `filelists/<block>.f` — one Verilator filelist per block (`timer`, `gpio`, `top`),
  paths relative to this directory.
- `Makefile` — `make lint BLOCK=<block>` runs `verilator --lint-only -Wall`;
  `make lint-all` runs it for every block; `make clean` removes build output.
- `scripts/gen_manifest.py` — writes `build/<block>.manifest.json`: the block's
  filelist and the sha256 of each source file, deterministic given the sources.
- `.chipgraph.yml` — the project profile: the `tinysoc` pack, and the `lint`
  adapter (`cmd` + the `verilator` log parser).
- `.chipgraph/packs/tinysoc/` — the project's own pack: a `human` rule
  (`tinysoc/rtl`) that declares each block's RTL as an artifact behind a `spec:{block}`
  gate, and a `gen` rule (`tinysoc/lint_manifest`) that writes the manifest and then
  runs the `lint` check against that block.
- `.chipgraph/decisions/` — the `baseline` decisions recorded by `chipgraph baseline`
  (DESIGN.md 6.4): the example ships as an already-baselined project, so `tinysoc/rtl`
  builds without stopping at its spec gate. Each decision pins the hashes of a block's
  reviewed inputs; editing one (e.g. a MAS) returns that gate to "waiting".

## The spec gate and `baseline`

`tinysoc/rtl` is gated by `spec:{block}`: a person approves the block's reviewed
inputs before its RTL is built on top of them (DESIGN.md 6.1). Those inputs are the
block's MAS (`doc/specs/TINY_{BLOCK}_MAS.md`, present for `timer` and `gpio`) and its
filelist (`filelists/{block}.f`, present for every block). `top` has no MAS, so its
gate covers only its filelist — it still has a tracked artifact to baseline.

An existing project like this one has reviewed those inputs through PRs but has no
chipgraph decision for them, so the first build would stop at every spec gate.
`chipgraph baseline` records that history as one `baseline` decision per gate over its
clean, tracked inputs, at their current hash (DESIGN.md 6.4). This example ships those
decisions under `.chipgraph/decisions/`, so `build` passes the spec gates out of the
box; editing a reviewed input (e.g. a MAS) changes its hash and returns that gate to
"waiting", and the RTL must be re-approved.

## Running it

Requires [Verilator](https://verilator.org/) and `make` on `PATH`.

This directory lives inside the chipgraph repository, whose git root would hold the
run state. Work on a copy with its own git repository:

```bash
cp -r examples/tinysoc /tmp/tinysoc && git -C /tmp/tinysoc init -q
cd /tmp/tinysoc
chipgraph config check                 # validate the profile
chipgraph doctor                       # confirm make and verilator are on PATH
chipgraph ingest                       # build the Design Model from chip.yml, RTL and MAS
chipgraph baseline                     # list the artifacts each spec gate covers (dry run)
chipgraph check                        # run `lint` directly, for every block
chipgraph build tinysoc/lint_manifest  # build and lint every block through the graph
chipgraph status                       # see what the last run did
```

`chipgraph baseline --confirm` records the `baseline` decisions (the example already
ships them). Delete `.chipgraph/decisions/` in your copy to see the gates start out
"waiting" and baseline them yourself.

`chipgraph build tinysoc/lint_manifest` builds one instance per block (`timer`,
`gpio`, `top`) plus the `tinysoc/rtl` instances it depends on. `chipgraph build '*'`
builds the whole graph, which is the same here. There is no `check:all` target
name yet.

Known limit: a block's rule lists only its own RTL file as input, so `top` is not
rebuilt when only `tiny_gpio.sv` changes, although its filelist includes it.
`chipgraph check` still lints every block. Dependencies read from filelists come
with the Design Model (M1).
