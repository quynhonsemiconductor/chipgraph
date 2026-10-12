"""Security review of M2-12 (findings 13, 15, 16, 19, the `wrap_check` side): unreadable or
non-UTF-8 files, `timeout_s`, the `emacs`/`src`/`out` args, and the marker fallback in CI.

No emacs needed: a canned runner answers for it."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from wrapper_helpers import (
    OUT,
    SRC,
    CannedRunner,
    generated_text,
    make_project,
    needs_emacs,
    source_text,
    tool_context,
)

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks.wrap_check import MAX_TIMEOUT_S, WrapCheck
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.packs.digital_rtl.wrapper import emacs as gen

BODY = gen.body_of(generated_text())
ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0


def _run(root: Path, runner: object | None = None, **args: object) -> CheckResult:
    spec = CheckSpec(id="wrap_check", capability="wrap_check", adapter="wrap_check", args=args)
    ctx = tool_context(root, runner or CannedRunner(expanded=BODY), {})
    return asyncio.run(WrapCheck().run(spec, ctx))


def _rules(result: CheckResult) -> list[str]:
    return [i.rule for i in result.issues]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CI", raising=False)
    return make_project(tmp_path / "t", commit=False)


# --- finding 13: bytes that are not UTF-8, unreadable files -----------------------------------


@pytest.mark.parametrize("rel", [SRC, OUT])
def test_a_non_utf8_byte_is_a_read_issue_not_a_crash(root: Path, rel: str) -> None:
    path = root / rel
    path.write_bytes(path.read_bytes() + b"// \xff\n")
    result = _run(root)
    assert result.status == "fail"
    assert [(i.rule, i.file) for i in result.issues] == [("wrap.read", rel)]
    assert "not UTF-8" in result.issues[0].msg


@pytest.mark.skipif(ROOT_USER or os.name != "posix", reason="needs binding POSIX permissions")
def test_an_unreadable_source_is_an_error_not_a_crash(root: Path) -> None:
    path = root / SRC
    path.chmod(0)
    try:
        result = _run(root)
    finally:
        path.chmod(0o644)
    assert result.status == "error" and _rules(result) == ["wrap.read"]


# --- finding 15: timeout_s -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["inf", "1e999", "10", True, False, float("inf"), float("nan"), 0, -1, MAX_TIMEOUT_S + 1],
)
def test_a_timeout_that_is_not_a_finite_bounded_number_is_refused(
    root: Path, value: object
) -> None:
    runner = CannedRunner(expanded=BODY)
    result = _run(root, runner, timeout_s=value)
    assert result.status == "error" and _rules(result) == ["wrap.args"]
    assert "timeout_s" in result.issues[0].msg and runner.commands == []


@pytest.mark.parametrize("value", [0.5, 1, 120, MAX_TIMEOUT_S])
def test_a_bounded_timeout_is_accepted(root: Path, value: object) -> None:
    assert _run(root, timeout_s=value).status == "pass"


# --- finding 16: the marker fallback is not a proof; CI requires emacs -----------------------


def _forged(root: Path) -> None:
    """Edit the generated body and re-render the marker and header, as an attacker could."""
    body = BODY.replace("unused_err", "forged_err")
    (root / OUT).write_text(gen.render(body, SRC, source_text()), encoding="utf-8")


def test_outside_ci_a_forged_marker_passes_the_fallback_with_a_warning_saying_so(
    root: Path,
) -> None:
    _forged(root)
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "pass"
    [warning] = result.issues
    assert warning.rule == "wrap.not_regenerated" and "not a signature" in warning.msg


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_in_ci_a_missing_emacs_is_an_error(
    root: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _forged(root)
    monkeypatch.setenv("CI", value)
    result = _run(root, CannedRunner(missing=True))
    assert result.status == "error" and _rules(result) == ["wrap.emacs_missing"]
    assert "CI environment variable" in result.issues[0].msg


def test_in_ci_the_profile_may_accept_the_fallback(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    result = _run(root, CannedRunner(missing=True), require_emacs=False)
    assert result.status == "pass" and _rules(result) == ["wrap.not_regenerated"]


@pytest.mark.parametrize("value", ["", "0", "false", "No"])
def test_a_ci_variable_that_says_no_is_not_ci(
    root: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CI", value)
    assert _run(root, CannedRunner(missing=True)).status == "pass"


def test_with_emacs_a_forged_marker_fails_in_ci_too(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forged(root)
    monkeypatch.setenv("CI", "true")
    result = _run(root)
    assert result.status == "fail" and "wrap.differs" in _rules(result)


@needs_emacs
def test_with_a_real_emacs_in_ci_the_sample_passes_and_a_forgery_fails(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    assert _run(root, LocalRunner()).status == "pass"
    _forged(root)
    result = _run(root, LocalRunner())
    assert result.status == "fail" and "wrap.differs" in _rules(result)


# --- finding 19: src, out and emacs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("out", ".git/hooks/pre-commit"),
        ("out", ".GIT/hooks/pre-commit"),
        ("out", "sub/.git/config"),
        ("src", ".chipgraph/naming.yml"),
        ("src", ".Chipgraph/naming.yml"),
        ("out", "/etc/hosts"),
        ("src", "../outside.sv"),
        ("out", "rtl/../../x.sv"),
    ],
)
def test_src_and_out_may_not_name_protected_or_outside_files(
    root: Path, key: str, value: str
) -> None:
    runner = CannedRunner(expanded=BODY)
    result = _run(root, runner, **{key: value})
    assert result.status == "error" and _rules(result) == ["wrap.args"]
    assert runner.commands == []
    assert "rules:" not in result.issues[0].msg  # never quotes the file


def test_a_link_into_git_is_refused_as_resolved(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / "rtl/hooks").symlink_to("../.git")
    result = _run(root, out="rtl/hooks/pre-commit")
    assert result.status == "error" and ".git" in result.issues[0].msg


@pytest.mark.parametrize("value", ["./emacs", "bin/emacs", "/nonexistent/emacs", "..\\emacs"])
def test_an_emacs_path_must_be_absolute_and_executable(root: Path, value: str) -> None:
    runner = CannedRunner(expanded=BODY)
    result = _run(root, runner, emacs=value)
    assert result.status == "error" and "args['emacs']" in result.issues[0].msg
    assert runner.commands == []


def test_a_non_executable_absolute_emacs_is_refused(root: Path) -> None:
    assert _run(root, emacs=str(root / SRC)).status == "error"


@pytest.mark.parametrize("value", ["emacs", "emacs-31"])
def test_a_command_name_or_an_absolute_executable_is_accepted(root: Path, value: str) -> None:
    runner = CannedRunner(expanded=BODY)
    assert _run(root, runner, emacs=value).status == "pass"
    assert runner.commands[0][0] == value
    runner = CannedRunner(expanded=BODY)
    assert _run(root, runner, emacs=sys.executable).status == "pass"
