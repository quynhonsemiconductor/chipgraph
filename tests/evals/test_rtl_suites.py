"""M2-13: the `rtl` and `tb` suites end to end.

- `fake`: every problem through the real `next_task`/`get_context`/`submit` (the rule's
  checks, labels and retries) with the scripted authors, graded against the oracle: the
  default scripts pass (one problem at once, one after a repair); overrides make it fail
  on purpose (accepted but wrong, rejected until the budget is gone, an unsound test);
- `claude-code`, against a scripted stand-in for `claude` (no model): the command each
  problem gets, its eval project (tiers, runtime, no testbench or no RTL), the budget, the
  stream's cost and tokens per model, and the forbidden-call check;
- the forbidden-call logic on its own, and that neither suite is in the weekly workflow
  or in `evals/run-claude-code.sh`'s defaults.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from rtl_eval_helpers import REPO, harness, needs_sim, needs_verilator, rtl

evals = harness()
authors = evals.authors


def _summary(out: Path) -> dict:
    return json.loads((out / "summary.json").read_text())


# --- fake -----------------------------------------------------------------------------


@needs_verilator
def test_fake_rtl_suite_passes_every_problem(tmp_path: Path) -> None:
    out = tmp_path / "rtl"
    result = evals.run_suite("rtl", out)
    summary = _summary(out)
    assert result.status == "pass", summary
    metrics = summary["metrics"]
    assert metrics["problems"] == 2
    assert (metrics["pass_at_1"], metrics["pass_at_k"]) == (0.5, 1.0)
    assert metrics["repair_turns_mean"] == 1.5
    assert metrics["first_rejections"] == {"syscsr_dbg_en": "constraint"}
    items = {i["id"]: i for i in summary["items"]}
    assert items["rom_wr_resp"]["diff"]["status"] == "match"
    assert items["syscsr_dbg_en"]["attempts"][0]["failed_checks"][0] == "lint"
    assert metrics["estimate"]["usd"] > 0
    assert summary["inspect"]["accuracy"] == 1.0
    md = (out / "summary.md").read_text()
    assert "| rom_wr_resp | yes | yes | 1/3 |" in md
    assert "| syscsr_dbg_en | no | yes | 2/3 | constraint |" in md
    answers = [json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines()]
    assert {a["id"] for a in answers} == {"rom_wr_resp", "syscsr_dbg_en"}
    assert all("module" in a["text"] for a in answers)


@needs_verilator
def test_fake_rtl_suite_fails_on_wrong_or_rejected_rtl(tmp_path: Path) -> None:
    options = evals.EvalOptions(
        fake_overrides={"rom_wr_resp": ["broken"], "syscsr_dbg_en": ["syntax_error"]}
    )
    out = tmp_path / "rtl"
    result = evals.run_suite("rtl", out, options)
    summary = _summary(out)
    assert result.status == "fail"
    items = {i["id"]: i for i in summary["items"]}
    rom = items["rom_wr_resp"]
    assert rom["accepted"] and not rom["matches_oracle"] and not rom["pass_at_1"]
    assert rom["diff"]["status"] == "mismatch"
    assert "differs from the oracle" in rom["note"]
    syscsr = items["syscsr_dbg_en"]
    assert not syscsr["accepted"] and syscsr["status"] == "budget_exhausted"
    assert syscsr["first_rejection"] == "constraint"
    assert syscsr["diff"]["status"] == "compile_error"
    assert summary["metrics"]["pass_at_k"] == 0.0


@needs_sim
def test_fake_tb_suite(tmp_path: Path) -> None:
    out = tmp_path / "tb"
    assert evals.run_suite("tb", out).status == "pass", _summary(out)
    metrics = _summary(out)["metrics"]
    assert (metrics["pass_at_1"], metrics["pass_at_k"]) == (0.5, 1.0)
    assert metrics["mutation_score_mean"] == 1.0 and metrics["soundness_mean"] == 1.0
    assert _summary(out)["inspect"]["accuracy"] == 1.0  # the scorer graded in its event loop
    assert metrics["first_rejections"] == {"rom_wr_resp": "verification"}

    bad = tmp_path / "tb-bad"
    options = evals.EvalOptions(
        only=("syscsr_dbg_en",), fake_overrides={"syscsr_dbg_en": ["broken"]}
    )
    assert evals.run_suite("tb", bad, options).status == "fail"
    [item] = _summary(bad)["items"]
    assert item["accepted"] and not item["matches_oracle"]
    assert item["tb"]["failing_on_oracle"] == ["test_follows_pin"]


def test_the_projects_keep_the_authors_apart(tmp_path: Path) -> None:
    project = rtl("project")
    problem = rtl("problems").load_problems().problem("syscsr_dbg_en")
    tb = project.tb_project(problem, tmp_path / "tb")
    files = {p.relative_to(tb).as_posix() for p in tb.rglob("*") if ".git" not in p.parts}
    assert not any(f.endswith(".sv") for f in files), "no RTL in the testbench project"
    from chipgraph.adapters.runtime.claude_code.guarded import _load_model
    from chipgraph.app.context import AppContext

    model = _load_model(AppContext.load(tb))
    ports = sorted(k for k in model.entities if k.startswith("port:spec.syscsr."))
    assert ports == sorted(f"port:spec.syscsr.{p.name}" for p in problem.interface)


# --- claude-code, against a stand-in -------------------------------------------------

FAKE_CLAUDE = r"""#!{python}
import json, os, sys

