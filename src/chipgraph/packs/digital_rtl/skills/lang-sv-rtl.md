---
id: lang-sv/rtl
description: How to write one synthesizable SystemVerilog module from its plan entry, requirements and interface, in the project's style.
roles: [rtl-author]
version: 0.1.0
---
Write one synthesizable SystemVerilog module per output file (`outputs[0]`), named as
`rtl.module.name`. You see the plan, the requirements, the interface, the naming rules and
the style exemplars; you never see a testbench, and must not guess one.

What to use from the context:

- `rtl.module`: the module's entry of the approved plan: its summary, the requirement ids
  it covers (`reqs`), its ports, the modules it depends on and the files it may write.
  Stay inside it: no file outside `outputs`, no port the entry does not list, no logic
  that belongs to another module of the plan.
- `rtl.interface.ports`: the module's ports, exactly (name, direction, width). A port with
  `source: model` is the Design Model's; one with `source: plan` is internal to the block,
  as a person approved it. Do not add, drop or rename a port.
- `rtl.dependencies`: the modules this one instantiates or connects to, declarations only.
  Instantiate them with named port connections (`.port(signal)`), every port connected.
- `rtl.requirements`: what the module must do. Implement each one, and put its id in a
  comment next to the logic that implements it: `// REQ-TIM-001: ...`.
- `rtl.style.exemplars`: files of this project. **Follow their style where it differs
  from your habits**: indentation, `begin` placement, how ports are declared and aligned,
  the reset (asynchronous or synchronous, active low or high, its name) and the clocked
  block, signal suffixes (`_q`/`_d`, `_r`/`_next`, ...), the comment header, blank lines,
  line length. `rtl.style.profile` is the same, measured; the `style` check compares your
  file with it (the keys in `rtl.style.enforce` fail the check).

Synthesizable RTL:

- `logic` for every signal; `always_ff` for registers, `always_comb` for logic, `assign`
  for simple wires. No `initial` blocks for state, no `#` delays, no `$display`.
- One clock and one reset convention, the exemplars' (asynchronous or synchronous, the
  active level, the reset's name); every register is reset.
- No latches: in `always_comb`, give every signal a value on every path (a default
  assignment first, or a `default` branch in every `case`).
- Every output is driven; every input bit is used (see lint, below).
- Width-explicit literals (`8'd0`, `1'b1`, `32'h0000_0010`); parameters (`localparam`) for
  every constant with a meaning, not magic numbers; never retype an address or size the
  spec names as a number: the `hardcode` check rejects it.
- Nonblocking `<=` in `always_ff`, blocking `=` in `always_comb`, never mixed.

Lint is Verilator with `-Wall`, and any warning fails the check:

- Every input bit is used, or consumed on purpose: for bits the module ignores, write
  `logic unused_ok; assign unused_ok = &{1'b0, wdata[31:8]};` (Verilator ignores a signal
  whose name contains `unused`). Do the same for a part of an internal signal you never
  read.
- No signal declared and never read, no width mismatch in an assignment or comparison
  (size both sides, or slice and extend explicitly), no output left unconnected on an
  instance (connect it to a named `unused_*` signal).
- No `lint_off` pragmas, unless the style exemplars use them.

The engine runs the project's formatter on your file, then lint, naming, style and
hardcode. If the requirements or the interface are ambiguous or contradict each other, do
not guess: stop and report `needs_human` with the question.
