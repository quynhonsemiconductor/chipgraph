---
id: plan/modules
description: How to split a block into modules and write its plan file.
roles: [planner]
version: 0.1.0
---
Write the block's plan to `outputs[0]` as YAML that follows `plan.schema` in your
context. A person reads it (`chipgraph plan show`) and approves it before any module
is built, so it must be complete and honest.

1. **Interface first (F1).** Copy ports from `plan.interface.ports` exactly: name,
   direction, width. Never invent a port. The `top` module carries the block's
   interface. A helper port between two modules of the block is `internal: true`, and
   is never on `top`.
2. **Requirements.** Assign every requirement in `plan.requirements` to the module that
   implements it, by its `id` (the REQ id; for an inferred requirement, its key such as
   `timer.h1a2b3c4d`). One you cannot place goes in `unassigned_reqs` with a reason. Do
   not invent requirements.
3. **Write sets (F2).** Each module lists every file its nodes write, and no two modules
   write the same file. Every path matches one of `plan.layout`. Each module's writes
   include the files in `plan.module_outputs` (fill in `{block}` and `{module}`). Never
   write a file in `plan.other_rules_write`. A file in `plan.existing_files` is written
   only on purpose: say `replaces: true` on that write.
4. **Dependencies.** `depends_on` names modules of this plan that must be built first
   (a module needs the interfaces of the modules it instantiates). No cycles.
5. **Size.** Stay inside `plan.limits`: at most `max_modules` modules, a dependency
   chain of at most `max_depth` modules, and module `budget.tries` adding up to at most
   `max_total_tries`. Prefer few modules with clear boundaries over many small ones.
6. **Names.** Module names follow `plan.naming` when it is set.
7. **Say what you assumed (F4).** List every assumption in `assumptions`, and anything
   a person must decide in `open_questions` (the approver must read them).

Example:

```yaml
schema_version: 1
block: timer
top: timer_top
modules:
  - name: timer_top
    summary: Register interface and the irq output; instantiates the counter.
    reqs: [REQ-TIM-004]
    interface:
      ports:
        - { name: clk, direction: input, width: 1 }
    depends_on: [timer_counter]
    writes:
      - { path: rtl/timer_top.sv }
    budget: { tries: 3, tier: medium }
  - name: timer_counter
    summary: The 32-bit counter and its compare.
    reqs: [REQ-TIM-001]
    interface:
      ports:
        - { name: clk, direction: input, width: 1 }
        - { name: count, direction: output, width: 32, internal: true }
    writes:
      - { path: rtl/timer_counter.sv }
assumptions: [The counter wraps at 2^32.]
open_questions: []
unassigned_reqs:
  - { req: REQ-TIM-005, reason: The read mux belongs to the bus wrapper, not planned here. }
```