args = sys.argv[1:]
if args == ["--help"]:
    print("  --max-budget-usd <amount>  Maximum dollar amount")
    sys.exit(0)
if args == ["--version"]:
    print("0.0.0 (fake Claude Code)")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "files": sorted(
        os.path.join(d, n)[2:] for d, _, ns in os.walk(".") if ".git" not in d for n in ns),
        "profile": open(".chipgraph.yml").read()}) + "\n")
texts = json.loads(os.environ["FAKE_CLAUDE_TEXTS"])
target = prompt.split(" ", 1)[1]
output, text = texts[target]


def emit(event):
    print(json.dumps(event), flush=True)


def call(tid, name, inp, result, parent=None):
    emit({"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
    emit({"type": "user", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(result)}]}]}})


tool = "mcp__plugin_chipgraph_chipgraph__"
agent = "chipgraph:rtl-author" if target.startswith("digital-rtl/") else "chipgraph:tb-author"
emit({"type": "system", "subtype": "init"})
call("n1", tool + "next_task", {"target": target}, {"tasks": [{"task_id": target}]})
call("s0", "ToolSearch", {"query": "select:" + tool + "submit"}, {})
call("a1", "Agent", {"subagent_type": agent, "model": "sonnet"}, {})
call("g1", tool + "get_context", {"task_id": target}, {"task_id": target}, parent="a1")
path = os.path.join(os.getcwd(), output)
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w").write(text)
call("w1", "Write", {"file_path": path, "content": text}, {}, parent="a1")
if os.environ.get("FAKE_CLAUDE_FOREIGN"):
    call("x1", "Bash", {"command": "cat design/*"}, {}, parent="a1")
    call("x2", "Write", {"file_path": os.path.join(os.getcwd(), "notes.txt")}, {}, parent="a1")
call("u1", tool + "submit", {"task_id": target}, {
    "accepted": False, "status": "rejected", "label": "constraint", "counted": True,
    "failed_checks": ["lint"], "tries": 3, "checks": []})
call("u2", tool + "submit", {"task_id": target}, {
    "accepted": True, "status": "accepted", "label": None, "tries": 3,
    "checks": [{"check_id": "lint", "status": "pass", "issues": []}]})
cost = float(os.environ.get("FAKE_CLAUDE_COST", "0.5"))
emit({"type": "result", "subtype": "success", "num_turns": 9, "total_cost_usd": cost,
      "modelUsage": {"claude-sonnet-x": {"inputTokens": 1000, "outputTokens": 200,
      "cacheReadInputTokens": 5000, "costUSD": cost}}})
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fixtures = rtl("fixtures")
    problems = rtl("problems").load_problems()
    texts = {}
    for p in problems.problems:
        texts[p.rtl_target] = [p.rtl_output, fixtures.strip_header(p.fixture(p.files.oracle))]
        texts[p.tb_target] = [p.tb_output, p.reference_path.read_text()]
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    script.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CHIPGRAPH_EVAL_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(calls))
    monkeypatch.setenv("FAKE_CLAUDE_TEXTS", json.dumps(texts))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-for-tests")
    return calls


