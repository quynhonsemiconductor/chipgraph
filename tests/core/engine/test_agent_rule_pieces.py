"""M2-02a: the pure pieces of the agent rule loop (labels, budget, feedback) and the loop
outside a scheduler, plus a property-style bound on runtime calls."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from agent_rule_helpers import (
    OUTPUT,
    ContentChecks,
    FakeRuntime,
    Step,
    agent_rule,
    project,
    result,
)

from chipgraph.core.contracts import AgentResult, Budget, Issue
from chipgraph.core.contracts.rule import RuleInstance
from chipgraph.core.engine.agent_rule import (
    DEFAULT_LABEL_MAP,
    AgentRuleExecutor,
    AttemptBudget,
    BudgetState,
    FailureClassifier,
    LabelMap,
    attempt_checks,
    classify,
    deterministic_label,
    failure_signature,
    render_feedback,
)
from chipgraph.core.engine.decide import DecisionLog, ModelAnswer, Question
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules

DONE = AgentResult(status="done")

# --- classify: one test per label -------------------------------------------------------


def test_label_infra_from_an_error_status_or_a_runtime_that_raised() -> None:
    assert classify([result("lint", "error")], DONE) == "infra"
    assert classify([], None) == "infra"
    # an errored check next to a content failure: the content failure decides
    assert classify([result("lint", "error"), result("sim", "fail")], DONE) == "verification"


def test_label_planning_from_needs_human_or_a_spec_issue() -> None:
    asking = AgentResult(status="needs_human", open_questions=("which clock?",))
    assert classify([], asking) == "planning"
    spec = result("cross", "fail", issues=(Issue(rule="spec.conflict.req", msg="REQ-1 vs REQ-2"),))
    assert classify([spec, result("lint", "fail")], DONE) == "planning"


def test_label_context_from_missing_inputs_outputs_or_an_agent_that_lacks_one() -> None:
    missing = result("agent.outputs", "fail", issues=(Issue(rule="output.missing", msg="x"),))
    assert classify([missing], DONE) == "context"
    gave_up = AgentResult(status="failed", open_questions=("the register map is missing",))
    assert classify([], gave_up) == "context"


def test_label_constraint_from_project_rules() -> None:
    assert classify([result("naming", "fail")], DONE) == "constraint"
    assert classify([result("filelist_rtl", "fail")], DONE) == "constraint"  # prefix match
    outside = result("custom", "fail", issues=(Issue(rule="write.outside", msg="x"),))
    assert classify([outside], DONE) == "constraint"
    # constraint wins over verification when both fail
    assert classify([result("lint", "fail"), result("layout", "fail")], DONE) == "constraint"


def test_label_verification_for_content_and_by_default() -> None:
    assert classify([result("lint", "fail")], DONE) == "verification"
    assert classify([result("my_own_check", "fail")], DONE) == "verification"
    assert deterministic_label([result("my_own_check", "fail")], DONE) is None  # ambiguous
    assert classify([], AgentResult(status="failed")) == "verification"


def test_label_map_is_data_and_packs_extend_it() -> None:
    extended = DEFAULT_LABEL_MAP.merged(
        LabelMap(checks={"my_own_check": "constraint", "lint": "constraint"})
    )
    assert classify([result("my_own_check", "fail")], DONE, label_map=extended) == "constraint"
    assert classify([result("lint", "fail")], DONE, label_map=extended) == "constraint"
    assert DEFAULT_LABEL_MAP.for_check("lint") == "verification"  # the default is unchanged
    # a prefix only matches at a separator, and the longest prefix wins
    table = LabelMap(checks={"sim": "verification", "sim.cfg": "context"})
    assert table.for_check("simple") is None
    assert table.for_check("sim.cfg.load") == "context"
    assert table.for_check("sim/timer") == "verification"
    # an issue's rule wins over its check id
    issue = result("lint", "fail", issues=(Issue(rule="naming.prefix", msg="x"),))
    assert classify([issue], DONE) == "constraint"


class _Backend:
    name = "fake"

    def __init__(self, value: str | None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail
        self.asked: list[Question] = []

    async def ask(self, question: Question, tier: str) -> ModelAnswer:
        self.asked.append(question)
        if self.fail:
            raise RuntimeError("refused")
        return ModelAnswer(value=self.value, confidence=0.95)


def test_classifier_asks_decide_only_when_no_rule_applies(tmp_path: Path) -> None:
    backend = _Backend("constraint")
    log = DecisionLog(tmp_path / "decisions.jsonl")
    classifier = FailureClassifier(backend=backend, log=log)
    assert asyncio.run(classifier.classify([result("lint", "fail")], DONE)) == "verification"
    assert backend.asked == []  # a rule answered
    unknown = [result("my_own_check", "fail", issues=(Issue(msg="odd"),))]
    assert asyncio.run(classifier.classify(unknown, DONE)) == "constraint"
    [question] = backend.asked
    assert question.id.startswith("triage.agent.")
    assert "infra" not in question.choices
    assert [e.event for e in log.read()] == ["decided"]


def test_classifier_falls_back_to_verification(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    unknown = [result("my_own_check", "fail")]
    for backend in (_Backend(None), _Backend("x", fail=True)):
        classifier = FailureClassifier(backend=backend, log=log)
        assert asyncio.run(classifier.classify(unknown, DONE)) == "verification"
    with pytest.raises(ValueError, match="DecisionLog"):
        FailureClassifier(backend=_Backend("context"))


# --- attempt_checks ---------------------------------------------------------------------


def test_attempt_checks_find_missing_and_empty_files_and_outside_writes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, LabelRules())
    graph = build_graph([agent_rule()], StaticForeach({}))
    instance: RuleInstance = graph.instances["p/write[]"]
    agent = AgentResult(status="done", files_written=(OUTPUT, "x.txt"))
    inputs, outputs, writes = attempt_checks(instance, agent, store, allowed_writes=(OUTPUT,))
    assert [i.rule for i in inputs.issues] == ["input.missing"]
    assert [i.rule for i in outputs.issues] == ["output.missing"]
    assert [i.file for i in writes.issues] == ["x.txt"]
    (tmp_path / "spec.md").write_text("s")
    (tmp_path / "rtl").mkdir()
    (tmp_path / OUTPUT).write_text("")
    inputs, outputs, writes = attempt_checks(instance, DONE, store, allowed_writes=(OUTPUT,))
    assert (inputs.status, writes.status) == ("pass", "pass")
    assert [i.rule for i in outputs.issues] == ["output.empty"]


# --- AttemptBudget ----------------------------------------------------------------------


def test_budget_counts_tries_not_infra_and_escalates_after_a_failed_try() -> None:
    budget = AttemptBudget(Budget(tries=2, tier="medium", escalate="large"))
    assert budget.tier == "medium"
    budget.record_infra()
    assert (budget.tier, budget.remaining, budget.exhausted) == ("medium", 2, None)
    budget.record_failure([result("lint", "fail")], {"a": "1"})
    assert budget.tier == "large"
    assert budget.task_budget() == Budget(tries=2, tier="large")
    budget.record_failure([result("lint", "fail")], {"a": "2"})
    assert budget.exhausted == "tries"
    assert budget.state.infra_failures == 1


def test_budget_role_ladder_and_caps() -> None:
    budget = AttemptBudget.for_rule(agent_rule(budget=Budget(tries=3)))
    assert (budget.first_tier, budget.escalate) == ("medium", "large")  # the author role
    capped = AttemptBudget(Budget(tries=9, tokens=50), max_cost=1.0)
    capped.spend(AgentResult(status="done", tokens=49, cost=0.5))
    assert capped.exhausted is None
    capped.spend(AgentResult(status="done", tokens=1))
    assert capped.exhausted == "tokens"
    cost = AttemptBudget(Budget(tries=9), max_cost=1.0)
    cost.spend(AgentResult(status="done", cost=1.0))
    assert cost.exhausted == "cost"


def test_budget_stagnation_needs_the_same_outputs_and_failures() -> None:
    budget = AttemptBudget(Budget(tries=9))
    budget.record_failure([result("lint", "fail")], {"a": "1"})
    budget.record_failure([result("lint", "fail")], {"a": "2"})  # new output
    assert budget.exhausted is None
    budget.record_failure([result("sim", "fail")], {"a": "2"})  # new failures
    assert budget.exhausted is None
    budget.record_failure([result("sim", "fail")], {"a": "2"})
    assert budget.exhausted == "stagnation"
    off = AttemptBudget(Budget(tries=9), stagnation=False)
    off.record_failure([], {})
    off.record_failure([], {})
    assert off.exhausted is None


def test_budget_state_round_trips() -> None:
    budget = AttemptBudget(Budget(tries=3, tier="small", escalate="medium"))
    budget.record_failure([result("lint", "fail")], {})
    again = AttemptBudget(
        budget.budget, state=BudgetState.model_validate_json(budget.state.model_dump_json())
    )
    assert (again.tier, again.remaining) == ("medium", 2)


def test_failure_signature_ignores_passing_checks_and_order() -> None:
    a = [result("lint", "fail"), result("sim", "pass"), result("x", "fail")]
    b = [result("x", "fail"), result("lint", "fail")]
    assert failure_signature(a) == failure_signature(b)
    assert failure_signature(a) != failure_signature([result("lint", "fail")])


# --- render_feedback --------------------------------------------------------------------


def test_feedback_names_checks_issues_label_and_hint() -> None:
    text = render_feedback(
        [
            result("lint", "fail", issues=(Issue(file="a.sv", line=7, rule="WIDTH", msg="w"),)),
            result("sim", "fail", log_tail="line 1\nassert failed at t=10"),
            result("ok", "pass"),
        ],
        "verification",
        attempt=2,
    )
    assert text.startswith("Attempt 2 failed (verification): 2 check(s) did not pass.")
    assert "- check `lint`: fail\n  - a.sv:7: [WIDTH] w" in text
    assert "assert failed at t=10" in text
    assert "`ok`" not in text
    assert "What to fix: Fix the content" in text


def test_feedback_is_bounded() -> None:
    many = tuple(Issue(file="a.sv", line=i + 1, msg="m" * 500) for i in range(100))
    long_log = "x" * 10_000
    text = render_feedback(
        [result("lint", "fail", issues=many), result("sim", "fail", log_tail=long_log)],
        "verification",
        max_issues_per_check=5,
    )
    assert text.count("  - a.sv:") == 5
    assert "… and 95 more issue(s)" in text
    assert len(text) <= 6000
    assert max(len(line) for line in text.splitlines()) <= 600 + 4  # log tail + indent
    tiny = render_feedback([result("lint", "fail", issues=many)], "context", max_chars=300)
    assert len(tiny) <= 300


# --- the executor outside a scheduler ---------------------------------------------------


def test_execute_without_a_scheduler_uses_its_own_check_runner(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("s")
    store = ArtifactStore(tmp_path, LabelRules())
    runtime = FakeRuntime(tmp_path, [Step(content="bad"), Step(content="good")])
    checks = ContentChecks(tmp_path)
    executor = AgentRuleExecutor(runtime, store=store, checks=checks)
    rule = agent_rule()
    instance = build_graph([rule], StaticForeach({})).instances["p/write[]"]
    outcome = asyncio.run(executor.execute(rule, instance))
    assert outcome.ok
    assert checks.calls == ["lint", "lint"]
    assert executor.results["p/write[]"].status == "done"


def test_rejects_negative_infra_retries(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, LabelRules())
    with pytest.raises(ValueError, match="max_infra_retries"):
        AgentRuleExecutor(FakeRuntime(tmp_path, [Step()]), store=store, max_infra_retries=-1)


# --- property: the loop is bounded for any sequence of results --------------------------

_KINDS = ("pass", "fail", "error", "raise", "needs_human", "failed", "same")


def _random_steps(rng: random.Random) -> list[Step]:
    steps = []
    for n in range(40):
        kind = rng.choice(_KINDS)
        if kind == "raise":
            steps.append(Step(raises=OSError("flaky")))
        elif kind == "needs_human":
            steps.append(Step(status="needs_human", questions=("q?",), content=None))
        elif kind == "failed":
            steps.append(Step(status="failed", content=f"bad {n}"))
        elif kind == "same":
            steps.append(Step(content="bad same"))
        elif kind == "pass":
            steps.append(Step(content="good", tokens=rng.choice((None, 10))))
        else:
            steps.append(Step(content=f"bad {n}" if kind == "fail" else "good"))
    return steps


@pytest.mark.parametrize("seed", range(60))
def test_runtime_calls_never_exceed_tries_plus_infra_retries(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    tries = rng.randint(1, 4)
    max_infra = rng.randint(0, 3)
    statuses = [rng.choice(("pass", "fail", "error")) for _ in range(40)]
    checks = ContentChecks(tmp_path, script={"lint": statuses})
    p = project(
        tmp_path,
        _random_steps(rng),
        budget=Budget(tries=tries, tokens=rng.choice((None, 25))),
        checks=checks,
        max_infra_retries=max_infra,
        stagnation=rng.random() < 0.5,
    )
    summary = asyncio.run(p.scheduler.run("p/write"))
    assert 1 <= p.runtime.calls <= tries + max_infra
    assert p.runtime.calls <= p.executor.max_calls(p.scheduler.graph.rules["p/write"])
    assert len(summary.done) + len(summary.failed) == 1
    final = p.executor.results["p/write[]"]
    assert final.status in ("done", "failed", "needs_human", "budget_exhausted")
    assert (final.status == "done") == (summary.done == ("p/write[]",))
