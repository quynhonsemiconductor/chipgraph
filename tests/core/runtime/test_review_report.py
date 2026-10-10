"""M2-09: the review reply schema (`ReviewReport`) and its pure validator
(`review_problems`), every rejection case; `parse_review`; `diff_hunks`; the role data
field `engine_writes`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chipgraph.core.runtime.roles import (
    ReviewReport,
    ReviewScope,
    RoleSpec,
    diff_hunks,
    get_role,
    parse_review,
    review_problems,
)
from chipgraph.core.runtime.roles import schema as role_schema

REPO = Path(__file__).resolve().parents[3]

SCOPE = ReviewScope(
    target="timer",
    base="b" * 40,
    head="h" * 40,
    hunks={"rtl/t.sv": ((25, 31), (48, 54)), "doc/t.md": ((10, 12),)},
)


def _comment(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "R1",
        "severity": "major",
        "category": "reset",
        "file": "rtl/t.sv",
        "line": 28,
        "claim": "COUNT resets to 1, the spec says 0.",
        "evidence": "doc/t.md:59 | `0x0` | `COUNT` | RW | 0 |",
        "suggestion": "reset to 0",
        "req_id": None,
    }
    return {**base, **over}


def _review(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "target": "timer",
        "base": "b" * 40,
        "head": "h" * 40,
        "reviewed": ["rtl/t.sv", "doc/t.md"],
        "comments": [_comment()],
        "summary": "One reset value is wrong.",
        "verdict": "changes_requested",
    }
    return {**base, **over}


def _problems(**over: Any) -> list[str]:
    return review_problems(ReviewReport.model_validate(_review(**over)), SCOPE)


def test_a_valid_review_has_no_problems() -> None:
    assert _problems() == []


def test_no_findings_is_valid_when_every_diff_file_is_reviewed() -> None:
    assert _problems(comments=[], verdict="approve") == []


def test_minor_and_nit_comments_may_approve() -> None:
    comments = [_comment(severity="minor"), _comment(id="R2", severity="nit", line=50)]
    assert _problems(comments=comments, verdict="approve") == []


def test_every_evidence_form_is_accepted() -> None:
    for evidence in (
        "doc/t.md:59 COUNT resets to 0",
        "doc/t.md:59-60 the register map",
        "model:register:timer.COUNT reset_value 0",
    ):
        assert _problems(comments=[_comment(evidence=evidence)]) == [], evidence


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"comments": [_comment(evidence="")]}, "evidence is empty"),
        ({"comments": [_comment(evidence="   ")]}, "evidence is empty"),
        ({"comments": [_comment(evidence="the spec says so")]}, "does not quote a source"),
        ({"comments": [_comment(evidence="doc/t.md:59")]}, "does not quote a source"),
        ({"comments": [_comment(line=40)]}, "rtl/t.sv:40 is outside the diff"),
        ({"comments": [_comment(file="rtl/other.sv")]}, "is not one of its files"),
        ({"comments": [_comment(), _comment(line=50)]}, "duplicate comment id 'R1'"),
        ({"comments": [_comment(claim=" ")]}, "claim is empty"),
        ({"verdict": "approve"}, "verdict 'approve' with blocker or major comments (R1)"),
        (
            {"verdict": "approve", "comments": [_comment(severity="blocker")]},
            "verdict 'approve' with blocker",
        ),
        ({"comments": [], "verdict": "changes_requested"}, "with no comments"),
        ({"comments": [], "verdict": "approve", "reviewed": ["rtl/t.sv"]}, "missing: doc/t.md"),
        ({"reviewed": []}, "reviewed is empty"),
        ({"reviewed": ["doc/t.md"]}, "file 'rtl/t.sv' is not in reviewed"),
        ({"reviewed": ["rtl/t.sv", "/abs/x.sv"]}, "not repo-relative: /abs/x.sv"),
        ({"reviewed": ["rtl/t.sv", "../x.sv"]}, "not repo-relative: ../x.sv"),
        ({"target": "gpio"}, "target 'gpio' is not the task's target 'timer'"),
        ({"base": "x"}, "base 'x' is not the task's base"),
        ({"head": "y"}, "head 'y' is not the task's head"),
    ],
)
def test_each_problem_is_reported(over: dict[str, Any], needle: str) -> None:
    problems = _problems(**over)
    assert any(needle in p for p in problems), problems


def test_every_problem_is_listed_at_once() -> None:
    problems = _problems(
        target="gpio", verdict="approve", comments=[_comment(evidence="", line=99)]
    )
    assert len(problems) == 4, problems


def test_a_line_on_a_hunk_edge_is_inside() -> None:
    assert _problems(comments=[_comment(line=25)]) == []
    assert _problems(comments=[_comment(line=54)]) == []
    assert _problems(comments=[_comment(line=55)]) != []


# --- parse_review -----------------------------------------------------------------------


def test_parse_an_object_and_its_json_text() -> None:
    review, problems = parse_review(_review())
    assert problems == [] and review is not None and review.verdict == "changes_requested"
    review, problems = parse_review(json.dumps(_review()))
    assert problems == [] and review is not None
    fenced = "```json\n" + json.dumps(_review(), indent=1) + "\n```"
    review, problems = parse_review(fenced)
    assert problems == [] and review is not None


def test_parse_refuses_bad_json_and_schema_violations() -> None:
    assert parse_review("{not json")[1][0].startswith("review is not valid JSON")
    assert "must be a JSON object" in parse_review("[1, 2]")[1][0]
    review, problems = parse_review(_review(verdict="lgtm"))
    assert review is None and any("review.verdict" in p for p in problems)
    review, problems = parse_review(_review(comments=[_comment(severity="critical")]))
    assert review is None and any("review.comments.0.severity" in p for p in problems)
    review, problems = parse_review({**_review(), "extra": 1})
    assert review is None and any("review.extra" in p for p in problems)
    review, problems = parse_review(_review(comments=[_comment(line=0)]))
    assert review is None and any("review.comments.0.line" in p for p in problems)
    review, problems = parse_review(_review(comments=[_comment(category="Reset Value")]))
    assert review is None and any("review.comments.0.category" in p for p in problems)
    bare = _review()
    del bare["comments"][0]["evidence"]
    review, problems = parse_review(bare)
    assert review is None and any("review.comments.0.evidence" in p for p in problems)


# --- diff_hunks -------------------------------------------------------------------------

DIFF = """\
diff --git a/rtl/t.sv b/rtl/t.sv
index 1111111..2222222 100644
--- a/rtl/t.sv
+++ b/rtl/t.sv
@@ -25,7 +25,7 @@ module t (
   always_ff @(posedge clk) begin
-    a <= 1'b0;
+    a <= 1'b1;
@@ -40,4 +40,0 @@
-    gone
diff --git a/rtl/new.sv b/rtl/new.sv
new file mode 100644
--- /dev/null
+++ b/rtl/new.sv
@@ -0,0 +1,3 @@
+module new_one;
++++ not a header
+endmodule
diff --git a/rtl/old.sv b/rtl/old.sv
deleted file mode 100644
--- a/rtl/old.sv
+++ /dev/null
@@ -1,2 +0,0 @@
-module old;
-endmodule
"""


def test_diff_hunks_reads_new_side_ranges() -> None:
    assert diff_hunks(DIFF) == {
        "rtl/t.sv": ((25, 31), (40, 41)),
        "rtl/new.sv": ((1, 3),),
    }
    assert diff_hunks("") == {}


# --- the role data and the committed schema ---------------------------------------------


def test_the_critic_has_the_engine_write_its_report() -> None:
    critic = get_role("critic")
    assert critic.write_scope == "none" and critic.engine_writes == ("report",)
    assert "write_outputs" not in critic.tools


def test_engine_writes_needs_a_role_that_writes_nothing() -> None:
    data = get_role("author").model_dump()
    with pytest.raises(ValueError, match="engine_writes is only for a role"):
        RoleSpec.model_validate({**data, "engine_writes": ["report"]})


def test_the_review_schema_is_committed() -> None:
    files = role_schema.generate()
    assert "ReviewReport.schema.json" in files
    committed = REPO / "schemas" / "roles" / "ReviewReport.schema.json"
    assert committed.read_text(encoding="utf-8") == files["ReviewReport.schema.json"]
    schema = json.loads(files["ReviewReport.schema.json"])
    assert {"target", "base", "head", "reviewed", "summary", "verdict"} <= set(schema["required"])
