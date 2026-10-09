# Requirement IDs

How chipgraph finds requirement IDs in a spec, which checks use them, and how to fix or
waive what they report. Background: [DECISIONS D37](DECISIONS.md).

## Why IDs

A requirement ID is the one string that ties a sentence of a spec to the RTL that
implements it and the tests that verify it. `trace` and `/trace` grep for it; a finding,
a review comment or a test name can cite it. The ID must therefore be stable: it does not
change when the list is reordered, when the text is reworded, or when another item is
removed.

## Declared IDs (the default)

The ID is written in the spec and matched by a regex from the profile:

```yaml
spec:
  requirements:
    id_pattern: '{BLOCK}_\d{3}'   # default: 'REQ-[A-Z][A-Z0-9_]*-\d+'
```

Before compiling, `{BLOCK}` is replaced by the block name in upper case and `{block}` by
it in lower case, so one pattern serves every block (`DMA_001` for `dma`, `UART_004` for
`uart`). A block whose spec uses another prefix overrides only the pattern:

```yaml
blocks:
  rom:
    spec:
      requirements: { id_pattern: '(?:ROM|BOOT)_\d{3}' }
```

The model key of a declared requirement is `requirement:<ID>`, with
`attrs.id_source = "declared"`.

### What counts as a declared ID

The MAS extractor (`mas-markdown`) takes the **first token** of:

- a list item, after its number (`1.`, `2)`) is removed;
- a paragraph;
- a heading;
- the first cell of a table row.

Surrounding backticks and `**` are stripped, then the token must match `id_pattern`
**as a whole**. `` `DMA_001` ``, `**DMA_001**` and `DMA_001` all declare `DMA_001`;
`DMA_001:` or `(DMA_001)` do not, and neither does an ID in the middle of a sentence
(that is a *reference*, see below). The text of the requirement is the rest of the item
or paragraph. The same ID declared twice in one file is the ingest error
`mas.req.duplicate`, which cites the first line.

## Inferred requirements (temporary)

For specs that have no IDs yet, `infer: verification` makes each top-level numbered item
of the Verification section a requirement:

```yaml
spec:
  requirements:
    id_pattern: '{BLOCK}_\d{3}'
    infer: verification          # default: off
    infer_heading: Verification  # the section title, without its number
```

An inferred requirement has no ID. Its key is `requirement:<block>.h<8 hex>`, from the
sha256 of the item text with its number removed and whitespace collapsed, so renumbering
or reordering the list does not change it, but rewording the item does. It is recorded
with `attrs.id_source = "inferred"`. A declared ID always wins: an item that starts with
an ID is declared, not inferred. Two items with the same text give one requirement and
the warning `mas.req.duplicate_text`.

This mode is a bridge (D37): once every spec declares its IDs, turn `infer` off.

## Every Verification item carries an ID

Rule: **in a MAS file that declares at least one requirement ID, every top-level numbered
item of its Verification section must carry an ID.**

An item carries an ID when its first token is one (it declares it) or when it names one
anywhere in its text as a whole word (it refers to a requirement declared elsewhere, as
in `` 2. Count load (`REQ-TIM-002`): ... ``). Nested items (sub-steps, indented deeper
than the first numbered item) and bullet items are not top-level items. A file with no
declared ID at all is not checked; with `infer: verification` its items are inferred as
before.

An item without an ID is recorded once, as a requirement with
`attrs.id_source = "missing"` (in both `infer` modes; it is never also inferred), the same
content-hash key as an inferred one, the item's text and line, and
`attrs.suggested_id` when a next ID can be proposed. `spec_schema` reports it:

```text
doc/specs/QNSC_DMA_MAS.md:377  [requirement.missing_id] Verification item has no ID; the other items of this file have one. Suggested next ID: DMA_014
```

Fix it by giving the item the suggested ID (or another unused one) as its first token,
then re-run `chipgraph ingest`.

### How the next ID is suggested

