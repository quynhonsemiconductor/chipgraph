"""M2-08: N candidates share ONE budget: the plan, the ledger, the guard, cancels."""

from __future__ import annotations

import asyncio
import random
import shutil
from pathlib import Path

import pytest
from select_helpers import (
    OUTPUT,
    Behaviour,
    Factory,
    Journal,
    ScriptedRuntime,
    best_of,
    check,
    instance,
    repo,
    rule,
)

from chipgraph.core.config.models import SelectCfg
from chipgraph.core.contracts import Budget
from chipgraph.core.engine.select import (
    BestOfNResult,
    BudgetPlan,
    CostLedger,
    LedgerExhausted,
    LedgerLimits,
    _GuardedRuntime,
    plan_budget,
)
from chipgraph.core.plugin_api.types import AgentTask

# --- the plan: N, the shared limits, the slices ---------------------------------------------


def test_the_default_plan_for_two_and_three_candidates() -> None:
    budget = Budget(tries=3, tokens=200_000)
    two = plan_budget(budget, requested=2, max_cost=2.0)
    assert (two.n, two.capped_by, two.factor) == (2, None, 1.5)
    assert (two.limits.tries, two.limits.tokens, two.limits.cost) == (4, 300_000, 3.0)
    assert (two.slice.tries, two.slice.tokens, two.slice_cost) == (2, 150_000, 1.5)

    three = plan_budget(budget, requested=3, max_cost=2.0)
    assert (three.n, three.limits.tries, three.limits.tokens) == (3, 4, 300_000)
    assert (three.slice.tries, three.slice.tokens, three.slice_cost) == (1, 100_000, 1.0)
    # Never N x the single budget: the slices add up to the shared limits at most.
    for plan in (two, three):
        assert plan.slice.tries * plan.n <= plan.limits.tries
        assert plan.slice.tokens is not None and plan.limits.tokens is not None
        assert plan.slice.tokens * plan.n <= plan.limits.tokens


def test_n_above_max_n_is_capped() -> None:
    plan = plan_budget(Budget(tries=10), requested=7, cfg=SelectCfg(max_n=3))
    assert (plan.requested, plan.n, plan.capped_by) == (7, 3, "max_n")
    assert SelectCfg(n=5).effective_n() == 3


def test_n_is_capped_at_the_variants_there_are() -> None:
    plan = plan_budget(Budget(tries=10), requested=3, variants=2)
    assert (plan.n, plan.capped_by) == (2, "variants")


def test_n_drops_to_what_the_budget_fits_and_one_is_a_plain_run() -> None:
    one = plan_budget(Budget(tries=1), requested=3)
    assert (one.n, one.capped_by, one.plain) == (1, "budget", True)
    two = plan_budget(Budget(tries=1), requested=3, cfg=SelectCfg(budget_factor=2.0))
    assert (two.n, two.capped_by, two.slice.tries) == (2, "budget", 1)


def test_candidates_start_at_the_rules_tier() -> None:
    plan = plan_budget(Budget(tries=4, tier="small", escalate="large"), requested=2)
    assert (plan.slice.tier, plan.slice.escalate) == ("small", "large")


# --- the ledger and the guard ---------------------------------------------------------------


def _task(tokens: int | None = None) -> AgentTask:
    return AgentTask(
        instance=instance(),
        role="author",
        allowed_writes=(OUTPUT,),
        budget=Budget(tries=2, tokens=tokens),
    )


def test_the_guard_reserves_a_try_lowers_the_token_cap_and_adds_the_variant(
    tmp_path: Path,
) -> None:
    ledger = CostLedger(LedgerLimits(tries=2, tokens=100))
    inner = ScriptedRuntime(tmp_path, Behaviour(tokens=lambda task: task.budget.tokens or 0))
    guard = _GuardedRuntime(inner, ledger, "a", {"variant": "approach a", "seed": "a-1"})
    asyncio.run(guard.run_task(_task(tokens=80)))
    sent = inner.tasks[0]
    assert (sent.context["variant"], sent.context["seed"], sent.budget.tokens) == (
        "approach a",
        "a-1",
        80,
    )
    asyncio.run(guard.run_task(_task(tokens=80)))
    assert inner.tasks[1].budget.tokens == 20  # what the ledger had left
    assert (ledger.total.tries, ledger.total.tokens, ledger.held_tokens) == (2, 100, 0)
    with pytest.raises(LedgerExhausted) as stop:
        asyncio.run(guard.run_task(_task(tokens=80)))
    assert stop.value.limit == "tries"
    assert len(inner.tasks) == 2 and ledger.limited_by == "tries"


