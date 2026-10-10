"""M2-09: the Critic review evals: the planted-defect data is true to tinysoc, the grader
grades correctly, and `chipgraph eval review` runs end to end with the fake runtime (the
real MCP service, a scripted Critic) and with a scripted stand-in for `claude`. No model.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "evals" / "harness"
TINYSOC = REPO / "examples" / "tinysoc"
SKILL = REPO / "src" / "chipgraph" / "packs" / "digital_rtl" / "skills" / "review-diff.md"


def _load_harness() -> ModuleType:
    if "chipgraph_evals" in sys.modules:
        return sys.modules["chipgraph_evals"]
    spec = importlib.util.spec_from_file_location(
        "chipgraph_evals", HARNESS / "__init__.py", submodule_search_locations=[str(HARNESS)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chipgraph_evals"] = module
    spec.loader.exec_module(module)
    return module


evals = _load_harness()
grade = evals.suites.load_grader("review")
SAMPLES = grade.load_samples()
BY_ID = {s.id: s for s in SAMPLES}


# --- the data ---------------------------------------------------------------------------


def test_the_set_has_enough_defects_classes_and_clean_diffs() -> None:
    defects = [s for s in SAMPLES if not s.clean]
    assert len(defects) >= 12
    assert len({s.cls for s in defects}) >= 6
    assert len([s for s in SAMPLES if s.clean]) == 4


def test_every_class_is_a_category_of_the_review_skill() -> None:
    categories = set(re.findall(r"`([a-z_]+)`", SKILL.read_text()))
    for sample in SAMPLES:
        for category in (c for c in sample.categories if c):
            assert category in categories, (sample.id, category)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s.id)
def test_each_sample_applies_to_tinysoc_and_points_at_its_line(sample: Any, tmp_path: Path) -> None:
    project = tmp_path / "tinysoc"
    shutil.copytree(TINYSOC, project)
    before = {e.file: (project / e.file).read_text() for e in sample.edits}
    grade.apply_edits(sample, project)
    for edit in sample.edits:
        assert (project / edit.file).read_text() != before[edit.file]
        assert edit.file.startswith("rtl/")
    if sample.clean:
        return
    lines = (project / sample.at_file).read_text().splitlines()
    assert sample.at_has in lines[sample.at_line - 1], lines[sample.at_line - 1]
    path, _, line = sample.spec.rpartition(":")
    spec_line = (TINYSOC / path).read_text().splitlines()[int(line) - 1]
    assert sample.spec_has in spec_line, spec_line
    if sample.req is not None:
        specs = "".join(p.read_text() for p in (TINYSOC / "doc" / "specs").glob("*.md"))
        assert f"`{sample.req}`" in specs
    # The planted line is in the changed file of the sample's own block.
    assert sample.at_file in {e.file for e in sample.edits}
    assert sample.at_file == f"rtl/tiny_{sample.block}.sv"


def test_a_find_that_is_not_unique_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "tinysoc"
    shutil.copytree(TINYSOC, project)
    edit = grade.Edit("rtl/tiny_timer.sv", "count_q", "x")
    sample = grade.Sample(id="x", block="timer", edits=(edit,), clean=True)
    with pytest.raises(grade.GradeError, match="occurs"):
        grade.apply_edits(sample, project)


# --- the grader -------------------------------------------------------------------------


def _comment(sample: Any, **over: Any) -> dict[str, Any]:
    comment = {
        "id": "R1",
        "severity": "major",
        "category": sample.cls,
        "file": sample.at_file,
        "line": sample.at_line,
        "req_id": None,
    }
    return {**comment, **over}


def _answer(sid: str, *comments: dict[str, Any]) -> dict[str, Any]:
    return {"id": sid, "review": {"comments": list(comments), "verdict": "approve"}}


def test_caught_by_category_or_by_req_within_three_lines() -> None:
    d02 = BY_ID["d-02"]
    assert grade.grade_one(d02, _answer("d-02", _comment(d02))).caught
    assert grade.grade_one(d02, _answer("d-02", _comment(d02, category="spec_mismatch"))).caught
    by_req = _comment(d02, category="other", req_id="REQ-TIM-003", line=d02.at_line + 3)
    assert grade.grade_one(d02, _answer("d-02", by_req)).caught
    far = _comment(d02, line=d02.at_line - 4)
    assert not grade.grade_one(d02, _answer("d-02", far)).caught
    wrong_kind = _comment(d02, category="naming")
    assert not grade.grade_one(d02, _answer("d-02", wrong_kind)).caught
    wrong_file = _comment(d02, file="rtl/tiny_gpio.sv")
    assert not grade.grade_one(d02, _answer("d-02", wrong_file)).caught
    # A minor comment on the defect still catches it.
    assert grade.grade_one(d02, _answer("d-02", _comment(d02, severity="minor"))).caught
    missed = grade.grade_one(d02, {"id": "d-02", "review": None})
    assert (missed.caught, missed.passed, missed.note) == (False, False, "no review")


def test_clean_false_alarms_are_blocker_or_major_only() -> None:
    c01 = BY_ID["c-01"]
    major = {"id": "R1", "severity": "major", "category": "other", "file": "x", "line": 1}
    assert grade.grade_one(c01, _answer("c-01", major)).false_alarm
    assert grade.grade_one(c01, _answer("c-01", {**major, "severity": "blocker"})).false_alarm
    assert not grade.grade_one(c01, _answer("c-01", {**major, "severity": "nit"})).false_alarm
    assert grade.grade_one(c01, _answer("c-01")).passed
    unreviewed = grade.grade_one(c01, None)
    assert (unreviewed.passed, unreviewed.false_alarm) == (False, False)


def _all_caught() -> dict[str, dict[str, Any]]:
    return {s.id: _answer(s.id) if s.clean else _answer(s.id, _comment(s)) for s in SAMPLES}


def test_thresholds_recall_and_false_alarms() -> None:
    answers = _all_caught()
    report = grade.grade(SAMPLES, answers)
    assert (report.caught, report.recall, report.false_alarms) == (16, 1.0, 0)
    assert report.precision_proxy == 1.0 and report.passed
    defects = [s.id for s in SAMPLES if not s.clean]
    for sid in defects[:4]:  # 12 of 16 = 75 %: still passes
        answers[sid] = _answer(sid)
    assert grade.grade(SAMPLES, answers).passed
    answers[defects[4]] = _answer(defects[4])  # 11 of 16 < 70 %
    report = grade.grade(SAMPLES, answers)
    assert report.caught == 11 and not report.passed
    alarm = {"id": "R9", "severity": "major", "category": "other", "file": "x", "line": 1}
    answers = _all_caught()
    answers["c-01"] = _answer("c-01", alarm)
    assert grade.grade(SAMPLES, answers).passed  # one false alarm is allowed
    answers["c-02"] = _answer("c-02", alarm)
    report = grade.grade(SAMPLES, answers)
    assert report.false_alarms == 2 and not report.passed
    assert report.precision_proxy == pytest.approx(16 / 18)
    assert report.to_json()["by_class"]["reset"] == {"total": 2, "caught": 2}


def test_grade_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "answers.jsonl"
    path.write_text("".join(json.dumps(a) + "\n" for a in _all_caught().values()))
    assert grade.main([str(path)]) == 0
    assert "recall 16/16" in capsys.readouterr().out
    path.write_text(json.dumps({"id": "d-01", "review": None}) + "\n")
    assert grade.main([str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["caught"] == 0
    path.write_text("not json\n")
    assert grade.main([str(path)]) == 2


# --- chipgraph eval review: fake runtime ------------------------------------------------


def test_fake_review_suite_passes_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "review"
    result = evals.run_suite("review", out)
    assert (result.status, result.exit_code) == ("pass", 0)
    summary = json.loads((out / "summary.json").read_text())
    metrics = summary["metrics"]
    assert (metrics["caught"], metrics["defects"], metrics["recall"]) == (16, 16, 1.0)
    assert (metrics["false_alarms"], metrics["clean"]) == (0, 4)
    assert summary["samples"] == {"total": 20, "run": 20, "not_run": {}}
    assert "planted defects caught: 16/16" in (out / "summary.md").read_text()
    answers = [json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines()]
    assert len(answers) == 20 and all(a["accepted"] for a in answers)
    # Each answer is the report the engine wrote, for the sample's own block and base.
    by_id = {a["id"]: a for a in answers}
    assert by_id["d-16"]["review"]["target"] == "top"
    assert by_id["d-01"]["review"]["comments"][0]["line"] == 28
    assert grade.main([str(out / "answers.jsonl")]) == 0


def test_fake_review_suite_fails_on_misses_and_false_alarms(tmp_path: Path) -> None:
    alarm = {
        "id": "R1",
        "severity": "major",
        "category": "other",
        "file": "rtl/tiny_timer.sv",
        "line": 4,
        "claim": "A made-up problem.",
        "evidence": "rtl/tiny_timer.sv:4 // A small free-running 32-bit counter",
    }
    overrides = {
        "d-01": {"comments": [], "verdict": "approve"},
        "c-01": {"comments": [alarm], "verdict": "changes_requested"},
        "d-03": {"comments": [], "verdict": "maybe"},  # invalid: rejected, no report
    }
    options = evals.EvalOptions(only=("d-01", "d-02", "d-03", "c-01"), fake_overrides=overrides)
    result = evals.run_suite("review", tmp_path / "out", options)
    assert result.status == "fail"
    metrics = result.summary["metrics"]
    assert (metrics["caught"], metrics["defects"], metrics["false_alarms"]) == (1, 3, 1)
    items = {i["id"]: i for i in result.summary["items"]}
    assert items["d-03"]["note"] == "no review" and items["d-03"]["run_note"] == "review rejected"
    assert items["c-01"]["note"] == "false alarm: R1"


# --- chipgraph eval review: claude-code runtime against a stand-in ----------------------

FAKE_CLAUDE = r"""#!{python}
import json, os, subprocess, sys