1. Take the IDs declared in this file and split each into `<prefix><number>` (the number
   is the trailing digits). IDs with no trailing digits are ignored.
2. Choose the prefix family with the most IDs; on a tie, the family with the higher
   number. (`SCRC_RST_001`, `SCRC_RST_002`, `SCRC_CLK_009` gives `SCRC_RST_`.)
3. Propose `max + 1`, zero-padded to the family's widest number (`DMA_007` gives
   `DMA_008`, `REQ-DMA-0041` gives `REQ-DMA-0042`). Gaps are never filled: a gap is a
   retired number.
4. Several items missing their ID in one file get `max + 1`, `max + 2`, ... in line
   order, so the suggestions never collide with each other.
5. A suggestion that does not match the block's `id_pattern` as a whole (`DMA_1000`
   against `{BLOCK}_\d{3}`) is dropped; the error is still reported, without a hint.

The suggestion only sees the current file: a number retired from the top of the list,
or used in another file of the same block, can be proposed again. Check before you take
it.

## The checks

| Rule | Check | Severity | Reports |
|---|---|---|---|
| `requirement.missing_id` | `spec_schema` | error | A Verification item with no ID, in a file that declares IDs (above). |
| `requirement.empty_text` | `spec_schema` | error | A requirement with no text (an ID and nothing after it). |
| `mas.req.duplicate` | ingest | error | An ID declared twice in one file; cites the first line. |
| `mas.req.duplicate_text` | ingest | warning | Two items with the same text; the second gives no requirement. |
| `req.no_test` | `trace` | error | A declared requirement whose ID no test file names. |
| `test.unknown_req` | `trace` | error | A test names a string that matches `id_pattern` for some block but is no requirement in the model: a stale, mistyped or unknown reference. |
| `req.inferred_untraceable` | `trace` | info | Per block, how many requirements have no declared ID (inferred, and missing an ID) and so cannot be traced. One line per block, never a failure. |

`spec_schema` and `trace` respect `chipgraph check --block <ip>`: only that block's (and
its instances') requirements are considered. `test.unknown_req` still uses every block's
pattern, so a test naming another block's stale ID is caught.

`test.unknown_req` is the "unknown reference" rule; there is no separate
`requirement.unknown_ref`.

## How tests cite an ID

`trace` reads the files matched by its `tests` globs and searches each declared ID as a
whole word: the characters around it must not be a letter, a digit, `_` or `-`. Any
mention counts: a comment, a docstring, a test name, a string.

```yaml
adapters:
  trace:
    use: trace
    tests: ["dv/**/*.py", "dv/**/*.sv"]
    id_pattern: '{BLOCK}_\d{3}'   # for test.unknown_req; default 'REQ-[A-Z][A-Z0-9_]*-\d+'
```

```python
def test_two_jobs_complete_in_order():
    """Verifies DMA_004."""
```

`trace` takes `id_pattern` from its own adapter options, not from
`spec.requirements`: set it to the same pattern, or `test.unknown_req` looks only for
strings of the default `REQ-` form. `severity: warning` on the adapter lowers `trace`'s
file-level errors to warnings while a project has no tests yet.

## Waiving a finding

Every issue of a check becomes a finding (`chipgraph findings`). Waive one with a reason:

```bash
chipgraph check --only spec_schema
chipgraph findings
chipgraph waive F-1a2b3c4d --reason "ID agreed with the block owner in review #12" --by <name>
```

A waiver is bound to the content hash of the file the finding points at. Editing the
file (adding the ID, for example) ends the waiver, and a finding whose message or line
changes is a new finding. A finding with no file (the `req.inferred_untraceable` summary)
is waived with `--bind PATH` naming the files whose change should end the waiver. To
switch a check off for a path for good, use a `paths:` rule with a `reason`.

## Appendix: proposed text for the QSoC MAS template

For `doc/specs/QNSC_TEMPLATE_MAS.md` in `quynhonsemiconductor/vlsi_deep_training`
(read at commit `418b21a`). It matches what DMA, I2C, ROM, SYSCSR, UART, WDT and
BOOT_SPEC already do (`` 1. `DMA_001` Reset values ... ``).