def test_a_runtime_that_raises_releases_its_hold(tmp_path: Path) -> None:
    ledger = CostLedger(LedgerLimits(tries=3, tokens=100))
    inner = ScriptedRuntime(tmp_path, Behaviour(raise_calls=1))
    guard = _GuardedRuntime(inner, ledger, "a", {})
    with pytest.raises(RuntimeError):
        asyncio.run(guard.run_task(_task(tokens=60)))
    assert (ledger.total.tries, ledger.held_tokens, ledger.remaining_tokens) == (1, 0, 100)


_BIGGEST_CALL = 0.05


async def _simulate(plan: BudgetPlan, ledger: CostLedger, rng: random.Random, root: Path) -> None:
    """N candidates calling through their guards at once, as the agent loop would."""

    def spend(task: AgentTask) -> int:
        return rng.randint(0, task.budget.tokens or 0)

    async def candidate(cid: str) -> None:
        inner = ScriptedRuntime(root, Behaviour(tokens=spend, cost=rng.random() * _BIGGEST_CALL))
        guard = _GuardedRuntime(inner, ledger, cid, {})
        # Up to the slice's tries plus two infra retries, as the agent loop allows.
        for _call in range(plan.slice.tries + 2):
            await asyncio.sleep(rng.random() / 1000)
            try:
                await guard.run_task(_task(tokens=plan.slice.tokens))
            except LedgerExhausted:
                return

    await asyncio.gather(*(candidate(c) for c in "abc"[: plan.n]))


def test_property_the_shared_ledger_never_exceeds_its_limits(tmp_path: Path) -> None:
    rng = random.Random(42)
    for _ in range(200):
        budget = Budget(tries=rng.randint(1, 5), tokens=rng.randint(50, 500))
        max_cost = rng.choice([None, 0.5, 1.0])
        plan = plan_budget(budget, requested=rng.randint(2, 3), max_cost=max_cost)
        ledger = CostLedger(plan.limits)
        asyncio.run(_simulate(plan, ledger, rng, tmp_path))
        assert ledger.total.tries <= plan.limits.tries
        assert plan.limits.tokens is not None and ledger.total.tokens <= plan.limits.tokens
        if plan.limits.cost is not None:
            # A task carries no cost cap: the calls already running may pass it, no more.
            assert ledger.total.cost <= plan.limits.cost + plan.n * _BIGGEST_CALL
        assert ledger.held_tokens == 0
        assert ledger.total.tries == sum(s.tries for s in ledger.accounts.values())


# --- the executor: one budget for every candidate (real git workspaces) ---------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _run(executor, rl, inst, journal: Journal | None = None):  # type: ignore[no-untyped-def]
    ctx = (journal or Journal()).ctx()
    return asyncio.run(executor.execute_checked(rl, inst, ctx))


def _result(payload: dict[str, object]) -> BestOfNResult:
    return BestOfNResult.model_validate(payload["best_of"])


@needs_git
def test_a_candidate_the_ledger_cannot_pay_for_is_cancelled_and_the_best_finished_wins(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path / "repo")
    # tries 3 x 1.5 = 4 shared tries, 2 per candidate. `a` crashes twice (infra retries
    # cost ledger tries too) and passes on its third call; `b` fails its first try and
    # its retry finds the ledger empty.
    factory = Factory(
        {
            "a": Behaviour(content="a\n", raise_calls=2),
            "b": Behaviour(content="b\n", lint=(check("fail"), check("pass"))),
        }
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2, max_parallel=1)
    outcome = _run(executor, rule(Budget(tries=3)), instance())
    result = _result(outcome.payload)
    assert outcome.ok, outcome.message
    assert result.winner == "a" and result.limited_by == "tries"
    by_id = {c.id: c for c in result.candidates}
    assert by_id["a"].status == "passed" and by_id["a"].spend.tries == 3
    assert by_id["b"].status == "cancelled" and by_id["b"].spend.tries == 1
    assert "shared budget is spent" in by_id["b"].message
    assert result.spend.tries == 4 == result.plan.limits.tries
    assert result.selection is not None and result.selection.decided_by == "only"