args = sys.argv[1:]
if args == ["--help"]:
    print("  --max-budget-usd <amount>  Maximum dollar amount")
    sys.exit(0)
if args == ["--version"]:
    print("0.0.0 (fake Claude Code)")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
target = prompt.split()[1]
block = target.split("block=")[1].rstrip("]")
diff = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True).stdout
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "diff": diff.split(),
                        "profile": open(".chipgraph.yml").read()}) + "\n")
# Stand in for the engine: the report an accepted review leaves behind.
review = {"target": block, "comments": [], "verdict": "approve"}
os.makedirs("reports/review", exist_ok=True)
with open(f"reports/review/{block}.json", "w") as out:
    json.dump(review, out)


def emit(event):
    print(json.dumps(event), flush=True)


def call(tid, name, args, result, parent=None):
    emit({"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": args}]}})
    emit({"type": "user", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(result)}]}]}})


tool = "mcp__plugin_chipgraph_chipgraph__"
call("n1", tool + "next_task", {"target": target}, {"tasks": []})
call("a1", "Agent", {"subagent_type": "chipgraph:critic", "model": "opus"}, {})
call("g1", tool + "get_context", {"task_id": target}, {"review": {}}, parent="a1")
if os.environ.get("FAKE_CLAUDE_FOREIGN"):
    call("w1", "Write", {"file_path": "x"}, {}, parent="a1")
