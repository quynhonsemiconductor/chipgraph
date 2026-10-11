"""M2-04: the `style` check: a style profile derived from exemplars, each deviation rule,
`style.enforce`, the distance, and the check's run (files, exemplars, skipped, errors).

The two exemplar sets are the acceptance script's (`docs/rtl-claude-code/exemplars/`).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
import yaml

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.checks.style import (
    StyleCheck,
    deviations,
    observe,
    style_distance,
    style_issues,
    style_profile,
)
from chipgraph.core.config.models import STYLE_KEYS
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api.types import ToolContext

REPO = Path(__file__).resolve().parents[2]
SETS = REPO / "docs" / "rtl-claude-code" / "exemplars"
TINYSOC = REPO / "examples" / "tinysoc"


def _set(name: str) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((SETS / name).glob("*.sv"))}


A = style_profile(_set("a"))
B = style_profile(_set("b"))

GOOD_A = """\
// A module in the style of set A.

module ex_a_hold (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [3:0] value,
  output logic [3:0] held
);

  logic [3:0] held_d;
  logic [3:0] held_q;

  always_comb begin
    held_d = value;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      held_q <= '0;
    end else begin
      held_q <= held_d;
    end
  end

  assign held = held_q;

endmodule
"""


def _double_indent(text: str) -> str:
    return re.sub(r"(?m)^( +)", lambda m: m.group(1) * 2, text)


def _keys(text: str, profile: object = A) -> dict[str, list[int]]:
    found, _ = deviations(profile, observe(text, "x.sv"))  # type: ignore[arg-type]
    out: dict[str, list[int]] = {}
    for dev in found:
        out.setdefault(dev.key, []).append(dev.line)
    return out


# --- the profile -------------------------------------------------------------------------------


def test_the_profile_of_set_a() -> None:
    assert (A.indent_char, A.indent_width, A.begin) == ("space", 2, "same_line")
    assert A.ports is not None and (A.ports.ansi, A.ports.one_per_line, A.ports.aligned) == (
        True,
        True,
        True,
    )
    assert A.reset is not None and (A.reset.kind, A.reset.active, A.reset.name) == (
        "async",
        "low",
        "rst_n",
    )
    assert A.sequential is not None
    assert A.sequential.template == "always_ff @(posedge clk or negedge rst_n)"
    assert (A.reg_suffix, A.next_suffix, A.port_suffixes) == ("_q", "_d", False)
    assert (A.header, A.max_blank_lines, A.local_case) == (True, 1, "snake")
    assert A.suffix_freq["_q"] > 0 and A.suffix_freq["_r"] == 0


def test_the_profile_of_set_b() -> None:
    assert (B.indent_char, B.indent_width, B.begin) == ("space", 4, "next_line")
    assert B.ports is not None and B.ports.aligned is False
    assert B.reset is not None and (B.reset.kind, B.reset.active, B.reset.name) == (
        "sync",
        "low",
        "rst_n",
    )
    assert B.sequential is not None and B.sequential.template == "always_ff @(posedge clk)"
    assert (B.reg_suffix, B.next_suffix) == ("_r", "_next")
    assert (B.header, B.max_blank_lines) == (False, 2)
    assert B.max_line_length == 96 and A.max_line_length == 86


def test_the_profile_of_the_real_tinysoc_rtl() -> None:
    files = {
        f"rtl/{n}": (TINYSOC / "rtl" / n).read_text() for n in ("tiny_timer.sv", "tiny_gpio.sv")
    }
    t = style_profile(files)
    assert (t.indent_char, t.indent_width, t.begin, t.header) == ("space", 2, "same_line", True)
    assert t.reset is not None and (t.reset.kind, t.reset.active, t.reset.name) == (
        "async",
        "low",
        "rst_n",
    )
    assert (t.reg_suffix, t.next_suffix, t.local_case) == ("_q", None, "snake")
    assert t.ports is not None and t.ports.aligned is True
    # the third tinysoc file follows the first two
    assert style_distance(t, (TINYSOC / "rtl" / "tiny_top.sv").read_text()) == 0.0


def test_the_profile_is_deterministic_and_describes_itself() -> None:
    assert style_profile(_set("a")) == style_profile(dict(reversed(list(_set("a").items()))))
    text = A.describe()
    assert set(text) | {"reset_kind"} == set(STYLE_KEYS)  # reset_kind is part of `reset`
    assert "2 spaces" in text["indent"] and "rst_n" in text["reset"] and "_q" in text["suffix"]


# --- each deviation rule ---------------------------------------------------------------------


def test_a_file_in_the_style_has_no_deviation() -> None:
    assert _keys(GOOD_A) == {}
    assert style_distance(A, GOOD_A) == 0.0
    assert style_distance(B, GOOD_A) > 0.3


def test_indent() -> None:
    assert "indent" in _keys(_double_indent(GOOD_A))
    tabs = GOOD_A.replace("\n  ", "\n\t")
    assert "indent" in _keys(tabs)


def test_begin_placement() -> None:
    text = GOOD_A.replace("always_comb begin", "always_comb\n  begin")
    keys = _keys(text)
    assert keys["begin"] == [14]


def test_ports() -> None:
    text = GOOD_A.replace("input  logic       clk", "input logic clk")
    assert "ports" in _keys(text)
    two = GOOD_A.replace(
        "  input  logic       clk,\n  input  logic       rst_n,",
        "  input  logic clk, input logic rst_n,",
    )
    assert "ports" in _keys(two)


def test_reset_and_sequential() -> None:
    sync = GOOD_A.replace("always_ff @(posedge clk or negedge rst_n)", "always_ff @(posedge clk)")
    assert "reset_kind" in _keys(sync) and "sequential" not in _keys(sync)
    high = GOOD_A.replace("negedge rst_n", "posedge rst_n")
    assert "reset" in _keys(high)
    plain = GOOD_A.replace("always_ff @(", "always @(")
    assert "sequential" in _keys(plain)


def test_suffix() -> None:
    text = GOOD_A.replace("held_q", "held_r")
    keys = _keys(text)
    assert "suffix" in keys
    nxt = GOOD_A.replace("held_d", "held_next")
    assert "suffix" in _keys(nxt)


def test_header_blank_lines_line_length_case() -> None:
    no_header = GOOD_A.split("\n", 2)[2]
    assert _keys(no_header) == {"header": [1]}
    blanks = GOOD_A.replace("  assign held", "\n\n  assign held")
    assert "blank_lines" in _keys(blanks)
    long = GOOD_A.replace("// A module in the style of set A.", "// " + "x" * 120)
    assert "line_length" in _keys(long)
    camel = GOOD_A.replace("held_d", "heldNext")
    assert "case" in _keys(camel)


def test_a_header_after_a_timescale_is_a_header() -> None:
    text = "`timescale 1ns/1ps\n\n" + GOOD_A
    assert observe(text).header is True
    assert observe(GOOD_A.split("\n", 2)[2]).header is False


def test_issues_are_warnings_unless_enforced() -> None:
    obs = observe(GOOD_A.replace("held_q", "held_r"), "rtl/x.sv")
    loose = style_issues(A, obs)
    assert loose and {i.severity for i in loose} == {"warning"}
    assert all(i.rule.startswith("style.") and i.file == "rtl/x.sv" and i.line for i in loose)
    assert any("exemplars: registers end in _q" in i.msg for i in loose)
    strict = style_issues(A, obs, enforce=["suffix"])
    assert {i.severity for i in strict if i.rule == "style.suffix"} == {"error"}


# --- the check -------------------------------------------------------------------------------


def _project(tmp_path: Path, style: dict[str, object] | None) -> Path:
    root = tmp_path / "p"
    root.mkdir(parents=True)
    for name, text in _set("a").items():
        (root / "style" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "style" / name).write_text(text)
    profile: dict[str, object] = {"project": "p", "adapters": {"style": {"use": "style"}}}
    if style is not None:
        profile["style"] = style
    (root / ".chipgraph.yml").write_text(yaml.safe_dump(profile))
    (root / "rtl").mkdir()
    return root


def _run(root: Path, args: dict[str, object], params: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    spec = CheckSpec(id="style", capability="style", adapter="style", args=args)
    ctx = ToolContext(repo_root=root, runner=LocalRunner(), params=params or {})
    return asyncio.run(StyleCheck().run(spec, ctx))


EXEMPLARS = ["style/ex_a_counter.sv", "style/ex_a_flag.sv"]


def test_the_check_passes_with_warnings_and_fails_on_an_enforced_key(tmp_path: Path) -> None:
    root = _project(tmp_path, {"exemplars": EXEMPLARS})
    (root / "rtl" / "x.sv").write_text(GOOD_A.replace("held_q", "held_r"))
    result = _run(root, {"files": ["rtl/{module}.sv"]}, {"module": "x"})
    assert result.status == "pass"
    assert any(i.rule == "style.suffix" and i.severity == "warning" for i in result.issues)
    [dist] = [i for i in result.issues if i.rule == "style.distance"]
    assert dist.severity == "info" and "style distance" in dist.msg
    strict = _project(tmp_path / "s", {"exemplars": EXEMPLARS, "enforce": ["suffix"]})
    (strict / "rtl" / "x.sv").write_text(GOOD_A.replace("held_q", "held_r"))
    failed = _run(strict, {"files": ["rtl/x.sv"]})
    assert failed.status == "fail"
    assert [i.line for i in failed.issues if i.severity == "error"]


def test_args_override_the_profile(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "rtl" / "x.sv").write_text(GOOD_A)
    assert _run(root, {"files": ["rtl/x.sv"]}).status == "skipped"  # no exemplars
    result = _run(
        root, {"files": ["rtl/x.sv"], "exemplars": EXEMPLARS, "enforce": list(STYLE_KEYS)}
    )
    assert result.status == "pass"


def test_skipped_without_files_and_errors(tmp_path: Path) -> None:
    root = _project(tmp_path, {"exemplars": EXEMPLARS})
    skipped = _run(root, {})
    assert skipped.status == "skipped" and skipped.issues[0].rule == "style.no_files"
    missing = _project(tmp_path / "m", {"exemplars": ["style/none.sv"]})
    (missing / "rtl" / "x.sv").write_text(GOOD_A)
    error = _run(missing, {"files": ["rtl/x.sv"]})
    assert error.status == "error" and "style/none.sv" in error.issues[0].msg
    bad = _run(root, {"files": ["rtl/x.sv"], "enforce": ["indentation"]})
    assert bad.status == "error" and "indentation" in bad.issues[0].msg


def test_a_checked_exemplar_is_left_out_of_its_own_profile(tmp_path: Path) -> None:
    root = _project(tmp_path, {"exemplars": [*EXEMPLARS, "rtl/x.sv"], "enforce": ["indent"]})
    (root / "rtl" / "x.sv").write_text(_double_indent(GOOD_A))
    result = _run(root, {"files": ["rtl/x.sv"]})
    assert result.status == "fail"  # measured against the other two, not itself


@pytest.mark.parametrize("name", ["a", "b"])
def test_each_exemplar_is_close_to_its_own_set(name: str) -> None:
    own = A if name == "a" else B
    other = B if name == "a" else A
    for text in _set(name).values():
        assert style_distance(own, text) < style_distance(other, text)


# --- reset_kind: a different kind of reset is always an error (r2-01) ----------------------


def _reset_kind_issues(exemplars: dict[str, str], text: str, enforce: tuple[str, ...] = ()) -> list:
    profile = style_profile(exemplars)
    return [i for i in style_issues(profile, observe(text, "rtl/m.sv"), enforce=enforce)]


def test_a_synchronous_reset_against_asynchronous_exemplars_is_an_error_without_enforce() -> None:
    sync = GOOD_A.replace("always_ff @(posedge clk or negedge rst_n)", "always_ff @(posedge clk)")
    issues = _reset_kind_issues(_set("a"), sync)
    [kind] = [i for i in issues if i.rule == "style.reset_kind"]
    assert kind.severity == "error" and kind.file == "rtl/m.sv" and kind.line
    assert "synchronous" in kind.msg and "asynchronous" in kind.msg
    # the other reset deviations stay warnings unless enforced
    assert all(i.severity == "warning" for i in issues if i.rule != "style.reset_kind")


def test_an_asynchronous_reset_against_synchronous_exemplars_is_an_error() -> None:
    sync_profile = style_profile(_set("b"))
    asynchronous = GOOD_A  # set A's own style: async reset
    issues = style_issues(sync_profile, observe(asynchronous, "rtl/m.sv"))
    assert [i.severity for i in issues if i.rule == "style.reset_kind"] == ["error"]


def test_the_same_kind_of_reset_is_not_reset_kind_even_if_the_polarity_differs() -> None:
    high = GOOD_A.replace("negedge rst_n", "posedge rst_n")
    issues = _reset_kind_issues(_set("a"), high)
    assert not [i for i in issues if i.rule == "style.reset_kind"]
    assert [i.severity for i in issues if i.rule == "style.reset"] == ["warning"]


def test_the_distance_still_counts_a_reset_kind_deviation() -> None:
    sync = GOOD_A.replace("always_ff @(posedge clk or negedge rst_n)", "always_ff @(posedge clk)")
    assert style_distance(A, observe(sync)) > style_distance(A, observe(GOOD_A)) == 0.0


def test_reset_kind_is_a_valid_enforce_key() -> None:
    assert "reset_kind" in STYLE_KEYS


def test_the_r2_01_defect_is_caught_by_the_style_check_not_by_a_reviewer() -> None:
    """The Critic missed `r2-01` (a timer whose async reset became synchronous). With the
    project's own RTL as the exemplars, the deterministic check flags it with a line."""
    exemplars = {p.name: p.read_text() for p in sorted((TINYSOC / "rtl").glob("tiny_*.sv"))}
    timer = (TINYSOC / "rtl" / "tiny_timer.sv").read_text()
    old = "  always_ff @(posedge clk or negedge rst_n) begin\n"
    assert old in timer
    broken = timer.replace(old, "  always_ff @(posedge clk) begin\n", 1)
    others = {k: v for k, v in exemplars.items() if k != "tiny_timer.sv"}
    assert not [
        i
        for i in style_issues(style_profile(others), observe(timer, "rtl/tiny_timer.sv"))
        if i.rule == "style.reset_kind"
    ]
    [issue] = [
        i
        for i in style_issues(style_profile(others), observe(broken, "rtl/tiny_timer.sv"))
        if i.rule == "style.reset_kind"
    ]
    assert issue.severity == "error" and issue.line == 26
