---
id: review/diff
description: How to review a block's diff against its requirements, interface and Design Model.
roles: [critic]
version: 0.1.0
---
Review the diff in `review.diff` against `review.spec` (the block's requirements, its
interface and its registers) and `review.model`. Read a changed file in full when the
hunk alone does not show enough.

Look for, in this order:

- **spec/RTL mismatch**: behaviour a requirement states that the new code does not do,
  or does differently (`spec_mismatch`; `missing_req` when a requirement's behaviour is
  gone or never written).
- **reset**: a register's reset value differs from the spec's `Reset` column (`reset`).
- **width**: a signal, port or field narrower or wider than the spec says, a
  truncating or extending assignment (`width`).
- **counters and compares**: off by one, `<` for `<=`, the wrong step (`counter`).
- **register map**: a register decoded at the wrong offset, a wrong access type, an
  address decode that does not match the memory map (`register_map`).
- **bit order**: fields or bits swapped or reversed against the spec (`bit_order`).
- **latches**: a combinational block that does not assign every output on every path
  (`latch`).
- **CDC**: a signal crossing clock domains without a synchroniser (`cdc`).
- **hard-coded values**: a literal where the spec has a register, parameter or named
  constant (`hard_coded`).
- **naming** against the project's rules, and **test gaps**: a changed requirement with
  no test (`naming`, `test_gap`). Anything else: `other`.

Rules:

- Every comment is on a `file:line` inside the diff, and its `evidence` quotes what the
  claim rests on: the spec line (`doc/...md:68 While CTRL.EN = 1, ...`) or the code line.
  No evidence, no comment.
- Name the requirement in `req_id` when there is one.
- `blocker` or `major`: the design is wrong against the spec. `minor`: a real but small
  problem. `nit`: optional. Do not comment on formatting, whitespace or comments that a
  formatter or lint covers, and do not invent requirements the spec does not state.
- When the diff is correct, say so: no comments, `reviewed` lists every file of the diff,
  verdict `approve`.