How to apply: replace the comment under `# 12. Verification` with (a), and the comment
under `# 6. Register map` as in (b). Both stay HTML comments, like the rest of the
template's guidance. Before pasting (b), confirm the read and write behaviour stated for
RO, WO and RSVD against the team's bus rules. Two follow-ups outside the template:

- SCRC numbers its items `SCRC_<AREA>_NNN` (`SCRC_CLK_001`, `SCRC_RST_001`), which
  `{BLOCK}_\d{3}` does not match, so chipgraph infers its 21 items today. Either rule (a)
  allows an optional area and the profile uses `{BLOCK}_(?:[A-Z]+_)?\d{3}`, or SCRC
  moves to `SCRC_NNN` once, before tests cite its IDs.
- TIMER, PWM, RAM, SYSDBG and Interrupt_Map have no IDs yet. Once they do, drop `infer`
  from the profile (D37).

### (a) Requirement IDs, section 12

```markdown
# 12. Verification

<!-- A numbered list of checks, one per claim above. If a claim cannot be turned
     into a check, it does not belong in this document. Include at least one
     check that fails if the design drifts from its own description -- for a
     combinational block, that no always_ff appears.

     Every item starts with its requirement ID, in backticks, as its first word:

         1. `<BLOCK>_001` Reset values and access types match section 6.
         2. `<BLOCK>_002` ...

     The ID is <BLOCK>_NNN: the name this file is named after, in capitals
     (QNSC_DMA_MAS.md gives DMA_), an underscore and exactly three digits.
     IDs belong to this file. They are never reused and never renumbered:
     DV tests and reviews cite them, and a reused or renumbered ID silently
     moves a test onto a different claim.
       - A new item takes the next number after the highest one this file has
         ever used, wherever it goes in the list.
       - A removed item takes its number with it. The number stays retired:
         say so in the revision history ("DMA_011 removed"), and never give it
         to another item.
       - The list number (1., 2., ...) may change; the ID may not.
     A sub-step of an item is indented under it and needs no ID of its own.
     chipgraph reports an item without an ID as requirement.missing_id and
     suggests the next free number. -->
```

### (b) Register access types, section 6

Before:

```markdown
<!-- Offset, field, bits, access type, reset value, description. The access type
     is mandatory and must be one of RW, RO, WO, W1C, RSVD: DV generates the
     reset-value and access-type tests from this column.
     If the block has no registers, say that and say where configuration lives
     instead. -->
```

After:

```diff
 <!-- Offset, field, bits, access type, reset value, description. The access type
-     is mandatory and must be one of RW, RO, WO, W1C, RSVD: DV generates the
-     reset-value and access-type tests from this column.
+     is mandatory and must be one of the types below: DV generates the
+     reset-value and access-type tests from this column.
+
+     | Access | Read           | Write                                     |
+     |--------|----------------|-------------------------------------------|
+     | RW     | current value  | sets the value                            |
+     | RO     | current value  | no effect                                 |
+     | WO     | 0              | sets the value                            |
+     | W1C    | current value  | 1 clears the bit, 0 leaves it             |
+     | RW1C   | current value  | 1 clears the bit, 0 leaves it             |
+     | RW0C   | current value  | 0 clears the bit, 1 leaves it             |
+     | RSVD   | 0              | no effect                                 |
+
+     RW1C and RW0C are for registers of vendored IP that already use these
+     names (OpenTitan, as in WDT); an in-house register uses W1C, not RW1C.
+     Any other type is an error in chipgraph unless the profile lists it in
+     spec.register_access.
      If the block has no registers, say that and say where configuration lives
      instead. -->
```

On the chipgraph side nothing changes: the QSoC profile already sets
`spec.register_access: [RW, RO, WO, W1C, RSVD, RW0C, RW1C]`; the built-in default stays
`RW, RO, WO, W1C, RSVD`.
