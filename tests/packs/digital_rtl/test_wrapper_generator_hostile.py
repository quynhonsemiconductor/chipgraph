"""The generator `emacs_auto` against a hostile source, vendor file or argument.

Without emacs: the shared argument helpers (`timeout_arg`, `emacs_arg`, `guarded`), the gen
tool refusing bad arguments (a canned runner, which emacs never reaches), `neutralize` on
hostile text, and the `GUARD` snippet on the command line. With emacs (skipped when it or
verilog-mode is missing): the sample still expands byte-identically with the guard on, and
Lisp that writes a file runs neither from a vendor file nor past the guard.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest
from wrapper_helpers import (
    OUT,
    SRC,
    CannedRunner,
    generated_text,
    make_project,
    needs_emacs,
    tool_context,
)

from chipgraph.core.contracts import CheckSpec
from chipgraph.packs.digital_rtl.wrapper import emacs as gen
from chipgraph.packs.digital_rtl.wrapper import lisp
from chipgraph.packs.digital_rtl.wrapper.common import (
    MAX_TIMEOUT_S,
    WrapperError,
    emacs_arg,
    guarded,
    timeout_arg,
)

CANNED = "module tiny_fifo_wrap ();\nendmodule\n"


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(id="emacs_auto", capability="gen", adapter="emacs_auto", args=args)


def _run(root: Path, runner: object, spec: CheckSpec | None = None):  # type: ignore[no-untyped-def]
    tool = gen.EmacsAutoTool()
    return asyncio.run(tool.run(spec or _spec(), tool_context(root, runner, None)))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_project(tmp_path / "t", generated=False, commit=False)


# --- the argument helpers ----------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 0.5, 30.0, MAX_TIMEOUT_S])
def test_timeout_arg_takes_a_finite_positive_number_up_to_the_cap(value: float) -> None:
    assert timeout_arg(value, 30.0) == float(value)


def test_timeout_arg_defaults_when_none() -> None:
    assert timeout_arg(None, 30.0) == 30.0


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, MAX_TIMEOUT_S + 1, math.inf, math.nan, "10", [10]]
)
def test_timeout_arg_refuses_anything_else(value: object) -> None:
    with pytest.raises(WrapperError, match="timeout_s"):
        timeout_arg(value, 30.0)


def test_emacs_arg_takes_a_bare_name_or_an_absolute_executable(tmp_path: Path) -> None:
    assert emacs_arg("emacs") == "emacs"
    exe = tmp_path / "my-emacs"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    assert emacs_arg(str(exe)) == str(exe)


@pytest.mark.parametrize("kind", ["relative", "backslash", "missing", "directory", "not-exec"])
def test_emacs_arg_refuses_a_path_that_is_not_an_absolute_executable(
    tmp_path: Path, kind: str
) -> None:
    plain = tmp_path / "plain"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)
    value = {
        "relative": "bin/emacs",
        "backslash": "bin\\emacs",
        "missing": str(tmp_path / "nope"),
        "directory": str(tmp_path),
        "not-exec": str(plain),
    }[kind]
    with pytest.raises(WrapperError, match="emacs"):
        emacs_arg(value)


def test_guarded_takes_an_ordinary_repo_file(tmp_path: Path) -> None:
    assert guarded(tmp_path, "rtl/x.sv") == tmp_path / "rtl/x.sv"
    assert guarded(tmp_path, "docs/chipgraph/x.sv") == tmp_path / "docs/chipgraph/x.sv"


@pytest.mark.parametrize(
    "rel",
    [
        ".git/hooks/pre-commit",
        ".GIT/config",
        "rtl/.git/x.sv",
        "rtl/.Git/x.sv",
        ".chipgraph/state.db",
        ".ChipGraph/x",
    ],
)
def test_guarded_refuses_git_and_chipgraph_as_written(tmp_path: Path, rel: str) -> None:
    with pytest.raises(WrapperError, match=r"\.git/ or \.chipgraph/"):
        guarded(tmp_path, rel, "out")


def test_guarded_refuses_git_and_chipgraph_as_resolved(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chipgraph").mkdir()
    (tmp_path / "hooks").symlink_to(tmp_path / ".git")
    (tmp_path / "state").symlink_to(tmp_path / ".chipgraph")
    for rel in ("hooks/pre-commit", "state/x.db"):
        with pytest.raises(WrapperError, match=r"\.git/ or \.chipgraph/"):
            guarded(tmp_path, rel)


def test_guarded_refuses_a_path_outside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").symlink_to(tmp_path)
    for rel in ("../x.sv", "/etc/passwd", "out/x.sv"):
        with pytest.raises(WrapperError):
            guarded(repo, rel)


# --- the gen tool refuses bad arguments before emacs runs ---------------------------------------


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"timeout_s": True}, "timeout_s"),
        ({"timeout_s": 0}, "timeout_s"),
        ({"timeout_s": 601}, "timeout_s"),
        ({"timeout_s": float("inf")}, "timeout_s"),
        ({"emacs": "bin/emacs"}, "emacs"),
        ({"emacs": "./emacs"}, "emacs"),
        ({"out": ".git/hooks/post-checkout"}, "out"),
        ({"out": "rtl/.GIT/x.sv"}, "out"),
        ({"out": ".chipgraph/state.db"}, "out"),
        ({"src": ".git/config"}, "src"),
    ],
)
def test_the_tool_refuses_a_bad_argument_without_running_emacs(
    root: Path, args: dict[str, object], needle: str
) -> None:
    runner = CannedRunner(expanded=CANNED)
    result = _run(root, runner, _spec(**args))
    assert result.status == "error" and result.issues[0].rule == "wrap.args"
    assert needle in result.issues[0].msg
    assert runner.commands == []


def test_the_tool_refuses_an_out_that_resolves_into_git(root: Path) -> None:
    (root / "gen").symlink_to(root / ".git")
    runner = CannedRunner(expanded=CANNED)
    result = _run(root, runner, _spec(out="gen/hooks/pre-commit"))
    assert result.status == "error" and result.issues[0].rule == "wrap.args"
    assert runner.commands == [] and not (root / ".git/hooks/pre-commit").exists()


def test_the_default_timeout_is_30_s_and_case_fold_is_pinned_off(root: Path) -> None:
    assert gen.TIMEOUT_S == 30.0
    assert dict(gen.PINNED)["verilog-case-fold"] == "nil"
    runner = CannedRunner(expanded=CANNED)
    assert _run(root, runner).status == "pass"
    assert "verilog-case-fold nil" in runner.commands[1][runner.commands[1].index("--eval") + 1]


def test_a_guard_refusal_in_the_log_is_a_lisp_issue(root: Path) -> None:
    log = 'chipgraph-guard: a call of a function not allowed: (shell-command "x")\n'
    for code in (255, 0):  # refused even if verilog-mode caught the error
        runner = CannedRunner(expanded=None if code else CANNED, fail_log=log, fail_code=code)
        if code == 0:
            runner = _LoggingRunner(expanded=CANNED, fail_log=log)
        result = _run(root, runner)
        assert result.status == "fail", result.issues
        [issue] = result.issues
        assert (issue.file, issue.rule) == (SRC, "wrap.lisp")
        assert "chipgraph-guard: a call of a function not allowed" in issue.msg
        assert not (root / OUT).exists()


class _LoggingRunner(CannedRunner):
    """Expands like `CannedRunner`, and exits 0 with `fail_log` on stderr."""

    async def run(self, cmd, *, cwd, env=None, timeout_s=None):  # type: ignore[no-untyped-def]
        result = await super().run(cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        if "-f" in cmd:
            return result.model_copy(update={"stderr": self.fail_log})
        return result


# --- neutralize ----------------------------------------------------------------------------------

HOSTILE = (
    '// auto_lisp(write-region "x" nil "/tmp/p")\n'
    '/* Auto_Lisp  \n(shell-command "id") */\n'
    '// AUTO_LI\u017fP(delete-file "x")\n'  # long s folds to s
    "// \uff21UTO_LISP(x)\n"  # fullwidth A
    "/*AUTOINSERTLISP(foo)*/ /*autoinsertlast(bar)*/\n"
    '// Local\tVARIABLES :\n// eval: (shell-command "id")\n// End:\n'
    "// -*- mode: verilog; eval: (x) -*-\n"
    "// L\u043ecal Variables:\n"  # Cyrillic o: not ASCII, left alone (emacs reads it as text)
    '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/\n'
)


def _live(text: str) -> list[str]:
    """What is still active in `text`: Lisp entry points, file-local marks."""
    import re

    sk = lisp.skeleton(text)
    found = [m.group(0) for m in re.finditer(r"auto_lisp\(|autoinsertl(?:isp|ast)", sk)]
    found += [m.group(0) for m in re.finditer(r"local\s*variables\s*:|-\*-", sk)]
    return found


def test_neutralize_breaks_every_lisp_entry_and_file_local_mark_in_a_vendor_copy() -> None:
    out = gen.neutralize(HOSTILE)
    assert _live(out) == []
    assert out.count("\n") == HOSTILE.count("\n")  # line for line
    assert "AUTO_LISP (setq verilog-auto-output-ignore-regexp" in out  # vendor: broken too
    assert "AUTOINSERT LISP(foo)" in out and "AUTOINSERT LAST(bar)" in out
    assert "(ignored):" in out and "- * -" in out


def test_neutralize_keeps_only_the_accepted_form_in_the_source_copy() -> None:
    out = gen.neutralize(HOSTILE, keep_accepted=True)
    assert _live(out) == ["auto_lisp("]
    assert '/*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/' in out
    assert "// AUTO_LISP (write-region" in out
    assert out.count("\n") == HOSTILE.count("\n")


@pytest.mark.parametrize(
    "text",
    [
        "/* AUTO_LISP(x) */",  # a space before: not the accepted form
        "/*auto_lisp(x)*/",  # lowercase
        "/*AUTO_LISP (x)*/",
        "AUTO_LISP(x)",  # at the start of the text
        "/*AUTO_LI\u017fP(x)*/",
    ],
)
def test_neutralize_keeps_no_other_spelling_even_in_the_source(text: str) -> None:
    assert _live(gen.neutralize(text, keep_accepted=True)) == []


def test_neutralize_leaves_ordinary_text_alone() -> None:
    text = "module m (input a); // a comment, AUTO_INST, AUTOARG\nendmodule\n"
    assert gen.neutralize(text) == text


# --- the guard -----------------------------------------------------------------------------------


def test_the_guard_is_an_eval_after_the_settings_advising_eval_and_eval_region() -> None:
    argv = gen.emacs_argv("emacs", Path("/s/w.sv"), [Path("/s/ip/0/v.sv")])
    first = argv.index("--eval")
    assert argv[first + 1] == gen.settings([Path("/s/ip/0/v.sv")])
    assert argv[first + 2 : first + 4] == ["--eval", gen.GUARD]
    assert argv[first + 4] == "/s/w.sv"
    guard = gen.GUARD
    assert isinstance(guard, str) and guard.count("(") == guard.count(")")
    assert "(advice-add (quote eval) :around" in guard
    assert "(advice-add (quote eval-region) :around" in guard
    assert "(narrow-to-region start end)" in guard and "(read (current-buffer))" in guard
    assert gen.GUARD_PREFIX == "chipgraph-guard:" and gen.GUARD_PREFIX in guard
    for name in lisp.FUNCTIONS:
        assert gen.lisp_string(name) in guard, name
    assert "verilog-auto-[a-z]+-ignore-regexp" in guard
    assert "\\\\`\\\\(?:" in guard  # SETQ_VARIABLE, in Emacs regexp syntax


# --- with a real emacs ---------------------------------------------------------------------------


@needs_emacs
def test_with_the_guard_the_sample_is_byte_identical_and_lisp_writes_no_file(
    tmp_path: Path,
) -> None:
    from chipgraph.adapters.runner.local import LocalRunner

    # The sample (it has an accepted AUTO_LISP setq) expands as committed, guard on.
    root = make_project(tmp_path / "t", generated=False, commit=False)
    result = _run(root, LocalRunner())
    assert result.status == "pass", result.issues
    assert (root / OUT).read_text() == generated_text()
    assert gen.GUARD_PREFIX not in result.log_tail

    canary = tmp_path / "canary"
    attack = f'// auto_lisp(write-region "pwned" nil {gen.lisp_string(str(canary))})\n'

    # In a vendor file (not scanned, only neutralised): the expansion runs, nothing is written.
    vendor = next((root / "vendor").rglob("*.sv"))
    vendor.write_text(attack + vendor.read_text())
    result = _run(root, LocalRunner())
    assert result.status == "pass", result.issues
    assert not canary.exists()

    # Past the scanner and neutralize (emacs given the raw text): the guard refuses it.
    scratch = Path(tempfile.mkdtemp(prefix="chipgraph-test-"))
    try:
        target = scratch / "w.sv"
        target.write_text(
            "module w;\n"
            f'/*AUTO_LISP(write-region "pwned" nil {gen.lisp_string(str(canary))})*/\n'
            "endmodule\n"
        )
        argv = gen.emacs_argv(os.environ.get("EMACS", "emacs"), target, [])
        run = asyncio.run(LocalRunner().run(argv, cwd=scratch, timeout_s=60))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    assert not canary.exists()
    assert gen.GUARD_PREFIX in run.stdout + run.stderr
    assert "write-region" in run.stdout + run.stderr
