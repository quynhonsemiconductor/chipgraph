"""Security review of M2-12: the Lisp scanner treats the agent's source as hostile.

Findings 1-7 and the second-round template finding, scanner only: no emacs needed, so CI
always covers them. The same inputs run through real emacs in
`test_wrapper_lisp_emacs.py`.
"""

from __future__ import annotations

import pytest

from chipgraph.packs.digital_rtl.wrapper import lisp

SHELL = '(shell-command "touch /tmp/x")'
GOOD = '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/'


def _refused(text: str) -> list[str]:
    return [p.message for p in lisp.scan(text) if not p.note]


def _source(extra: str) -> str:
    from wrapper_helpers import source_text

    return source_text() + "\n" + extra + "\n"


# --- finding 1: AUTO_LISP anywhere, in any case ---------------------------------------------

FINDING_1 = [
    f"// AUTO_LISP{SHELL}",
    f"/*auto_lisp{SHELL}*/",
    f"/*Auto_Lisp{SHELL}*/",
    f"/* see AUTO_LISP{SHELL} */",
    '/*autoinsertlisp(shell-command "touch /tmp/x")*/',
    '/*AutoInsertLast(shell-command "touch /tmp/x")*/',
    '// AUTO_LISP(write-region "pwned" nil "/tmp/canary")',
    f'initial $display("AUTO_LISP{SHELL}");',
    f"/*AUTO_LISP {SHELL}*/",
    f"/* AUTO_LISP{SHELL}*/",
    f"//AUTO_LISP\n{SHELL}",
    # lookalikes that fold to the ASCII keyword: long s, Kelvin sign, dotless i, fullwidth
    f"/*AUTO_LI\u017fP{SHELL}*/",
    f"// auto_l\u0131sp{SHELL}",
    f"/*\uff21UTO_LISP{SHELL}*/",
    f"/*autoin\u017fertlisp{SHELL}*/",
    "/*AUTOINSERTLAST*/",
]


@pytest.mark.parametrize("hostile", FINDING_1)
def test_every_spelling_and_place_of_auto_lisp_is_refused(hostile: str) -> None:
    assert _refused(_source(hostile)), hostile


def test_a_good_auto_lisp_with_a_hostile_one_in_its_string_is_refused() -> None:
    text = f'/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "AUTO_LISP{SHELL}")*/'
    assert _refused(text)


def test_the_exact_form_is_still_accepted_and_a_mention_without_paren_is_not_lisp() -> None:
    assert lisp.scan(_source(GOOD)) == []
    assert lisp.scan("// the AUTO_LISP settings below exclude w_* signals\n") == []


def test_the_skeleton_folds_lookalikes_and_keeps_the_length() -> None:
    text = "AUTO_LI\u017fP \u212a \u0131 \uff21 \u00e9 \u4e2d"
    folded = lisp.skeleton(text)
    assert len(folded) == len(text)
    assert folded.startswith("auto_lisp k i a e")
    assert folded[-1] == "\x7f"


# --- finding 2: the region is exactly one form in a restricted syntax -------------------------

FINDING_2 = [
    # a second form after the first
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x") (shell-command "id")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x")(delete-file "a")*/',
    # `;` is a Lisp comment but Verilog punctuation: the two see different forms
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x" ;)\n (shell-command "id"))*/',
    # `?` char literals hide a quote or a paren from one of the two readers
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ?\\")(shell-command "id")")*/',
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ?\\C-x)*/",
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ?\\N{LATIN SMALL LETTER A})*/",
    # backslashes, brackets, comment marks, dots
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp \\(shell-command))*/",
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ["a" (shell-command "id")])*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp {"a"})*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x" // ) (shell-command "id")\n)*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp ("a" . "b"))*/',
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp 1.5)*/",
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp |a b|)*/",
    # numbers are strict integers
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp 1e999)*/",
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp 1_0)*/",
    # strings: only plain characters and `\\`
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "\\N{COMMERCIAL AT}")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "\\^@")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "a\\"b")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "a\nb")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "a\u00e9")*/',
    # the comment closes inside a string or never
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "*/")*/',
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x")',
    # quoted data can build any text
    "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp '(64 34))*/",
]


@pytest.mark.parametrize("hostile", FINDING_2)
def test_a_region_that_is_not_one_plain_form_is_refused(hostile: str) -> None:
    assert _refused(hostile), hostile


def test_a_regexp_with_its_escaped_alternation_is_still_accepted() -> None:
    text = '/*AUTO_LISP(setq verilog-auto-input-ignore-regexp "^w_\\\\|^t_")*/'
    assert lisp.scan(text) == []
    spaced = '/*AUTO_LISP(setq verilog-auto-input-ignore-regexp  "^w_" )*/'
    assert lisp.scan(spaced) == []


# --- findings 3, 4 and the second round of template functions ---------------------------------

