"""Security review of M2-12, finding 1 and its neighbours, against a real emacs.

The wrapper author is an AI: its `.src.sv` is hostile input. Each test writes a canary path
**outside** the scratch directory into a payload and asserts the canary is never created, by
the scanner (nothing reaches emacs), by `neutralize` (the token is broken), or by the guard
(emacs refuses to evaluate anything outside the allow-list), each tested with the layers above
it bypassed. A control shows that the payload really does write the canary where nothing guards.

Everything that needs emacs skips cleanly without it; the scanner-only part always runs.
"""

from __future__ import annotations

import asyncio
import random
import subprocess
import unicodedata
from pathlib import Path

import pytest
from wrapper_helpers import (
    IP_FILE,
    SRC,
    WRAPPER,
    make_project,
    model_style_source,
    needs_emacs,
)

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.packs.digital_rtl.wrapper import emacs as gen
from chipgraph.packs.digital_rtl.wrapper import lisp


def _canary(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    return outside / "canary.txt"


def _write_form(canary: Path) -> str:
    return f'(write-region "pwned" nil "{canary}")'


def _with_line(line: str) -> str:
    """The model-style source with `line` inserted right after the module header."""
    source = model_style_source()
    marker = "  /*AUTOWIRE*/\n"
    assert marker in source
    return source.replace(marker, f"{marker}  {line}\n", 1)


def _expand(root: Path, text: str, *, vendor: list[str] | None = None) -> gen.Expansion:
    return asyncio.run(
        gen.expand(LocalRunner(), root, SRC, vendor or [IP_FILE], name=WRAPPER, text=text)
    )


def _variants(canary: Path) -> dict[str, str]:
    form = _write_form(canary)
    return {
        "line-comment": f"// AUTO_LISP{form}",
        "lowercase": f"/*auto_lisp{form}*/",
        "mixedcase": f"/*Auto_Lisp{form}*/",
        "inside-comment": f"/* see AUTO_LISP{form} */",
        "in-string": f'localparam string K = "AUTO_LISP{form.replace(chr(34), chr(39))}";',
        "insertlisp": f"/*autoinsertlisp{form}*/",
        "insertlast": f"/*AUTOINSERTLAST{form}*/",
        "insertlast-lower": f"/*autoinsertlast{form}*/",
        "long-s": f"/*AUTO_LI\u017fP{form}*/".replace("\u017f", "\u017f"),
        "kelvin": f"/*\u212aelvin AUTO_LISP{form}*/",
        "dotless-i": f"/*AUTO_L\u0131SP{form}*/",
        "fullwidth": f"/*\uff21\uff35\uff34\uff2f_LISP{form}*/",
        "space-before-paren": f"/*AUTO_LISP {form}*/",
        "plain-exact": f"/*AUTO_LISP{form}*/",
    }


@needs_emacs
@pytest.mark.parametrize(
    "name",
    [
        "line-comment",
        "lowercase",
        "mixedcase",
        "inside-comment",
        "in-string",
        "insertlisp",
        "insertlast",
        "insertlast-lower",
        "long-s",
        "kelvin",
        "dotless-i",
        "fullwidth",
        "space-before-paren",
        "plain-exact",
    ],
)
def test_no_variant_of_auto_lisp_reaches_the_canary(tmp_path: Path, name: str) -> None:
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    result = _expand(root, _with_line(_variants(canary)[name]))
    assert not canary.exists(), f"{name}: the payload ran ({result.issues})"


@needs_emacs
def test_the_control_payload_really_writes_the_canary_where_nothing_guards(
    tmp_path: Path,
) -> None:
    """The same text, raw to emacs with no scanner, neutralize or guard: the harness notices."""
    canary = _canary(tmp_path)
    for index, line in enumerate(
        (f"// AUTO_LISP{_write_form(canary)}", f"/* see AUTO_LISP{_write_form(canary)} */")
    ):
        canary.unlink(missing_ok=True)
        scratch = tmp_path / f"raw{index}"
        scratch.mkdir()
        target = scratch / "w.sv"
        target.write_text(_with_line(line))
        subprocess.run(
            [
                "emacs",
                "-Q",
                "--batch",
                "--eval",
                gen.settings([]),
                str(target),
                "-f",
                "verilog-batch-auto",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert canary.exists(), line


@needs_emacs
def test_a_vendor_file_with_auto_lisp_cannot_run_it(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    ip = root / IP_FILE
    original = ip.read_text(encoding="utf-8")
    for line in (
        f"// AUTO_LISP{_write_form(canary)}",
        f"/*auto_lisp{_write_form(canary)}*/",
        f"/*AUTOINSERTLISP{_write_form(canary)}*/",
    ):
        ip.write_text(f"{line}\n{original}", encoding="utf-8")
        _expand(root, model_style_source())
        assert not canary.exists(), line


@needs_emacs
@pytest.mark.parametrize(
    "block",
    [
        '// Local Variables:\n// eval: (write-region "pwned" nil "{c}")\n// End:\n',
        '/* -*- eval: (write-region "pwned" nil "{c}") -*- */\n',
        '// -*- mode: verilog; eval: (write-region "pwned" nil "{c}") -*-\n',
        "// -f {c}.f\n// -y {c}.dir\n",
        '// Local Variables:\n// verilog-library-flags: ("-f {c}.f")\n// End:\n',
    ],
)
def test_local_variables_and_flags_lines_are_inert(tmp_path: Path, block: str) -> None:
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    text = block.replace("{c}", str(canary))
    _expand(root, model_style_source() + text)
    assert not canary.exists()
    ip = root / IP_FILE
    ip.write_text(ip.read_text(encoding="utf-8") + text, encoding="utf-8")
    _expand(root, model_style_source())
    assert not canary.exists()


# --- template functions: the second-round vectors --------------------------------------------

_TEMPLATE_PAYLOADS = {
    # no quotes at all, so emacs gets past `replace-match`: the path is a symbol name
    "direct": "(write-region (symbol-name 'pwned) nil (symbol-name '{c}))",
    # a result that contains a new @"..." (verilog-mode restarts from the start of the string)
    "concat-codes": "(concat '(64 34 40 119 114 105 116 101))",
    "number-result": "(+ 1 (write-region (symbol-name 'x) nil (symbol-name '{c})))",
}


def _template(form: str) -> str:
    return (
        "  /* vendor_fifo AUTO_TEMPLATE (\n"
        '      .rst_ni (i_rst_n@"' + form + '"),\n'
        "      .\\(.*\\)_i (i_\\1[]),\n"
        "      .\\(.*\\)_o (o_\\1[]),\n"
        "  ); */\n"
    )


def _template_source(form: str) -> str:
    source = model_style_source()
    start = source.index("  /* vendor_fifo AUTO_TEMPLATE")
    end = source.index("); */\n", start) + len("); */\n")
    return source[:start] + _template(form) + source[end:]


@needs_emacs
@pytest.mark.parametrize("vector", sorted(_TEMPLATE_PAYLOADS))
def test_template_vectors_layer_by_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str
) -> None:
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    text = _template_source(_TEMPLATE_PAYLOADS[vector].replace("{c}", str(canary)))

    # layer 1: the scanner refuses, emacs is never started for the expansion
    assert [p for p in lisp.scan(text) if not p.note], "the scanner accepted a hostile template"
    _expand(root, text)
    assert not canary.exists()

    # layer 2: scanner bypassed -> neutralize does not touch @"..."; the guard must hold
    monkeypatch.setattr(lisp, "scan", lambda _text: [])
    result = _expand(root, text)
    assert not canary.exists(), f"{vector}: the guard let it through ({result.issues})"
    assert not result.ok
    assert any(issue.rule == "wrap.lisp" for issue in result.issues), result.issues


@needs_emacs
def test_without_the_guard_the_direct_template_payload_writes_the_canary(tmp_path: Path) -> None:
    """Control for the guard: the same template, emacs without GUARD, does write it."""
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    text = _template_source(_TEMPLATE_PAYLOADS["direct"].replace("{c}", str(canary)))
    scratch = tmp_path / "rawt"
    scratch.mkdir()
    target = scratch / "w.sv"
    target.write_text(text)
    ip = scratch / "vendor_fifo.sv"
    ip.write_text((root / IP_FILE).read_text())
    subprocess.run(
        [
            "emacs",
            "-Q",
            "--batch",
            "--eval",
            gen.settings([ip]),
            str(target),
            "-f",
            "verilog-batch-auto",
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert canary.exists()


# --- the differential test: scanner vs the guard ------------------------------------------------

_BENIGN = [
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/',
    '/*AUTO_LISP(setq verilog-auto-input-ignore-regexp "^i_clk$")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_\\\\|^dbg_")*/',
    "/* just a comment about wrappers */",
    "// AUTO is a verilog-mode word, but this is only a comment",
    "",
]
_HOSTILE = [
    "// AUTO_LISP{w}",
    "/*auto_lisp{w}*/",
    "/*Auto_Lisp{w}*/",
    "/* x AUTO_LISP{w} */",
    "/*AUTO_LISP{w}*/",
    '/*AUTO_LISP{w} (setq verilog-auto-output-ignore-regexp "^u")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^u");{w}*/',
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ?\\C-a)*/",
    "/*AUTOINSERTLISP{w}*/",
    "/*autoinsertlast{w}*/",
    "/*AUTO_LISP(progn{w})*/",
    '/*AUTO_LISP(setq default-directory "/")*/',
    '/*AUTO_LISP(funcall \'shell-command "id")*/',
    '/*AUTO_LISP(load "/etc/passwd")*/',
    "/*AUTO_LISP((lambda () 1))*/",
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp (concat '(64 34)))*/",
]
_WRAPS = ["{}", "\u2028{}", "{}\t", "  {}  ", "{}\n\n", "\n{}"]


def _corpus(seed: int, count: int, canary: Path) -> list[str]:
    rng = random.Random(seed)
    write = _write_form(canary)
    out: list[str] = []
    for _ in range(count):
        kind = rng.random()
        if kind < 0.35:
            line = rng.choice(_BENIGN)
        else:
            line = rng.choice(_HOSTILE).replace("{w}", write)
            if rng.random() < 0.4:  # mutate case
                line = "".join(c.swapcase() if rng.random() < 0.3 else c for c in line)
            if rng.random() < 0.2:  # a lookalike for one letter
                line = line.replace("S", "\u017f", 1)
        out.append(_with_line(rng.choice(_WRAPS).replace("{}", line)))
    return out


def test_scanner_only_corpus_refuses_what_it_should(tmp_path: Path) -> None:
    """Always runs (no emacs): about 40 hostile inputs, every one refused or neutralised."""
    canary = _canary(tmp_path)
    corpus = _corpus(7, 120, canary)
    refused = accepted = 0
    for text in corpus:
        problems = [p for p in lisp.scan(text) if not p.note]
        if problems:
            refused += 1
            continue
        accepted += 1
        # accepted text must not hold any evaluatable AUTO_LISP form but the exact accepted one
        skeleton = lisp.skeleton(text)
        for index in range(len(skeleton)):
            if skeleton.startswith("auto_lisp", index):
                assert text[max(0, index - 2) : index] == "/*", text
    assert refused > 20 and accepted > 20, (refused, accepted)


@needs_emacs
def test_scanner_and_guard_agree_on_a_generated_corpus(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    canary = _canary(tmp_path)
    corpus = _corpus(11, 150, canary)
    accepted = refused = disagreements = 0
    for text in corpus:
        problems = [p for p in lisp.scan(text) if not p.note]
        result = _expand(root, text)
        if problems:
            refused += 1
            assert not result.ok  # a refused source never expands
            continue
        accepted += 1
        guard_refusals = [i for i in result.issues if gen.GUARD_PREFIX in i.msg]
        if guard_refusals:
            disagreements += 1  # the scanner accepted what the guard refuses
        assert not canary.exists(), text
    assert disagreements == 0, f"{disagreements} scanner/guard disagreements"
    assert accepted > 20 and refused > 20, (accepted, refused)


def test_normalisation_names_what_it_catches() -> None:
    """The lookalikes the corpus uses do fold to ASCII in the skeleton."""
    for ch, ascii_ in (("\u017f", "s"), ("\u212a", "k"), ("\u0131", "i"), ("\uff21", "a")):
        assert unicodedata.normalize("NFKC", ch).lower() == ascii_ or lisp.skeleton(ch) == ascii_
