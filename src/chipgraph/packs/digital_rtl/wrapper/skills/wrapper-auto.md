---
id: rtl/wrapper-auto
description: How to write a wrapper's source around a vendor IP in the emacs verilog-mode AUTO style, which a generator expands.
roles: [rtl-author]
version: 0.1.0
---
You write the **source** of a wrapper around a third-party IP: `outputs[0]`, a
`.src.sv` file. You never write its expansion. A generator copies your source, runs emacs
verilog-mode on it (`verilog-batch-auto`) with the settings in
`rtl.wrapper.generator.pinned`, and writes the expanded wrapper that the build compiles.
You cannot run emacs: on submit `wrap_check` expands your source and reports, with the
expanded lines, every IP port it left unmapped. Fix exactly what it reports.

What to use from the context:

- `rtl.wrapper.vendor`: the IP module's header: its parameters and its ports (name,
  direction, type, dimensions). That is all you see of the IP, on purpose.
- `rtl.wrapper.interface`: the wrapper's own ports when the project knows them (Design
  Model or plan). Your template must produce exactly those names. When it is empty,
  name each wrapper port from the IP port it comes from, by the naming rules.
- `rtl.naming.text`: the project's naming rules (see the `org/naming` skill).
  Every name the expansion declares (ports, wires) comes from your template, so the
  template is where the naming rules are applied.
- `rtl.wrapper.exemplar`, when present, and its text in `inputs`: another wrapper of
  this project. Imitate its structure (header comment, port groups, comments, how the
  template is laid out), not its IP or its names.

## The file you write

```systemverilog
// <header comment: what the wrapper is, which IP, "edit this source only">

module <rtl.wrapper.name> (
  // clock and reset
  /*AUTOINPUT("^i_clk\|^i_rst")*/
  // <a group per function: one AUTOINPUT/AUTOOUTPUT per kind of port>
  /*AUTOINPUT("^i_push\|^i_data")*/
  /*AUTOOUTPUT("^o_full")*/
  // others: must stay empty once the template maps every IP port
  /*AUTOINPUT*/
  /*AUTOOUTPUT*/
);

  /*AUTOWIRE*/

  /*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/

  /* <ip module> AUTO_TEMPLATE (
      .rst_ni     (i_rst_n),        // one port: rename
      .testmode_i (1'b0),           // tie an input off
      .err_o      (unused_err),     // sink an output the wrapper does not use
      .\(.*\)_i   (i_\1[]),         // a regexp: every other `<x>_i` becomes `i_<x>`
      .\(.*\)_o   (o_\1[]),
  ); */
  <ip module> #(
    .<Param> (<value>)              // every parameter the spec fixes, set here
  ) u_<function> (/*AUTOINST*/);

endmodule
```

The AUTO idioms:

- `/*AUTOINST*/` inside the instance's parentheses: emacs writes one `.port (signal),`
  line per IP port, from the template. Write nothing else in those parentheses.
- `/* <module> AUTO_TEMPLATE ( ... ); */`, a comment just before the instance: one line
  per rule, `.<ip port or regexp> (<signal>),`. The first rule that matches a port wins,
  so put single ports first and catch-all regexps last. `\(...\)` captures part of the
  port's name and `\1` reuses it; `[]` keeps the IP port's width (`o_data[7:0]`); a
  slice is written out (`o_data[3:0]`). The template's module name must be exactly the
  IP module's name, or no rule applies.
- `@"(...)"` in a template line is a Lisp expression, e.g.
  `(o_bus_@"(downcase (symbol-name '\1))"[])`. Only the functions in
  `rtl.wrapper.generator.lisp_functions` and the variables in `lisp_variables` (`vl-dir`,
  `vl-width`, ...) are allowed; anything else makes the generator refuse the source.
- `/*AUTOINPUT("regexp")*/`, `/*AUTOOUTPUT("regexp")*/` (and `/*AUTOINOUT*/`) in the
  port list: emacs declares, with their widths, the instance's inputs and outputs whose
  connected signal matches the regexp and is not declared elsewhere. Group them by
  function, each group under a comment; the last, unfiltered pair catches what no group
  took and must expand to nothing. Declare no port by hand: a hand-written port next to
  AUTO groups breaks the commas, and the expansion would then not be the interface.
- `/*AUTOWIRE*/` after the port list: emacs declares the instance outputs that are not
  ports (the `unused_*` sinks). `/*AUTO_LISP(setq verilog-auto-output-ignore-regexp
  "^unused_")*/` keeps those sinks from becoming outputs; AUTO_LISP may set only
  verilog-mode's `verilog-auto-<kind>-ignore-regexp` filters.
- `/*AUTOARG*/` is for a non-ANSI header (`module m (/*AUTOARG*/); input ...`); a wrapper
  uses the ANSI form above with AUTOINPUT/AUTOOUTPUT, so it needs no AUTOARG.
- `/*AUTORESET*/` resets the registers of an `always_ff` block; a wrapper has no
  registers of its own unless its spec asks for one (a synchroniser), so it is rarely
  used here.

Mapping the IP's names to the project's:

- Read each IP port in `rtl.wrapper.vendor.ports` and decide its project name by the
  naming rules: the direction prefix or suffix the rules want (`i_`/`o_` or `_i`/`_o`),
  the active-low mark (`rst_ni` becomes, e.g., `i_rst_n`), the project's vocabulary
  (`irq` -> `int` when the rules say so). Prefer one regexp rule per naming pattern of
  the IP (all `*_i`, all `*_o`), with single-port rules before it for the exceptions.
- Every IP input is connected: to a wrapper port, or tied off (`1'b0`, `'0`, a constant
  the spec states). Never leave an input open (`()`).
- Every IP output is used: a wrapper port, or an `unused_<what>` sink (lint accepts a
  signal whose name contains `unused`). Do not leave an output open (`()`): lint warns.
- Set every parameter the spec or the plan fixes in the instance's `#( ... )`; the
  generator substitutes the values into the widths it declares.

Never:

- edit, copy or re-declare the IP's files, or write a module the IP already has: the
  `vendor_guard` check fails on any change to them;
- write the expanded port list, the `// Beginning of automatic ...` blocks or the
  `.port (signal), // Templated` lines yourself: emacs writes them, and `wrap_check`
  rejects a source whose expansion does not match its own;
- put logic in the wrapper, except tie-offs and the width or polarity adaption the spec
  states (an inverted reset, a zero-extended bus), each named by the naming rules;
- write a `Local Variables:` block, `AUTOINSERTLISP` or other Lisp: the generator
  ignores file-local variables and refuses Lisp beyond the allowed list.

If the IP's ports cannot be mapped from the context (a port whose purpose is unclear,
an interface the project names that the IP cannot provide), stop and report
`needs_human` with the question.