@needs_git
def test_a_candidate_that_cannot_start_is_skipped(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="a\n", raise_calls=2, lint=(check("fail"),)),
            "b": Behaviour(content="b\n"),
            "c": Behaviour(content="c\n"),
        }
    )
    # tries 2 x 1.5 = 3: `a` takes all three (two crashes and a failing try).
    executor, _ = best_of(root, tmp_path / "t", factory, n=2, max_parallel=1)
    outcome = _run(executor, rule(Budget(tries=2)), instance())
    result = _result(outcome.payload)
    by_id = {c.id: c for c in result.candidates}
    assert by_id["b"].status == "skipped" and by_id["b"].spend.tries == 0
    assert not outcome.ok and result.status == "failed"
    assert result.selection is not None and result.selection.none_passed
    assert result.branch is None


@needs_git
def test_a_token_overrun_stops_the_others_and_the_best_finished_wins(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            # `a` ignores its token cap (75) and reports 140; it passes.
            "a": Behaviour(content="a\n", tokens=140),
            # `b` keeps to its cap: the 10 tokens left; it fails and its retry is refused.
            "b": Behaviour(
                content="b\n",
                tokens=lambda task: task.budget.tokens or 0,
                lint=(check("fail"), check("pass")),
            ),
        }
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2, max_parallel=1)
    outcome = _run(executor, rule(Budget(tries=3, tokens=100)), instance())
    result = _result(outcome.payload)
    assert outcome.ok and result.winner == "a"
    assert result.limited_by == "tokens"
    assert factory.runtimes["b"].tasks[0].budget.tokens == 10
    assert {c.id: c.status for c in result.candidates} == {"a": "passed", "b": "cancelled"}
    assert result.spend.tokens == 150


@needs_git
def test_n_drops_to_a_plain_run_when_only_one_fits(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory({})
    executor, plain = best_of(root, tmp_path / "t", factory, n=3)
    outcome = _run(executor, rule(Budget(tries=1)), instance())
    result = _result(outcome.payload)
    assert outcome.ok and plain.calls == [instance().instance_id]
    assert result.status == "plain" and result.plan.n == 1 and result.plan.capped_by == "budget"
    assert "a plain run" in result.message
    assert factory.envs == []  # no candidate, no workspace


@needs_git
@pytest.mark.parametrize("seed", range(6))
def test_property_a_best_of_run_never_spends_more_than_the_shared_budget(
    tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    root = repo(tmp_path / "repo")
    budget = Budget(tries=rng.randint(2, 4), tokens=rng.randint(100, 400))

    def spend(task: AgentTask) -> int:
        return rng.randint(1, task.budget.tokens or 1)

    behaviours = {
        cid: Behaviour(
            content=f"{cid}\n",
            tokens=spend,
            cost=0.01,
            raise_calls=rng.randint(0, 1),
            lint=tuple(
                check(rng.choice(["pass", "fail"]), warnings=rng.randint(0, 2)) for _ in range(3)
            ),
        )
        for cid in "abc"
    }
    executor, _ = best_of(
        root,
        tmp_path / "t",
        Factory(behaviours),
        n=rng.randint(2, 3),
        max_cost=0.05,
        max_parallel=rng.choice([1, 3]),
    )
    outcome = _run(executor, rule(budget), instance())
    result = _result(outcome.payload)
    limits = result.plan.limits
    assert result.spend.tries <= limits.tries
    assert limits.tokens is not None and result.spend.tokens <= limits.tokens
    assert sum(c.spend.tries for c in result.candidates) + result.critic_spend.tries == (
        result.spend.tries
    )
    if outcome.ok:
        winner = next(c for c in result.candidates if c.id == result.winner)
        assert winner.status == "passed"


@needs_git
def test_an_unguarded_runtime_is_charged_from_its_agent_turns(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="a\n", tokens=11, lint=(check("fail"), check("pass"))),
            "b": Behaviour(content="b\n", tokens=7),
        },
        guard=False,
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2)
    result = _result(_run(executor, rule(Budget(tries=3)), instance()).payload)
    by_id = {c.id: c.spend for c in result.candidates}
    assert (by_id["a"].tries, by_id["a"].tokens) == (2, 22)
    assert (by_id["b"].tries, by_id["b"].tokens) == (1, 7)
    assert result.spend.tries == 3 and result.winner == "a"
    assert "variant" not in factory.runtimes["a"].tasks[0].context
