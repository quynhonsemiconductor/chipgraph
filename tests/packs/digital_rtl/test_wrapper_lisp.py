"""M2-12: the Lisp a wrapper source may carry (`packs/digital_rtl/wrapper/lisp.py`).

The source is written by an agent without a shell, so emacs must not become one: only
side-effect-free string and arithmetic forms, and `setq` of verilog-mode's name filters.
No emacs needed.
"""

from __future__ import annotations

import pytest

from chipgraph.packs.digital_rtl.wrapper import lisp
from chipgraph.packs.digital_rtl.wrapper.emacs import PINNED


def _refused(text: str) -> list[str]:
    return [p.message for p in lisp.scan(text) if not p.note]


def test_the_sample_source_and_a_template_regexp_need_no_lisp() -> None:
    from wrapper_helpers import model_style_source, source_text

    assert lisp.scan(source_text()) == []
    assert lisp.scan(model_style_source()) == []


def test_auto_lisp_may_set_verilog_modes_name_filters() -> None:
    text = '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/\n'
    assert lisp.scan(text) == []
    both = '/*AUTO_LISP(setq verilog-auto-input-ignore-regexp "^t_" verilog-typedef-regexp "_t$")*/'
    assert lisp.scan(both) == []


def test_a_template_function_may_use_string_forms_and_template_variables() -> None:
    text = (
        "/* m AUTO_TEMPLATE (\n"
        '  .\\(.*\\)_o (o_@"(downcase vl-name)"[]),\n'
        '  .\\(.*\\)_i (i_@"(concat (substring vl-name 0 1) \\"x\\")"),\n'
        ") */\n"
    )
    assert lisp.scan(text) == []


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('/*AUTO_LISP(shell-command "id")*/', "shell-command"),
        ('/*AUTO_LISP(setq default-directory "/")*/', "default-directory"),
        (
            '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp (shell-command-to-string "id"))*/',
            "shell-command-to-string",
        ),
        ('/*AUTO_LISP(load "/tmp/x.el")*/', "load"),
        ('/*AUTO_LISP(eval (read "(+ 1 2)"))*/', "eval"),
        ('/*AUTOINSERTLISP(insert "x")*/', "AUTOINSERTLISP"),
        ("/*AUTOINSERTLAST(x)*/", "AUTOINSERTLAST"),
        ('/* m AUTO_TEMPLATE (\n .a (@"(getenv \\"HOME\\")")\n) */', "getenv"),
        ('/* m AUTO_TEMPLATE (\n .a (@"(setq vl-name 1)")\n) */', "setq"),
        ('/* m AUTO_TEMPLATE (\n .a (@"(funcall (quote delete-file) x)")\n) */', "funcall"),
        ('/*AUTO_LISP(setq verilog-auto-output-ignore-regexp `(,(getenv "X")))*/', "reader syntax"),
        ("/*AUTO_LISP(setq verilog-auto-output-ignore-regexp #'car)*/", "reader syntax"),
        (
            "/*AUTO_LISP(setq verilog-auto-output-ignore-regexp user-login-name)*/",
            "user-login-name",
        ),
        ('/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "x"*/', "cannot be read"),
    ],
)
def test_a_form_beyond_the_allowed_list_is_refused(text: str, needle: str) -> None:
    refused = _refused(text)
    assert refused, text
    assert any(needle in message for message in refused), (needle, refused)


def test_a_refusal_points_at_its_line() -> None:
    text = 'module m;\n\n/*AUTO_LISP(shell-command "id")*/\nendmodule\n'
    [problem] = lisp.scan(text)
    assert problem.line == 3 and not problem.note


def test_a_local_variables_block_is_a_note_not_a_refusal() -> None:
    text = 'module m; endmodule\n// Local Variables:\n// eval: (shell-command "id")\n// End:\n'
    [note] = lisp.scan(text)
    assert note.note and note.line == 2 and "ignored" in note.message


def test_no_allowed_function_has_a_side_effect() -> None:
    effects = {
        "setq",
        "load",
        "eval",
        "funcall",
        "apply",
        "shell-command",
        "insert",
        "write-region",
    }
    assert not effects & lisp.FUNCTIONS
    assert lisp.SETQ_VARIABLE.match("verilog-auto-output-ignore-regexp")
    assert not lisp.SETQ_VARIABLE.match("default-directory")


def test_every_pinned_variable_is_a_verilog_mode_variable() -> None:
    names = [name for name, _ in PINNED]
    assert len(names) == len(set(names))
    assert all(name.startswith("verilog-") for name in names)