call("s1", tool + "submit", {"task_id": target}, {"accepted": True, "reasons": []})
emit({"type": "result", "subtype": "success", "num_turns": 4, "total_cost_usd": 0.25,
      "modelUsage": {"claude-opus": {"outputTokens": 99}}})
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    script.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CHIPGRAPH_EVAL_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(calls))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-for-tests")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return calls


def _opt(argv: list[str], flag: str) -> str:
    return str(argv[argv.index(flag) + 1])


def test_one_headless_run_per_sample_in_its_own_copy(tmp_path: Path, fake_claude: Path) -> None:
    out = tmp_path / "out"
    options = evals.EvalOptions(
        runtime="claude-code", only=("d-01", "c-04"), main_model="haiku", large_model="opus"
    )
    result = evals.run_suite("review", out, options)
    calls = [json.loads(line) for line in fake_claude.read_text().splitlines()]
    assert len(calls) == 2
    by_prompt = {_opt(c["argv"], "-p"): c for c in calls}
    timer = by_prompt["/chipgraph:run digital-rtl/review[block=timer]"]
    top = by_prompt["/chipgraph:run digital-rtl/review[block=top]"]
    # Each sample in its own copy, with its change uncommitted in the working tree.
    assert timer["diff"] == ["rtl/tiny_timer.sv"] and top["diff"] == ["rtl/tiny_top.sv"]
    assert timer["cwd"] != top["cwd"]
    assert "digital-rtl" in timer["profile"] and "    large: opus\n" in timer["profile"]
    argv = timer["argv"]
    assert _opt(argv, "--model") == "haiku"
    allowed = _opt(argv, "--allowedTools").split()
    assert {"Agent", "Read", "Glob", "Grep"} <= set(allowed)
    assert "mcp__plugin_chipgraph_chipgraph__submit" in allowed
    denied = _opt(argv, "--disallowedTools").split()
    assert {"Bash", "Write", "Edit"} <= set(denied)
    assert _opt(argv, "--max-budget-usd") == "3.00"

    summary = result.summary
    assert summary["models"] == {"main": "haiku", "critic": "opus"}
    assert summary["foreign_tool_calls"] == []
    assert summary["cost_usd"]["total"] == 0.5
    # The stand-in's empty review misses d-01 and passes c-04.
    items = {i["id"]: i for i in summary["items"]}
    assert not items["d-01"]["passed"] and items["c-04"]["passed"]
    answers = {
        a["id"]: a
        for a in (json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines())
    }
    assert answers["c-04"]["accepted"] is True and answers["c-04"]["submits"] == 1
    assert answers["c-04"]["review"]["target"] == "top"
    assert (out / "streams" / "d-01.jsonl").is_file()


def test_a_write_by_the_critic_fails_the_run(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FOREIGN", "1")
    options = evals.EvalOptions(runtime="claude-code", only=("c-04",))
    result = evals.run_suite("review", tmp_path / "out", options)
    assert result.status == "fail"
    assert result.summary["foreign_tool_calls"] == ["c-04:Write"]