QSOC_TEMPLATE = (
    "/* apb_slave AUTO_TEMPLATE (\n"
    "    .P\\(ADDR\\|WDATA\\|WRITE\\|SEL\\|ENABLE\\) "
    '(i_bus_apb_p@"(downcase (symbol-name \'\\1))"[]),\n'
    ") */\n"
)


def _template(lisp_text: str) -> str:
    return f'/* m AUTO_TEMPLATE (\n .a (x_@"{lisp_text}"),\n) */\n'


def test_the_qsoc_style_template_function_is_accepted() -> None:
    assert lisp.scan(QSOC_TEMPLATE) == []
    assert lisp.scan(_template("(downcase vl-name)")) == []
    assert lisp.scan(_template('(concat (substring vl-name 0 1) \\"x\\")')) == []
    assert lisp.scan(_template("(number-to-string (+ 1 (length vl-name)))")) == []


TEMPLATE_HOSTILE = [
    # format builds `@`, `"` and `\` from numbers: a second-round @"..." would run
    '(format \\"%c%c(shell-command %c%cx%c%c)%c\\" 64 34 92 34 92 34 34)',
    '(format \\"%s\\" vl-name)',
    # concat of a quoted list or vector of character codes
    "(concat '(64 34 40 115 104))",
    "(concat (quote (64 34)))",
    "(concat [64 34])",
    # `@` and backslashes inside the Lisp
    "(+ @ 1)",
    '(concat \\"@\\" vl-name)',
    '(concat \\\\\\"a)',
    "(downcase \\1)",
    "(symbol-name '\\1x)",
    # trailing forms: emacs evaluates the first only, so the scanner refuses the rest
    '(downcase vl-name) (shell-command \\"id\\")',
    '(downcase vl-name))(shell-command \\"id\\"',
    # `?` char literals of every length
    "(concat ?\\C-x)",
    "(concat ?\\M-\\C-a)",
    "(concat ?\\^?)",
    "(concat ?\\N{U+40})",
    "(concat ?\\u00e9)",
    "(concat ?a)",
    # catastrophic regexps, computed regexps
    '(string-match \\"\\\\(a+\\\\)+$\\" vl-name)',
    "(string-match vl-name vl-name)",
]


@pytest.mark.parametrize("lisp_text", TEMPLATE_HOSTILE)
def test_a_template_function_that_could_run_more_lisp_is_refused(lisp_text: str) -> None:
    assert _refused(_template(lisp_text)), lisp_text


def test_an_at_sign_before_a_template_function_is_refused() -> None:
    assert _refused('/* m AUTO_TEMPLATE (\n .a (x_@@"(downcase vl-name)"),\n) */\n')


def test_an_unclosed_template_function_is_refused() -> None:
    assert _refused('/* m AUTO_TEMPLATE (\n .a (x_@"(downcase vl-name)\n) */\n')


def test_format_is_not_allowed() -> None:
    assert "format" not in lisp.FUNCTIONS


def test_a_regexp_with_nested_repetition_is_refused_and_a_plain_one_accepted() -> None:
    assert lisp.scan(_template('(if (string-match \\"^a_.*$\\" vl-name) 1 0)')) == []
    nested = (
        "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "
        '(if (string-match "\\\\(a+\\\\)+$" "aaaa") "x" "y"))*/'
    )
    assert any("repeats a repeated group" in m for m in _refused(nested))
    plain = nested.replace("+\\\\)+", "\\\\)+")
    assert lisp.scan(plain) == []


# --- finding 7: deep nesting is a refusal, not a crash ----------------------------------------


@pytest.mark.parametrize("depth", [lisp.MAX_DEPTH + 1, 1000, 3000, 20000])
def test_deep_nesting_is_refused_without_a_crash(depth: int) -> None:
    text = "/*AUTO_LISP" + "(" * depth + ")" * depth + "*/"
    assert any("deeper" in m for m in _refused(text))
    template = _template("(downcase " * depth + "vl-name" + ")" * depth)
    assert _refused(template)


def test_quotes_and_lists_at_the_limit_still_read() -> None:
    inner = "(concat " * (lisp.MAX_DEPTH - 1) + "vl-name" + ")" * (lisp.MAX_DEPTH - 1)
    assert lisp.scan(_template(inner)) == []


def test_a_scanner_failure_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str) -> list[lisp.LispProblem]:
        raise RecursionError("boom")

    monkeypatch.setattr(lisp, "_scan", boom)
    [problem] = lisp.scan(GOOD)
    assert not problem.note and "could not read" in problem.message


# --- finding 8 (scanner side): lookalikes of `Local Variables` are noted ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "// Local Variables:\n",
        "// LOCAL VARIABLES:\n",
        "// Local Variable\u017f:\n",
        "// local\tvariables :\n",
        "// -*- mode: verilog -*-\n",
    ],
)
def test_file_local_variables_in_any_spelling_are_noted(text: str) -> None:
    problems = lisp.scan(text)
    assert problems and all(p.note for p in problems)
