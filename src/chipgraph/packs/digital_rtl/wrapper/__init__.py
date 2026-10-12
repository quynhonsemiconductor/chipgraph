"""Wrappers around third-party (vendor) IP, in the emacs verilog-mode AUTO style (M2-12).

A wrapper instantiates an IP module it never edits and maps the IP's ports to the
project's names. The RTL Author writes only the wrapper's *source*,
`<wrapper>.src.sv`: its port groups (`/*AUTOINPUT("regexp")*/`, ...), an `AUTO_TEMPLATE`
that maps the IP's ports (regexps, tie-offs, `unused_*` sinks), the instance with its
parameters and `/*AUTOINST*/`, and nothing expanded by hand. A generator runs emacs on
it and writes the expanded `<wrapper>.sv`; a check regenerates and compares.

- `common`: default paths, the profile's `blocks.<b>.wrappers`, path templates;
- `lisp`: the Emacs Lisp a source may carry (`AUTO_LISP`, `@"(...)"` in a template),
  allowed only from a small list of side-effect-free forms: emacs runs it;
- `emacs`: the gen tool `emacs_auto` (rule `digital-rtl/wrapper_gen`): `emacs -Q --batch`
  on a scratch copy, the verilog-mode variables pinned, through the `Runner`;
- `expansion`: the expanded instance read back (its connections, `// Templated` or not)
  and checked against the IP's port declaration (pyslang, header only);
- `resolver`: the `foreach` selectors `wrappers` and `wrappers.vendor`;
- `context`: the `rtl.wrapper` section of the author's context.

The rules are `rules/wrapper.yml` (agent, `rtl-author`) and `rules/wrapper_gen.yml`
(gen); the skill is `skills/wrapper-auto.md` (`rtl/wrapper-auto`); the check is
`chipgraph.checks.wrap_check`, and `chipgraph.checks.vendor_guard` keeps the IP files
as recorded.
"""
