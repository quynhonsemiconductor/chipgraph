---
id: org/naming
description: How to name modules, ports, parameters and signals from the project's naming rules in the context.
roles: [rtl-author]
version: 0.1.0
---
Names come from the project's naming rules, never from memory or another project's
habits. The rules are in your context as text: `rtl.naming.text` (from the file
`rtl.naming.rules`).

How to read them:

- "Allowed names, per kind": one line per kind of name you declare: `module`, `port`,
  `parameter`, `localparam`, `enum_value`, `instance`, `signal`, `memory`, `genvar`.
  The whole name must match the pattern of its kind. A port is checked as a port, a
  signal as a signal: the check classifies each declaration by its syntax.
- "Lexical rules": apply to every name you declare (case, active-low marks, indices).
- "Vocabulary": a word on the left must not appear in a name; use the word on the right.

Then:

- The module name and the port names are fixed by `rtl.module` and `rtl.interface`; they
  already follow the rules. Use them exactly.
- Name every other signal, parameter and instance by its kind's pattern. When the style
  exemplars use a suffix the rules allow (`_q`, `_d`, `_r`, `_next`), use the exemplars'.
- When the rules and the exemplars disagree, the rules win: the `naming` check rejects a
  name, the `style` check only warns unless the profile enforces that key.
- When `rtl.naming` is empty, the project has no naming rules: follow the exemplars.
- Never use the exemption comment the rules name.
