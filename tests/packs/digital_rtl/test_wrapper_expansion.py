"""M2-12: the expanded wrapper read back and checked against the IP's port declaration.

Canned expansions, no emacs: the committed sample wrapper's text, and edits of it that
break one thing each. The declaration is the IP module's header, read with pyslang.
"""

from __future__ import annotations

from wrapper_helpers import EXAMPLE, IP_FILE, WRAPPER, generated_text

from chipgraph.core.contracts import Issue
from chipgraph.packs.digital_rtl.wrapper import emacs as gen
from chipgraph.packs.digital_rtl.wrapper.expansion import instances, mask, module_name, validate
from chipgraph.packs.dv.declaration import ModuleDeclaration, extract_declaration

EXPANDED = gen.body_of(generated_text())


def declaration() -> ModuleDeclaration:
    decl = extract_declaration([EXAMPLE / IP_FILE], "vendor_fifo")
    assert decl is not None
    return decl


def _rules(issues: list[Issue]) -> list[str]:
    return [i.rule or "" for i in issues]


def _check(text: str) -> list[Issue]:
    return validate(text, declaration(), file="rtl/emacs/w.src.sv", wrapper=WRAPPER)


def test_the_declaration_is_the_ip_header_only() -> None:
    decl = declaration()
    assert [p.name for p in decl.parameters] == ["Width", "Depth"]
    assert [(p.name, p.direction) for p in decl.ports][:3] == [
        ("clk_i", "input"),
        ("rst_ni", "input"),
        ("clr_i", "input"),
    ]
    assert len(decl.ports) == 11
    assert "mem_q" not in decl.model_dump_json()  # no line of the body


def test_the_committed_expansion_is_clean() -> None:
    assert _check(EXPANDED) == []


def test_instances_reads_each_connection() -> None:
    [inst] = instances(EXPANDED, "vendor_fifo")
    assert (inst.name, inst.autoinst) == ("u_fifo", True)
    ports = {c.port: c for c in inst.connections}
    assert len(ports) == 11
    assert ports["data_o"].expr == "o_data[7:0]" and ports["data_o"].templated
    assert ports["testmode_i"].expr == "1'b0"
    assert ports["pop_i"].text.startswith(".pop_i")
    assert not any(c.manual for c in inst.connections)


def test_comments_and_strings_never_count_as_code() -> None:
    text = (
        "module w;\n"
        "// vendor_fifo u_x (.clk_i (a));\n"
        '/* vendor_fifo u_y (.clk_i (a)); */ string s = "vendor_fifo u_z (.clk_i (a));";\n'
        "endmodule\n"
    )
    assert instances(text, "vendor_fifo") == []
    assert len(mask(text)) == len(text) and "u_x" not in mask(text)
    assert module_name(text) == "w"


def test_a_wrapper_that_does_not_instantiate_the_ip_is_refused() -> None:
    issues = _check("module tiny_fifo_wrap (input logic a);\nendmodule\n")
    assert _rules(issues) == ["wrap.no_instance"]
    assert "vendor_fifo" in issues[0].msg


def test_autoinst_that_expanded_to_nothing_is_refused() -> None:
    text = f"module {WRAPPER} ();\n  vendor_fifo u_fifo (/*AUTOINST*/);\nendmodule\n"
    [issue] = _check(text)
    assert issue.rule == "wrap.autoinst_empty" and issue.line == 2
    assert "expanded to no port" in issue.msg


def test_a_port_the_ip_does_not_declare_is_refused_with_the_connection() -> None:
    text = EXPANDED.replace(
        ".pop_i                      (i_pop)", ".drain_i (i_pop), .pop_i (i_pop)"
    )
    [issue] = _check(text)
    assert issue.rule == "wrap.unknown_port" and "`drain_i`" in issue.msg
    assert ".drain_i (i_pop)" in issue.msg


def test_a_port_left_out_of_the_instance_is_refused() -> None:
    text = EXPANDED.replace(
        "            .testmode_i                 (1'b0),                  // Templated\n", ""
    )
    [issue] = _check(text)
    assert issue.rule == "wrap.port_missing" and "`testmode_i`" in issue.msg


def test_a_port_the_template_does_not_map_is_refused_with_the_expanded_line() -> None:
    text = EXPANDED.replace("(o_full),                // Templated", "(full_o),")
    [issue] = _check(text)
    assert issue.rule == "wrap.untemplated"
    assert "`full_o`" in issue.msg and ".full_o" in issue.msg and "AUTO_TEMPLATE" in issue.msg


def test_an_input_left_open_is_an_error_and_an_open_output_a_note() -> None:
    text = EXPANDED.replace("(1'b0)", "()").replace("(unused_err)", "()")
    issues = _check(text)
    by_rule = {i.rule: i for i in issues}
    assert by_rule["wrap.input_open"].severity == "error"
    assert by_rule["wrap.output_open"].severity == "info"
    assert "tie it" in by_rule["wrap.input_open"].msg
    assert "unused_" in by_rule["wrap.output_open"].msg


def test_a_manual_connection_before_autoinst_is_not_untemplated() -> None:
    text = (
        f"module {WRAPPER} ();\n"
        "  vendor_fifo u_fifo (.clk_i (clk),\n"
        "    /*AUTOINST*/\n"
        "    .rst_ni (rst_n), .clr_i (a), .testmode_i (b), .push_i (c), .data_i (d), .full_o (e),\n"
        "    .pop_i (f), .data_o (g), .empty_o (h), .err_o (i));\n"
        "endmodule\n"
    )
    issues = _check(text)
    manual = [c for i in instances(text, "vendor_fifo") for c in i.connections if c.manual]
    assert [c.port for c in manual] == ["clk_i"]
    assert "wrap.untemplated" in _rules(issues)  # the templated-looking ones are flagged...
    assert not any("`clk_i`" in i.msg for i in issues)  # ...but not the hand-written one


def test_the_module_name_must_be_the_wrappers() -> None:
    text = EXPANDED.replace(f"module {WRAPPER}", "module other_wrap")
    [issue] = _check(text)
    assert issue.rule == "wrap.module_name" and "other_wrap" in issue.msg


def test_issues_point_at_the_line_the_caller_maps_them_to() -> None:
    text = EXPANDED.replace("(o_full),                // Templated", "(full_o),")
    issues = validate(text, declaration(), file="s.sv", line_for=lambda inst: 42)
    assert [i.line for i in issues] == [42]
