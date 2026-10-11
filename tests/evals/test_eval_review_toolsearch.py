"""The review harness counts a `ToolSearch` as foreign unless it only loads tools the run
may use (it fetches schemas of deferred tools and reads nothing).
"""

from __future__ import annotations

import importlib

from test_eval_review import evals  # the harness package, loaded once

review = importlib.import_module("chipgraph_evals.review")
TOOL = review.TOOL


def _search(query: str, parent: str | None = None) -> dict[str, object]:
    return {"parent": parent, "name": "ToolSearch", "input": {"query": query}, "result": None}


def test_loading_allowed_tools_is_neutral() -> None:
    assert evals is not None
    calls = [
        _search(f"select:{TOOL}next_task,{TOOL}submit"),
        _search(f"select:{TOOL}get_context", parent="toolu_1"),
    ]
    assert review.foreign_calls("d-01", calls) == []


def test_loading_another_tool_is_foreign() -> None:
    [out] = review.foreign_calls("d-01", [_search("select:SendMessage")])
    assert out == "d-01:ToolSearch(select:SendMessage)"


def test_a_mixed_query_is_foreign_when_any_name_is_not_allowed() -> None:
    calls = [_search(f"select:{TOOL}next_task,SendMessage")]
    assert review.foreign_calls("d-01", calls) == [
        f"d-01:ToolSearch(select:{TOOL}next_task,SendMessage)"
    ]


def test_a_tool_allowed_for_the_main_session_only_is_foreign_for_the_critic() -> None:
    calls = [_search(f"select:{TOOL}next_task", parent="toolu_1")]
    assert review.foreign_calls("d-01", calls) == [f"d-01:ToolSearch(select:{TOOL}next_task)"]


def test_a_keyword_query_cannot_be_verified_and_is_foreign() -> None:
    assert review.foreign_calls("d-01", [_search("notebook edit")]) == [
        "d-01:ToolSearch(notebook edit)"
    ]


def test_other_calls_are_judged_as_before() -> None:
    calls = [{"parent": None, "name": "Bash", "input": {}, "result": None}]
    assert review.foreign_calls("d-01", calls) == ["d-01:Bash"]
