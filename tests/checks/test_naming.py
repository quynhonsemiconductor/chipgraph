"""Tests for `NamingCheck` and its data model.

Fixtures under `naming_fixtures/` are checked against the built-in `org:qnsc/naming-v1.yml`
rule. `clean.sv` must pass; `violations.sv` seeds one or more violations whose exact
(line, rule) pairs are asserted below. The remaining behaviours (exemption comment,
vendor exclusion, `{block}` scope, parse error, bad rules ref, registration, schema) get
one test each.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import yaml
from conftest import FakeRunner, make_ctx, write

from chipgraph.checks import NamingCheck
from chipgraph.checks._naming_rules import NamingRules, schema_json
from chipgraph.core.config.loader import resolve_data_ref
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import Check
from chipgraph.core.plugin_api.types import ToolContext

_FIXTURES = Path(__file__).parent / "naming_fixtures"
_RULES = "org:qnsc/naming-v1.yml"

# The exact violations `violations.sv` should produce, as (1-based line, rule id).
EXPECTED: set[tuple[int, str]] = {
    (4, "2.1 module"),
    (5, "1.2 port prefix"),
    (9, "2.4 parameter"),
    (10, "2.3 signal"),
    (11, "2.3 signal"),
    (12, "1.3 active low"),
    (12, "2.3 signal"),
    (13, "1.4 index"),
    (13, "2.3 signal"),
    (14, "1.5 vocabulary"),
    (15, "1.1 case"),
    (15, "2.3 signal"),
    (17, "2.4 parameter"),
    (19, "2.2 instance"),
}


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(id="naming", capability="naming", adapter="naming", args=args)


def _run(spec: CheckSpec, ctx: ToolContext) -> CheckResult:
    return asyncio.run(NamingCheck().run(spec, ctx))


def _copy_fixture(root: Path, name: str, rel: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURES / name, dest)


# --------------------------------------------------------------------------------------
# the check itself
# --------------------------------------------------------------------------------------


def test_is_a_check() -> None:
    check = NamingCheck()
    assert isinstance(check, Check)
    assert check.id == "naming"


def test_clean_file_passes(tmp_path: Path, runner: FakeRunner) -> None:
    _copy_fixture(tmp_path, "clean.sv", "design/timer/rtl/m_qnsc_timer.sv")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "timer"),
    )
    assert result.status == "pass"
    assert result.issues == ()


def test_violations_match_expected_line_and_rule(tmp_path: Path, runner: FakeRunner) -> None:
    _copy_fixture(tmp_path, "violations.sv", "design/blk/rtl/violations.sv")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    assert result.status == "fail"
    got = {(issue.line, issue.rule) for issue in result.issues}
    assert got == EXPECTED


def test_issues_are_ordered_by_file_line_rule(tmp_path: Path, runner: FakeRunner) -> None:
    _copy_fixture(tmp_path, "violations.sv", "design/blk/rtl/violations.sv")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    keys = [(i.file or "", i.line or 0, i.rule) for i in result.issues]
    assert keys == sorted(keys)


def test_exemption_comment_suppresses_its_line(tmp_path: Path, runner: FakeRunner) -> None:
    write(
        tmp_path,
        "design/blk/rtl/x.sv",
        "module m_qnsc_x;\n"
        "  logic BadCase;  // naming-check: ignore -- kept for a reason\n"
        "  logic AlsoBad;\n"
        "endmodule\n",
    )
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    lines = {issue.line for issue in result.issues}
    assert 2 not in lines  # exempted
    assert 3 in lines  # still reported


def test_vendor_paths_are_excluded_by_default(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/blk/rtl/vendor/ip.sv", "module BadName; logic clk_i; endmodule\n")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    assert result.status == "pass"
    assert result.issues == ()


def test_block_substitution_scopes_to_the_block(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/timer/rtl/BadName.sv", "module BadName; endmodule\n")
    write(tmp_path, "design/uart/rtl/AlsoBad.sv", "module AlsoBad; endmodule\n")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "timer"),
    )
    files = {issue.file for issue in result.issues}
    assert files == {"design/timer/rtl/BadName.sv"}  # uart is out of scope


def test_missing_block_for_scope_placeholder_is_an_error(
    tmp_path: Path, runner: FakeRunner
) -> None:
    write(tmp_path, "design/blk/rtl/x.sv", "module m_qnsc_x; endmodule\n")
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx(tmp_path, runner),  # no block param
    )
    assert result.status == "error"
    assert "block" in result.issues[0].msg


def test_parse_error_is_an_issue_not_an_exception(tmp_path: Path, runner: FakeRunner) -> None:
    write(
        tmp_path,
        "design/blk/rtl/broken.sv",
        "module m_qnsc_x (input logic );\n endmodule oops\n",
    )
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    assert result.status == "fail"
    assert any(issue.rule == "parse" for issue in result.issues)
    assert all(issue.file == "design/blk/rtl/broken.sv" for issue in result.issues)


def test_unknown_rules_ref_is_an_error_status(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "design/blk/rtl/x.sv", "module m_qnsc_x; endmodule\n")
    result = _run(
        _spec(rules="org:qnsc/does-not-exist.yml", scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    assert result.status == "error"
    assert "does-not-exist" in result.issues[0].msg


def test_bad_rules_file_is_an_error_status(tmp_path: Path, runner: FakeRunner) -> None:
    write(tmp_path, "rules/bad.yml", "document: X\n")  # missing required fields
    write(tmp_path, "design/blk/rtl/x.sv", "module m_qnsc_x; endmodule\n")
    result = _run(
        _spec(rules="rules/bad.yml", scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    assert result.status == "error"


def test_missing_rules_arg_is_an_error(tmp_path: Path, runner: FakeRunner) -> None:
    result = _run(_spec(scope=["design/**/*.sv"]), make_ctx(tmp_path, runner))
    assert result.status == "error"


def test_idempotency_key_changes_with_the_rules_and_files(
    tmp_path: Path, runner: FakeRunner
) -> None:
    write(tmp_path, "design/blk/rtl/x.sv", "module m_qnsc_x; endmodule\n")
    ctx = make_ctx_block(tmp_path, runner, "blk")
    spec = _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"])
    first = _run(spec, ctx)
    write(tmp_path, "design/blk/rtl/x.sv", "module m_qnsc_x; // changed\nendmodule\n")
    second = _run(spec, ctx)
    assert first.idempotency_key != second.idempotency_key


# --------------------------------------------------------------------------------------
# rules data, registration and schema
# --------------------------------------------------------------------------------------


def test_builtin_qnsc_rule_resolves_and_validates() -> None:
    path = resolve_data_ref(_RULES, Path.cwd())
    assert path.is_file()
    rules = NamingRules.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert rules.document == "QNSC_RTL_Design_Naming_Rule"
    assert rules.version == "1.1"
    assert "module" in rules.identifiers
    assert rules.vocabulary is not None


def test_qnsc_org_profile_extends_resolves() -> None:
    # The org profile points the naming check at the built-in rule.
    from chipgraph.core.config.loader import builtin_data_dir

    data_dir = builtin_data_dir()
    assert data_dir is not None
    profile = data_dir / "orgs" / "qnsc" / "profile.yml"
    assert profile.is_file()
    doc = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert doc["naming"]["rules"] == _RULES


def test_check_is_registered_via_entry_point() -> None:
    registry = Registry()
    registry.discover()
    assert "naming" in registry.names("check")
    assert registry.get("check", "naming").id == "naming"


def test_committed_schema_matches_generated() -> None:
    committed = Path(__file__).parents[2] / "schemas" / "formats" / "naming-rules.schema.json"
    assert committed.read_text(encoding="utf-8") == schema_json()


# --------------------------------------------------------------------------------------
# helper
# --------------------------------------------------------------------------------------


def make_ctx_block(root: Path, runner: FakeRunner, block: str) -> ToolContext:
    return ToolContext(repo_root=root, runner=runner, params={"block": block})


def test_parse_error_does_not_hide_other_violations(tmp_path: Path, runner: FakeRunner) -> None:
    write(
        tmp_path,
        "design/blk/rtl/partly.sv",
        "module BadName (input logic i_a);\n  logic sig;\n  always_comb begin if end\nendmodule\n",
    )
    result = _run(
        _spec(rules=_RULES, scope=["design/{block}/rtl/**/*.sv"]),
        make_ctx_block(tmp_path, runner, "blk"),
    )
    rules = {issue.rule for issue in result.issues}
    assert "parse" in rules
    assert rules - {"parse"}, "names recovered around a parse error must still be checked"
