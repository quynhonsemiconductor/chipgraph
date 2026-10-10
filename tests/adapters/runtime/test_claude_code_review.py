"""M2-09: the review task in runtime claude-code, in process, on a tmp git copy of
tinysoc with the `digital-rtl` pack: the critic's context (the diff, its cap, the spec
and model slices, fresh, the base ref, the nda refusal) and `submit` with a valid, an
invalid and an evidence-free `review` (the engine writes the report; a rejection uses a
try). No model: the test plays the Critic's part.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from chipgraph.adapters.runtime.claude_code import review as review_mod
from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.ingest import run_ingest
from chipgraph.core.contracts import ArtifactRef, RuleInstance
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "examples" / "tinysoc"
TASK = "digital-rtl/review[block=timer]"
TARGET = "digital-rtl/review[block=timer]"
RTL = "rtl/tiny_timer.sv"
MAS = "doc/specs/TINY_TIMER_MAS.md"
REPORT = "reports/review/timer.json"
BUG = ("      count_q   <= 32'd0;\n", "      count_q   <= 32'd1;\n")
BUG_LINE = 28
NOTE_RULE = {
    "rule": "note",
    "kind": "agent",
    "role": "author",
    "outputs": ["doc/note.md"],
}


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture(scope="module")
def base_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """tinysoc with `digital-rtl` and a one-rule project pack `extra`, committed, ingested."""
    dest = tmp_path_factory.mktemp("review") / "tinysoc"
    shutil.copytree(EXAMPLE, dest)
    profile = yaml.safe_load((dest / ".chipgraph.yml").read_text())
    profile["packs"] = [*profile["packs"], "digital-rtl", "extra"]
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(profile, sort_keys=False))
    pack = dest / ".chipgraph" / "packs" / "extra"
    (pack / "rules").mkdir(parents=True)
    manifest = {"name": "extra", "version": "0.1.0", "provides": {"rules": ["rules"]}}
    (pack / "pack.yml").write_text(yaml.safe_dump(manifest))
    (pack / "rules" / "note.yml").write_text(yaml.safe_dump(NOTE_RULE))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with digital-rtl")
    run_ingest(AppContext.load(dest))
    return dest


@pytest.fixture
def project(base_project: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "tinysoc"
    shutil.copytree(base_project, dest, symlinks=True)
    return dest


def _ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def _plant(root: Path, find: str = BUG[0], replace: str = BUG[1], path: str = RTL) -> None:
    file = root / path
    file.write_text(file.read_text().replace(find, replace, 1))


def _dispatch(root: Path, target: str = TARGET) -> dict[str, Any]:
    answer = asyncio.run(service.next_task(_ctx(root), target))
    [task] = answer["tasks"]
    return task


def _context(root: Path, task_id: str = TASK) -> dict[str, Any]:
    return asyncio.run(service.get_context(_ctx(root), task_id))


def _submit(root: Path, review: Any, task_id: str = TASK) -> dict[str, Any]:
    report = service.SubmitReport(review=review)
    return asyncio.run(service.submit(_ctx(root), task_id, report))


def _reply(context: dict[str, Any], **over: Any) -> dict[str, Any]:
    review = context["review"]
    reply: dict[str, Any] = {
        "target": review["target"],
        "base": review["base"],
        "head": review["head"],
        "reviewed": list(review["changed"]),
        "comments": [
            {
                "id": "R1",
                "severity": "major",
                "category": "reset",
                "file": RTL,
                "line": BUG_LINE,
                "claim": "COUNT resets to 1; the register map resets it to 0.",
                "evidence": f"{MAS}:59 | `0x0` | `COUNT` | `COUNT` | 31:0 | RW | 0 |",
                "suggestion": "reset count_q to 0",
                "req_id": None,
            }
        ],
        "summary": "COUNT has the wrong reset value.",
        "verdict": "changes_requested",
    }
    reply.update(over)
    return reply


def _record(root: Path) -> Any:
    return TaskQueue(StateLayout(root)).require(TASK)


# --- next_task and get_context ----------------------------------------------------------


def test_the_review_task_goes_to_the_critic_on_the_large_tier(project: Path) -> None:
    _plant(project)
    task = _dispatch(project)
    assert task["task_id"] == TASK
    assert task["agent"] == "chipgraph:critic"
    assert (task["tier"], task["model"]) == ("large", "opus")
    assert task["outputs"] == [REPORT]
    assert task["reply"] == "review"
    assert "write no file" in task["prompt"]


def test_context_has_the_diff_spec_and_model_slices(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    context = _context(project)
    review = context["review"]
    head = _git(project, "rev-parse", "HEAD")
    assert (review["target"], review["base"], review["head"]) == ("timer", head, head)
    assert review["base_from"] == "merge-base of HEAD and main"
    assert review["changed"] == [RTL]
    assert {RTL, MAS, "filelists/timer.f"} <= set(review["files"])
    assert REPORT not in review["files"]
    # Each hunk line carries its new-file line number; the removed line has none.
    assert f"{BUG_LINE:>5} +{BUG[1].rstrip()}" in review["diff"]
    assert f"{'':>5} -{BUG[0].rstrip()}" in review["diff"]
    assert review["truncated"] is False and review["omitted"] == []
    req_ids = [r["id"] for r in review["spec"]["requirements"]]
    assert req_ids == [f"REQ-TIM-00{n}" for n in range(1, 6)]
    assert review["spec"]["requirements"][0]["source"] == f"{MAS}:68"
    assert {p["name"] for p in review["spec"]["interface"]} >= {"clk", "rst_n", "irq"}
    count = next(r for r in review["spec"]["registers"] if r["name"] == "COUNT")
    assert count["reset_value"] == 0 and count["source"] == f"{MAS}:59"
    assert review["model"]["key"] == "block:timer"
    assert review["reply_schema"]["title"] == "ReviewReport"
    assert "write no file" in context["instructions"]
    assert context["skill_texts"].keys() == {"review/diff"}


def test_context_is_fresh_no_earlier_review_and_no_author_reasoning(project: Path) -> None:
    old = project / REPORT
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"summary": "EARLIER-REVIEW-MARKER"}))
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "an earlier review")
    _plant(project)
    _dispatch(project)
    context = _context(project)
    text = json.dumps(context)
    assert "EARLIER-REVIEW-MARKER" not in text
    assert REPORT not in context["review"]["changed"]
    assert context["previous_rejection"] == []
    assert {i.get("model_key") for i in context["inputs"]} == {"block/timer"}


def test_the_diff_is_capped_with_a_note(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(project)
    _plant(project, "# 1. Overview", "# 1. Overview (edited)", path=MAS)
    monkeypatch.setattr(review_mod, "DIFF_CAP", 300)
    _dispatch(project)
    review = _context(project)["review"]
    assert review["changed"] == [MAS, RTL]
    assert review["truncated"] is True
    assert len(review["diff"]) <= 300
    assert review["omitted"] == [MAS, RTL][-len(review["omitted"]) :]
    assert "cut at 300 characters" in review["note"]
    assert all(path in review["note"] for path in review["omitted"])


def test_an_empty_diff_says_so(project: Path) -> None:
    _dispatch(project)
    review = _context(project)["review"]
    assert review["changed"] == [] and review["diff"] == ""
    assert "The diff is empty" in review["note"]


def test_base_is_the_merge_base_on_a_feature_branch(project: Path) -> None:
    fork = _git(project, "rev-parse", "HEAD")
    _git(project, "checkout", "-q", "-b", "feat/x")
    _plant(project)
    _git(project, "commit", "-q", "-am", "the change")
    _dispatch(project)
    review = _context(project)["review"]
    assert review["base"] == fork
    assert review["head"] == _git(project, "rev-parse", "HEAD")
    assert review["changed"] == [RTL]


def test_base_from_the_rule_param(project: Path) -> None:
    first = _git(project, "rev-parse", "HEAD")
    (project / "extra.txt").write_text("x\n")
    _git(project, "add", "extra.txt")
    _git(project, "commit", "-q", "-m", "second")
    instance = RuleInstance(
        rule_id="digital-rtl/review",
        params={"block": "timer", "base": first},
        outputs=(ArtifactRef(kind="report", path=REPORT),),
        instance_id=RuleInstance.make_id("digital-rtl/review", {"block": "timer", "base": first}),
    )
    base, how, head = asyncio.run(review_mod.resolve_refs(_ctx(project), instance))
    assert (base, how) == (first, f"rule param base={first}")
    assert head == _git(project, "rev-parse", "HEAD")
    bad = instance.model_copy(update={"params": {"block": "timer", "base": "no-such-ref"}})
    with pytest.raises(AppError, match="base ref 'no-such-ref' is not a commit"):
        asyncio.run(review_mod.resolve_refs(_ctx(project), bad))


def test_new_untracked_files_are_in_the_diff(project: Path) -> None:
    (project / "rtl" / "tiny_extra.sv").write_text("module tiny_extra;\nendmodule\n")
    head = _git(project, "rev-parse", "HEAD")
    diff = asyncio.run(review_mod.full_diff(_ctx(project), head, ["rtl/tiny_extra.sv", RTL]))
    assert "+++ b/rtl/tiny_extra.sv" in diff and "+module tiny_extra;" in diff


def test_a_new_file_the_block_filelist_lists_is_reviewed(project: Path) -> None:
    (project / "rtl" / "tiny_timer_pre.sv").write_text("module tiny_timer_pre;\nendmodule\n")
    with (project / "filelists" / "timer.f").open("a") as filelist:
        filelist.write("rtl/tiny_timer_pre.sv\n")
    (project / "rtl" / "unrelated.sv").write_text("module unrelated;\nendmodule\n")
    _dispatch(project)
    review = _context(project)["review"]
    assert review["changed"] == ["filelists/timer.f", "rtl/tiny_timer_pre.sv"]
    assert "    1 +module tiny_timer_pre;" in review["diff"]
    assert "unrelated" not in review["diff"]


def test_nda_files_in_the_diff_are_refused(project: Path) -> None:
    profile_path = project / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["data"] = {"nda_paths": [RTL]}
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    _git(project, "commit", "-q", "-am", "nda rtl")
    _plant(project)
    _dispatch(project)
    with pytest.raises(AppError, match=r"reviews files labelled 'nda' \(rtl/tiny_timer\.sv\)"):
        _context(project)
    assert _record(project).status == "needs_human"


# --- submit -----------------------------------------------------------------------------


def test_a_valid_review_is_written_by_the_engine_and_accepted(project: Path) -> None:
    _plant(project)
    run_id = asyncio.run(service.next_task(_ctx(project), TARGET))["run_id"]
    reply = _reply(_context(project))
    answer = _submit(project, reply)
    assert answer["accepted"] is True, answer
    assert answer["result"]["files_written"] == [REPORT]
    written = json.loads((project / REPORT).read_text())
    assert written == {"schema_version": 1, **reply}
    # The build goes on: the review is done and fresh.
    assert asyncio.run(service.next_task(_ctx(project), TARGET))["done"] is True
    events = journal_mod.read(StateLayout(project).journal(run_id)).events
    submit = next(e for e in events if e.payload.get("phase") == "submit")
    assert submit.payload["review"] == {"verdict": "changes_requested", "comments": 1}


def test_no_findings_is_accepted(project: Path) -> None:
    _plant(project, "// A minimal", "// A small")
    _dispatch(project)
    reply = _reply(_context(project), comments=[], verdict="approve")
    assert _submit(project, reply)["accepted"] is True
    assert json.loads((project / REPORT).read_text())["comments"] == []


def test_review_as_json_text_is_accepted(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    text = "```json\n" + json.dumps(_reply(_context(project))) + "\n```"
    assert _submit(project, text)["accepted"] is True


def test_invalid_json_is_rejected_and_uses_a_try(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    _context(project)
    answer = _submit(project, "{this is not json")
    assert answer["accepted"] is False and answer["status"] == "rejected"
    assert answer["attempts"] == 1
    assert answer["reasons"][0].startswith("review: review is not valid JSON")
    assert answer["missing"] == []
    assert not (project / REPORT).exists()


def test_a_schema_violation_is_rejected(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    answer = _submit(project, _reply(_context(project), verdict="lgtm"))
    assert answer["accepted"] is False
    assert any("review.verdict" in r for r in answer["reasons"])


def test_empty_evidence_is_rejected_then_the_budget_runs_out(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    context = _context(project)
    bare = _reply(context)
    bare["comments"][0]["evidence"] = ""
    first = _submit(project, bare)
    assert first["status"] == "rejected" and first["attempts"] == 1
    assert any("evidence is empty" in r for r in first["reasons"])
    assert not (project / REPORT).exists()
    # Dispatched again with the reason; the context says why.
    task = _dispatch(project)
    assert task["attempt"] == 2
    assert any("evidence is empty" in r for r in _context(project)["previous_rejection"])
    outside = _reply(context)
    outside["comments"][0]["line"] = 60
    second = _submit(project, outside)
    assert second["status"] == "budget_exhausted" and second["attempts"] == 2
    assert any("rtl/tiny_timer.sv:60 is outside the diff" in r for r in second["reasons"])


def test_a_review_task_without_review_is_rejected(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    _context(project)
    answer = asyncio.run(service.submit(_ctx(project), TASK, service.SubmitReport()))
    assert answer["accepted"] is False
    assert "submitted with `review`" in answer["reasons"][0]


def test_submit_without_get_context_checks_against_the_diff_now(project: Path) -> None:
    _plant(project)
    _dispatch(project)
    head = _git(project, "rev-parse", "HEAD")
    reply = {
        "target": "timer",
        "base": head,
        "head": head,
        "reviewed": [RTL],
        "comments": [],
        "summary": "fine",
        "verdict": "approve",
    }
    assert _submit(project, reply)["accepted"] is True


def test_review_on_a_task_that_is_not_a_review_is_an_error(project: Path) -> None:
    task = _dispatch(project, "extra/note")
    assert "reply" not in task
    with pytest.raises(AppError, match="is not a review task"):
        _submit(project, {"verdict": "approve"}, task_id="extra/note[]")
    assert TaskQueue(StateLayout(project)).require("extra/note[]").status == "dispatched"


def test_the_mcp_submit_tool_takes_review(project: Path) -> None:
    from mcp import Client

    from chipgraph.mcp.server import build_server

    server = build_server(project)

    async def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
        assert not result.is_error, result.content
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    _plant(project)
    handed = asyncio.run(_call("next_task", {"target": TARGET}))
    assert handed["tasks"][0]["reply"] == "review"
    context = asyncio.run(_call("get_context", {"task_id": TASK}))
    result = {"status": "done", "review": _reply(context)}
    answer = asyncio.run(_call("submit", {"task_id": TASK, "result": result}))
    assert answer["accepted"] is True, answer
    assert json.loads((project / REPORT).read_text())["verdict"] == "changes_requested"
