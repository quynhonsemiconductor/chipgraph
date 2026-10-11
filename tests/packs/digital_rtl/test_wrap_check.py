"""M2-12: the check `wrap_check` (`checks/wrap_check.py`).

Without emacs: the logic on canned expansions (a canned runner "writes" the text emacs
would), the fallback when emacs is missing. With emacs (skipped when it or verilog-mode is
missing): the committed sample passes, a hand edit and a broken template fail, and the
generated file passes real Verilator lint and the project's naming check.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from wrapper_helpers import (
    OUT,
    SRC,
    WRAPPER,
    CannedRunner,
    generated_text,
    make_project,
    model_style_source,
    needs_emacs,
    needs_verilator,
    source_text,
    tool_context,
)

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks.generated import verify
from chipgraph.checks.wrap_check import MAX_HUNKS, WrapCheck, compare
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.packs.digital_rtl.wrapper import emacs as gen

BODY = gen.body_of(generated_text())
INSTANCE_LINE = source_text().splitlines().index("  vendor_fifo #(") + 1


def _run(
    root: Path, runner: object | None = None, params: dict[str, str] | None = None, **args: object
) -> CheckResult:
    spec = CheckSpec(id="wrap_check", capability="wrap_check", adapter="wrap_check", args=args)
    ctx = tool_context(root, runner or CannedRunner(expanded=BODY), params or {})
    return asyncio.run(WrapCheck().run(spec, ctx))


def _rules(result: CheckResult) -> list[str]:
    return [i.rule for i in result.issues]


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text()
    assert text.count(old) == 1, (rel, old)
    path.write_text(text.replace(old, new))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_project(tmp_path / "t", commit=False)


def test_the_check_is_registered() -> None:
    from importlib.metadata import entry_points

    [ep] = [e for e in entry_points(group="chipgraph.adapters.check") if e.name == "wrap_check"]
    assert ep.load() is WrapCheck


def test_the_committed_wrapper_passes_a_regeneration_that_equals_it(root: Path) -> None:
    runner = CannedRunner(expanded=BODY)
    result = _run(root, runner)
    assert result.status == "pass", result.issues
    assert result.issues == ()
    assert len(runner.commands) == 2  # the probe and the expansion, both through the runner


def test_a_hand_edit_is_reported_as_a_hunk_at_the_committed_line(root: Path) -> None:
    path = root / OUT
    old = "  output logic          o_full,                 // From u_fifo of vendor_fifo.v\n"
    new = "  output logic          o_full_flag,            // From u_fifo of vendor_fifo.v\n"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    result = _run(root)
    assert result.status == "fail"
    [issue] = [i for i in result.issues if i.rule == "wrap.differs"]
    committed = path.read_text().splitlines()
    assert issue.file == OUT and committed[issue.line - 1].strip().startswith("output logic")
    assert "o_full_flag" in committed[issue.line - 1]
    assert "- " in issue.msg and "+ " in issue.msg and "o_full," in issue.msg
    assert "run the generator" in issue.msg and "digital-rtl/wrapper_gen" in issue.msg
    assert "edited after it was generated" in issue.msg  # the marker no longer matches


def test_each_differing_hunk_is_one_issue(root: Path) -> None:
    path = root / OUT
    text = path.read_text()
    text = text.replace("(o_full)", "(o_full_x)").replace("(i_pop)", "(i_pop_x)")
    path.write_text(text)
    result = _run(root)
    hunks = [i for i in result.issues if i.rule == "wrap.differs"]
    assert len(hunks) == 2 and len({i.line for i in hunks}) == 2
    assert all(i.severity == "error" for i in hunks)


def test_a_line_missing_from_the_committed_file_is_a_hunk(root: Path) -> None:
    path = root / OUT
    text = path.read_text().replace(
        "            .err_o                      (unused_err),            // Templated\n", ""
    )
    path.write_text(text)
    [issue] = [i for i in _run(root).issues if i.rule == "wrap.differs"]
    assert "is missing lines of" in issue.msg and "+             .err_o" in issue.msg


def test_a_difference_in_whitespace_only_is_a_warning(root: Path) -> None:
    path = root / OUT
    path.write_text(path.read_text().replace("(o_full),                //", "(o_full), //"))
    result = _run(root)
    assert result.status == "pass"
    [issue] = result.issues
    assert (issue.rule, issue.severity) == ("wrap.whitespace", "warning")
    assert "whitespace only" in issue.msg


def test_more_hunks_than_the_cap_are_summarised() -> None:
    old = "\n".join(f"line {i}" for i in range(0, 100, 1))
    new = "\n".join(f"line {i}" if i % 2 else f"other {i}" for i in range(0, 100, 1))
    issues = compare(old, new, "w.sv", "run the generator")
    assert len(issues) == MAX_HUNKS + 1 and "more differing hunks" in issues[-1].msg
    shown = compare("\n".join("a" * 5 for _ in range(40)), "b", "w.sv", "x")[0].msg
    assert "more lines" in shown


def test_a_missing_generated_file_is_reported_in_block_mode_not_in_task_mode(root: Path) -> None:
    (root / OUT).unlink()
    block = _run(root, params={"block": "top"})
    assert block.status == "fail" and _rules(block) == ["wrap.not_generated"]
    assert block.issues[0].file == OUT and "run the generator" in block.issues[0].msg
    # the author's check runs before the generator: no file yet is not its fault
    task = _run(root, params={"block": "top", "wrapper": WRAPPER})
    assert task.status == "pass", task.issues


def test_in_task_mode_a_file_from_an_older_source_is_not_compared(root: Path) -> None:
    (root / SRC).write_text(source_text() + "// a new comment, so the old file is stale\n")
    params = {"block": "top", "wrapper": WRAPPER}
    assert _run(root, params=params).status == "pass"
    assert _run(root, params=params, compare="always").status == "fail"
    assert _run(root, compare="never").status == "pass"


def test_the_compare_argument_is_validated(root: Path) -> None:
    result = _run(root, compare="sometimes")
    assert result.status == "error" and "compare" in result.issues[0].msg


def test_a_template_that_matched_nothing_fails_with_the_empty_autoinst(root: Path) -> None:
    runner = CannedRunner(expanded=source_text())
    result = _run(root, runner)  # the "expansion" is the unexpanded source
    assert result.status == "fail"
    [issue] = [i for i in result.issues if i.rule == "wrap.autoinst_empty"]
    assert issue.file == SRC and issue.line == INSTANCE_LINE and "no port" in issue.msg


def test_a_port_the_template_leaves_unmapped_fails_with_the_expanded_line(root: Path) -> None:
    expanded = BODY.replace("(o_full),                // Templated", "(full_o),")
    result = _run(root, CannedRunner(expanded=expanded))
    assert result.status == "fail"
    [issue] = [i for i in result.issues if i.rule == "wrap.untemplated"]
    assert issue.file == SRC and issue.line == INSTANCE_LINE
    assert "`full_o`" in issue.msg and ".full_o                     (full_o)" not in issue.msg
    assert ".full_o (full_o)," in issue.msg  # the expanded connection, whitespace squeezed
    assert "wrap.differs" in _rules(result)  # and the committed file disagrees as well


def test_a_port_the_vendor_module_does_not_declare_fails(root: Path) -> None:
    expanded = BODY.replace(".pop_i  ", ".pop_ii ")
    result = _run(root, CannedRunner(expanded=expanded))
    rules = _rules(result)
    assert result.status == "fail"
    assert "wrap.unknown_port" in rules and "wrap.port_missing" in rules
    [unknown] = [i for i in result.issues if i.rule == "wrap.unknown_port"]
    assert "`pop_ii`" in unknown.msg and "vendor_fifo" in unknown.msg


def test_an_input_left_open_fails(root: Path) -> None:
    expanded = BODY.replace("(1'b0)", "()")
    result = _run(root, CannedRunner(expanded=expanded))
    assert result.status == "fail" and "wrap.input_open" in _rules(result)


def test_emacs_failing_on_the_source_fails_with_its_message(root: Path) -> None:
    log = "%Error: tiny_fifo_wrap.sv:40: Cannot locate module vendor_fifo\n"
    result = _run(root, CannedRunner(expanded=None, fail_log=log))
    assert result.status == "fail"
    assert [(i.file, i.line, i.rule) for i in result.issues] == [(SRC, 40, "wrap.emacs")]


def test_a_missing_source_fails(root: Path) -> None:
    (root / SRC).unlink()
    result = _run(root)
    assert result.status == "fail" and _rules(result) == ["wrap.src_missing"]


def test_lisp_beyond_the_allowed_list_fails_without_running_emacs(root: Path) -> None:
    (root / SRC).write_text(source_text() + '\n/*AUTO_LISP(shell-command "id")*/\n')
    runner = CannedRunner(expanded=BODY)
    result = _run(root, runner)
    assert result.status == "fail" and _rules(result) == ["wrap.lisp"] and runner.commands == []


def test_a_local_variables_block_is_a_note(root: Path) -> None:
    (root / SRC).write_text(
        source_text() + "\n// Local Variables:\n// eval: (kill-emacs)\n// End:\n"
    )
    result = _run(root, params={"block": "top", "wrapper": WRAPPER})
    assert result.status == "pass"
    [note] = [i for i in result.issues if i.rule == "wrap.lisp"]
    assert note.severity == "info" and "ignored" in note.msg


def test_a_vendor_module_the_files_do_not_declare_is_an_infra_error(root: Path) -> None:
    (root / "vendor/vendor_fifo/vendor_fifo.sv").write_text("module other (); endmodule\n")
    result = _run(root)
    assert result.status == "error" and _rules(result) == ["wrap.vendor_declaration"]


# --- without emacs -----------------------------------------------------------------------------


def test_without_emacs_a_fresh_marked_file_is_validated_and_passes_with_a_warning(
    root: Path,
) -> None:
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "pass", result.issues
    [warning] = result.issues
    assert warning.rule == "wrap.not_regenerated" and warning.severity == "warning"
    assert "apt install emacs-nox" in warning.msg


def test_without_emacs_the_committed_expansion_is_still_validated(root: Path) -> None:
    path = root / OUT
    broken = gen.render(BODY.replace("(1'b0)", "()"), SRC, source_text())
    path.write_text(broken)  # a correctly marked file, but its expansion is wrong
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "fail" and "wrap.input_open" in _rules(result)


def test_without_emacs_a_hand_edited_file_is_an_error_with_the_install_hint(root: Path) -> None:
    path = root / OUT
    path.write_text(path.read_text().replace("o_full", "o_fullx"))
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "error"
    [issue] = result.issues
    assert issue.rule == "wrap.emacs_missing" and "no valid generated marker" in issue.msg
    assert "emacs-nox" in issue.msg


def test_without_emacs_a_file_from_an_older_source_is_an_error(root: Path) -> None:
    (root / SRC).write_text(source_text() + "// changed\n")
    result = _run(root, CannedRunner(missing=True))
    assert (
        result.status == "error" and "not generated from the current source" in result.issues[0].msg
    )


def test_without_emacs_and_without_a_file_it_is_an_error(root: Path) -> None:
    (root / OUT).unlink()
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "error" and "does not exist" in result.issues[0].msg


def test_require_emacs_makes_a_missing_emacs_an_error_even_for_a_fresh_file(root: Path) -> None:
    result = _run(root, CannedRunner(missing=True), require_emacs=True)
    assert result.status == "error" and "require_emacs" in result.issues[0].msg


def test_emacs_without_verilog_mode_is_the_same_infra_error(root: Path) -> None:
    runner = CannedRunner(
        probe_code=255, probe_out="", probe_err="Cannot open load file verilog-mode"
    )
    (root / OUT).write_text("// edited\n")
    result = _run(root, runner)
    assert result.status == "error" and result.issues[0].rule == "wrap.verilog_mode_missing"


# --- scope and arguments ------------------------------------------------------------------------


def test_no_wrapper_in_the_project_is_skipped(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / ".chipgraph.yml").write_text("project: bare\n")
    result = _run(bare)
    assert result.status == "skipped" and result.issues[0].rule == "wrap.none"


def test_a_project_without_a_profile_is_an_error(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.status == "error" and "no .chipgraph.yml" in result.issues[0].msg


def test_an_unknown_wrapper_is_an_error(root: Path) -> None:
    result = _run(root, params={"block": "top", "wrapper": "nope"})
    assert result.status == "error" and "no wrapper 'nope'" in result.issues[0].msg


def test_the_idempotency_key_follows_the_files(root: Path) -> None:
    first = _run(root).idempotency_key
    assert _run(root).idempotency_key == first
    (root / SRC).write_text(source_text() + "// x\n")
    assert _run(root, compare="never").idempotency_key != first


# --- with a real emacs ---------------------------------------------------------------


@needs_emacs
def test_the_sample_wrapper_passes_wrap_check_with_a_real_emacs(root: Path) -> None:
    result = _run(root, LocalRunner())
    assert result.status == "pass", result.issues
    assert result.issues == ()


@needs_emacs
def test_a_hand_edit_fails_with_a_real_emacs(root: Path) -> None:
    _edit(root, OUT, "output logic          o_full, ", "output logic          o_full_x,")
    result = _run(root, LocalRunner())
    assert result.status == "fail"
    [issue] = result.issues
    assert issue.rule == "wrap.differs" and issue.file == OUT and issue.line is not None
    assert (root / OUT).read_text().splitlines()[issue.line - 1].strip().startswith("output logic")
    assert verify((root / OUT).read_text()) is False


@needs_emacs
def test_a_broken_template_fails_with_a_real_emacs(root: Path) -> None:
    _edit(root, SRC, "      .\\(.*\\)_o   (o_\\1[]),\n", "")
    result = _run(root, LocalRunner())
    assert result.status == "fail"
    unmapped = sorted(i.msg.split("`")[5] for i in result.issues if i.rule == "wrap.untemplated")
    assert unmapped == ["data_o", "empty_o", "full_o"]
    assert all(i.file == SRC for i in result.issues if i.rule == "wrap.untemplated")
    assert "wrap.differs" in _rules(result)  # the committed expansion is now stale


@needs_emacs
def test_dropping_the_single_port_lines_lets_the_regexps_map_them(root: Path) -> None:
    _edit(root, SRC, "      .testmode_i (1'b0),\n      .err_o      (unused_err),\n", "")
    result = _run(root, LocalRunner())
    # the regexps catch `testmode_i` and `err_o`, so no port is unmapped, but the wrapper
    # now has two more ports than the committed file says: the generator must run again
    assert result.status == "fail"
    assert "wrap.untemplated" not in _rules(result) and "wrap.differs" in _rules(result)


@needs_emacs
def test_a_template_for_the_wrong_module_matches_nothing(root: Path) -> None:
    _edit(root, SRC, "vendor_fifo AUTO_TEMPLATE", "vendor_fifo_old AUTO_TEMPLATE")
    result = _run(root, LocalRunner())
    assert result.status == "fail"
    assert {"wrap.untemplated"} <= set(_rules(result))


@needs_emacs
def test_a_source_written_like_a_model_passes_once_generated(tmp_path: Path) -> None:
    root = make_project(tmp_path / "t", generated=False, commit=False)
    (root / SRC).write_text(model_style_source())
    task = {"block": "top", "wrapper": WRAPPER}
    assert _run(root, LocalRunner(), task).status == "pass"  # before the generator
    assert (
        _run(root, LocalRunner(), params=None).status == "fail"
    )  # the project check wants the file
    from test_wrapper_generator import _run as generate  # type: ignore[import-not-found]

    assert generate(root, LocalRunner()).status == "pass"
    assert _run(root, LocalRunner()).status == "pass"
    assert verify((root / OUT).read_text()) is True


@needs_emacs
@needs_verilator
def test_the_generated_wrapper_passes_real_verilator_lint(root: Path) -> None:
    done = subprocess.run(
        ["make", "lint", "BLOCK=top"], cwd=root, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "-f filelists/tiny_fifo_wrap.f --top-module tiny_fifo_wrap" in done.stdout
    assert "%Warning" not in done.stdout + done.stderr
    (root / OUT).write_text((root / OUT).read_text().replace("o_data[7:0]", "o_data[6:0]"))
    broken = subprocess.run(
        ["make", "lint", "BLOCK=top"], cwd=root, capture_output=True, text=True, check=False
    )
    assert broken.returncode != 0  # lint reads the generated file, so a bad edit shows


def test_the_generated_wrapper_passes_the_naming_check(root: Path) -> None:
    from chipgraph.app.checks import ProfileCheckRunner
    from chipgraph.app.context import AppContext
    from chipgraph.core.contracts import ArtifactRef, RuleInstance

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    params = {"block": "top", "wrapper": WRAPPER}
    instance = RuleInstance(
        rule_id="digital-rtl/wrapper_gen",
        params=params,
        outputs=(ArtifactRef(kind="rtl", path=OUT),),
        instance_id=RuleInstance.make_id("digital-rtl/wrapper_gen", params),
    )
    result = asyncio.run(ProfileCheckRunner(AppContext.load(root)).run("naming", instance))
    assert result.status == "pass", result.issues
    # and the check does look at the generated file: a name the rules reject fails
    (root / OUT).write_text((root / OUT).read_text().replace("unused_err", "BadSignal"))
    bad = asyncio.run(ProfileCheckRunner(AppContext.load(root)).run("naming", instance))
    assert bad.status == "fail" and any(i.file == OUT for i in bad.issues)


def test_the_generated_check_understands_the_generated_wrapper(root: Path) -> None:
    from chipgraph.checks import GeneratedCheck

    spec = CheckSpec(
        id="generated", capability="layout", adapter="generated", args={"files": ["rtl/tiny_*.sv"]}
    )
    ctx = tool_context(root, CannedRunner(), {})
    ok = asyncio.run(GeneratedCheck().run(spec, ctx))
    assert ok.status == "pass", ok.issues
    path = root / OUT
    path.write_text(path.read_text() + "// a hand edit\n")
    bad = asyncio.run(GeneratedCheck().run(spec, ctx))
    assert bad.status == "fail" and bad.issues[0].file == OUT