def _calls(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _opt(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@needs_verilator
def test_claude_code_rtl_one_session_per_problem(tmp_path: Path, fake_claude: Path) -> None:
    out = tmp_path / "out"
    options = evals.EvalOptions(runtime="claude-code")
    result = evals.run_suite("rtl", out, options)
    summary = _summary(out)
    calls = _calls(fake_claude)
    assert len(calls) == 2
    argv = calls[0]["argv"]
    assert (
        _opt(argv, "-p")
        == "/chipgraph:run digital-rtl/rtl_module[block=rom,module=m_qnsc_rom_wr_resp]"
    )
    assert _opt(argv, "--model") == "sonnet"
    assert _opt(argv, "--permission-mode") == "acceptEdits"
    assert set(_opt(argv, "--allowedTools").split()) == set(authors.AUTHOR_TOOLS)
    assert "Bash" in _opt(argv, "--disallowedTools").split()
    assert float(_opt(argv, "--max-budget-usd")) == 10.0
    profile = calls[0]["profile"]
    assert "runtime: claude-code" in profile and "medium: sonnet" in profile
    assert "large: opus" in profile
    assert "plan/rom.approved.json" in calls[0]["files"]
    assert not any(f.startswith("dv/") for f in calls[0]["files"])
    assert not any(f.endswith("m_qnsc_rom_wr_resp.sv") for f in calls[0]["files"])

    assert result.status == "pass", summary
    assert summary["cost_usd"] == {"budget": 10.0, "total": 1.0}
    assert summary["models"]["main"] == "sonnet"
    metrics = summary["metrics"]
    assert (metrics["pass_at_1"], metrics["pass_at_k"]) == (0.0, 1.0)
    assert metrics["repair_turns_mean"] == 2.0
    assert metrics["usage_by_model"]["claude-sonnet-x"]["output"] == 400.0
    assert metrics["tokens"] == 2 * 6200
    assert summary["foreign_tool_calls"] == []
    assert (out / "streams" / "rom_wr_resp.jsonl").is_file()


@needs_sim
def test_claude_code_tb_project_has_no_rtl(tmp_path: Path, fake_claude: Path) -> None:
    out = tmp_path / "out"
    options = evals.EvalOptions(runtime="claude-code", only=("syscsr_dbg_en",))
    assert evals.run_suite("tb", out, options).status == "pass", _summary(out)
    [call] = _calls(fake_claude)
    assert _opt(call["argv"], "-p") == "/chipgraph:run dv/tb_module[block=syscsr]"
    assert not any(f.endswith(".sv") for f in call["files"])


@needs_verilator
def test_claude_code_forbidden_calls_and_budget(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FOREIGN", "1")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "6")
    out = tmp_path / "out"
    options = evals.EvalOptions(runtime="claude-code", budget_usd=5.0)
    result = evals.run_suite("rtl", out, options)
    summary = _summary(out)
    assert result.status == "fail"
    foreign = summary["foreign_tool_calls"]
    assert "rom_wr_resp:Bash" in foreign
    assert any(f.startswith("rom_wr_resp:Write(") and f.endswith("notes.txt)") for f in foreign)
    assert summary["samples"]["not_run"] == {"budget": ["syscsr_dbg_en"]}
    assert len(_calls(fake_claude)) == 1


# --- forbidden calls, on their own ------------------------------------------------------


def _call(name: str, parent: str | None = None, **inp: object) -> dict:
    return {"name": name, "parent": parent, "input": inp, "result": None}


def test_foreign_calls(tmp_path: Path) -> None:
    tool = authors.TOOL
    out = "design/rom/rtl/m.sv"
    ok = [
        _call(f"{tool}next_task"),
        _call("ToolSearch", query=f"select:{tool}next_task,{tool}submit"),
        _call("Agent", subagent_type="chipgraph:rtl-author"),
        _call(f"{tool}get_context", "a1"),
        _call("ToolSearch", "a1", query=f"select:{tool}get_context"),
        _call("Write", "a1", file_path=str(tmp_path / out)),
        _call("Edit", "a1", file_path=out),
        _call(f"{tool}submit"),
    ]
    assert authors.foreign_calls("rtl", "s", ok, tmp_path, out) == []
    bad = [
        _call(f"{tool}get_context"),  # the main session never calls it
        _call("Read", "a1", file_path="dv/x.py"),
        _call("Write", "a1", file_path=str(tmp_path / "dv/rom/test_rom.py")),
        _call("ToolSearch", "a1", query="select:Bash"),
        _call("ToolSearch", query="testbench"),
        _call("Agent", subagent_type="chipgraph:tb-author"),
        _call("Write"),
    ]
    found = authors.foreign_calls("rtl", "s", bad, tmp_path, out)
    assert found == [
        f"s:{tool}get_context",
        "s:Read",
        f"s:Write({tmp_path / 'dv/rom/test_rom.py'})",
        "s:ToolSearch(select:Bash)",
        "s:ToolSearch(testbench)",
        "s:Write",
        "s:Agent(chipgraph:tb-author)",
    ]
    assert (
        authors.foreign_calls(
            "tb", "s", [_call("Agent", subagent_type="chipgraph:tb-author")], tmp_path, out
        )
        == []
    )


def test_not_in_the_weekly_workflow_or_the_default_script() -> None:
    import re

    workflow = (REPO / ".github" / "workflows" / "evals.yml").read_text()
    script = (REPO / "evals" / "run-claude-code.sh").read_text()
    lists = re.findall(r'suites="([^"]*)"', workflow) + re.findall(
        r'SUITES="\$\{SUITES:-([^}]*)\}"', script
    )
    assert lists, "no suite list found"
    for names in lists:
        assert not {"rtl", "tb"} & set(names.split())
    assert evals.SUITES["rtl"].budget_usd == evals.SUITES["tb"].budget_usd == 10.0
