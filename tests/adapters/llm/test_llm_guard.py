"""`GuardedProvider`: nda blocking, retry with backoff, budget stop, usage and cost."""

from __future__ import annotations

import asyncio

import pytest
from llm_helpers import SleepRecorder, make_request

from chipgraph.adapters.llm import (
    BudgetExceeded,
    FakeProvider,
    GuardedProvider,
    Ledger,
    ModelPrice,
    NdaBlocked,
    ProviderError,
    RetriesExhausted,
    RetryPolicy,
    RunBudget,
    call_with_retry,
    is_retryable,
    price_for,
)
from chipgraph.adapters.llm._budget import DEFAULT_PRICES
from chipgraph.core.config.models import DataCfg
from chipgraph.core.contracts import Budget
from chipgraph.core.plugin_api.protocols import LlmProvider

POLICY = RetryPolicy(max_tries=4, base_delay_s=1.0, max_delay_s=30.0)


def _guard(
    inner: FakeProvider,
    *,
    ledger: Ledger | None = None,
    data: DataCfg | None = None,
    sleep: SleepRecorder | None = None,
    rand: float = 0.0,
) -> GuardedProvider:
    return GuardedProvider(
        inner,
        ledger=ledger,
        data=data,
        retry=POLICY,
        sleep=sleep or SleepRecorder(),
        rand=lambda: rand,
    )


def test_is_an_llm_provider_with_the_inner_name_and_locality() -> None:
    guard = _guard(FakeProvider(name="glm", local=True))
    assert isinstance(guard, LlmProvider)
    assert (guard.name, guard.local) == ("glm", True)


# --- nda ---------------------------------------------------------------------------------


class _NoCall:
    """A provider that fails the test if it is ever called."""

    name = "cloud"
    local = False

    async def complete(self, request: object) -> object:
        raise AssertionError("the provider must not be called")


def test_nda_is_blocked_before_any_call_for_a_cloud_provider() -> None:
    guard = GuardedProvider(_NoCall())  # type: ignore[arg-type]
    with pytest.raises(NdaBlocked, match="'cloud' is not local"):
        asyncio.run(guard.complete(make_request(labels=("internal", "nda"))))
    assert guard.usage.calls == 0


def test_nda_is_allowed_for_a_local_provider() -> None:
    fake = FakeProvider(["local reply"], local=True)
    response = asyncio.run(_guard(fake).complete(make_request(labels=("nda",))))
    assert response.text == "local reply"


def test_nda_to_a_local_provider_follows_the_profile_nda_model() -> None:
    nda = make_request(labels=("nda",))
    blocked = _guard(FakeProvider(local=True), data=DataCfg(nda_model="block"))
    with pytest.raises(NdaBlocked, match="nda_model"):
        asyncio.run(blocked.complete(nda))
    allowed = _guard(FakeProvider(["ok"], local=True), data=DataCfg(nda_model="local"))
    assert asyncio.run(allowed.complete(nda)).text == "ok"


def test_requests_without_nda_go_to_a_cloud_provider() -> None:
    fake = FakeProvider(["ok"])
    assert asyncio.run(_guard(fake).complete(make_request(labels=("internal",)))).text == "ok"


# --- retry -------------------------------------------------------------------------------


def test_retryable_failures_then_success() -> None:
    fake = FakeProvider(["done"], fail=2, retryable=True)
    sleep = SleepRecorder()
    guard = _guard(fake, sleep=sleep)
    assert asyncio.run(guard.complete(make_request())).text == "done"
    assert len(fake.requests) == 3
    assert len(sleep.delays) == 2
    assert guard.usage.calls == 1


def test_a_fatal_failure_is_not_retried() -> None:
    fake = FakeProvider(["never"], fail=1, retryable=False)
    sleep = SleepRecorder()
    with pytest.raises(ProviderError) as info:
        asyncio.run(_guard(fake, sleep=sleep).complete(make_request()))
    assert info.value.status == 400
    assert len(fake.requests) == 1
    assert sleep.delays == []


def test_retries_are_exhausted() -> None:
    fake = FakeProvider(["never"], fail=10, retryable=True)
    sleep = SleepRecorder()
    with pytest.raises(RetriesExhausted) as info:
        asyncio.run(_guard(fake, sleep=sleep).complete(make_request()))
    assert info.value.tries == 4
    assert isinstance(info.value.last_error, ProviderError)
    assert len(fake.requests) == 4
    assert len(sleep.delays) == 3


@pytest.mark.parametrize(
    ("rand", "expected"),
    [
        (0.0, [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0]),
        (0.999999, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]),
    ],
)
def test_backoff_is_exponential_with_jitter_and_capped(rand: float, expected: list[float]) -> None:
    fake = FakeProvider(["ok"], fail=7, retryable=True)
    sleep = SleepRecorder()
    guard = GuardedProvider(
        fake,
        retry=RetryPolicy(max_tries=8, base_delay_s=1.0, max_delay_s=30.0),
        sleep=sleep,
        rand=lambda: rand,
    )
    asyncio.run(guard.complete(make_request()))
    assert sleep.delays == pytest.approx(expected, abs=1e-4)


def test_backoff_honours_retry_after_up_to_the_cap() -> None:
    assert POLICY.delay_s(1, 0.0, retry_after_s=7.0) == 7.0
    assert POLICY.delay_s(1, 0.0, retry_after_s=120.0) == 30.0
    assert POLICY.delay_s(3, 0.0, retry_after_s=0.1) == 2.0


def test_what_is_retryable() -> None:
    assert is_retryable(ProviderError("x", retryable=True, status=429))
    assert not is_retryable(ProviderError("x", retryable=False, status=404))
    assert is_retryable(TimeoutError())
    assert is_retryable(ConnectionResetError())
    assert not is_retryable(ValueError())


def test_call_with_retry_retries_timeouts() -> None:
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError
        return "ok"

    sleep = SleepRecorder()
    result = asyncio.run(call_with_retry(flaky, POLICY, sleep=sleep, rand=lambda: 0.0))
    assert result == "ok"
    assert sleep.delays == [0.5, 1.0]


# --- budget and usage --------------------------------------------------------------------


def test_budget_stops_before_the_call_once_tokens_are_spent() -> None:
    # Each call: 2 input words + 3 output words = 5 tokens.
    fake = FakeProvider(lambda _: "three word reply")
    ledger = Ledger(RunBudget(max_tokens=10))
    guard = _guard(fake, ledger=ledger)
    asyncio.run(guard.complete(make_request()))
    asyncio.run(guard.complete(make_request()))
    with pytest.raises(BudgetExceeded, match="10 of 10 tokens"):
        asyncio.run(guard.complete(make_request()))
    assert len(fake.requests) == 2
    assert guard.usage.total_tokens == 10


def test_budget_stops_on_usd() -> None:
    prices = {"m": ModelPrice(input_per_mtok=1_000_000.0, output_per_mtok=0.0)}
    ledger = Ledger(RunBudget(max_usd=3.0), prices)
    fake = FakeProvider(lambda _: "x")
    guard = _guard(fake, ledger=ledger)
    asyncio.run(guard.complete(make_request("a b", model="m")))  # $2
    asyncio.run(guard.complete(make_request("a b", model="m")))  # $4 -> spent
    with pytest.raises(BudgetExceeded, match="USD budget spent"):
        asyncio.run(guard.complete(make_request("a b", model="m")))
    assert len(fake.requests) == 2


def test_a_shared_ledger_spends_one_budget_across_providers() -> None:
    ledger = Ledger(RunBudget(max_tokens=5))
    first = _guard(FakeProvider(lambda _: "three word reply"), ledger=ledger)
    second_inner = FakeProvider(["unused"])
    second = _guard(second_inner, ledger=ledger)
    asyncio.run(first.complete(make_request()))
    with pytest.raises(BudgetExceeded):
        asyncio.run(second.complete(make_request()))
    assert second_inner.requests == []


def test_run_budget_from_a_rule_budget() -> None:
    assert RunBudget.from_budget(Budget(tokens=5000)) == RunBudget(max_tokens=5000)
    assert RunBudget.from_budget(Budget()) == RunBudget()


def test_cost_with_a_known_price() -> None:
    # 1000 input words, 200 output words at $2 / $10 per 1M tokens.
    fake = FakeProvider(lambda _: " ".join(["w"] * 200), model="claude-sonnet-5-5-20260401")
    guard = _guard(fake)
    asyncio.run(guard.complete(make_request(" ".join(["w"] * 1000))))
    usage = guard.usage
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 1000, 200)
    assert usage.cost_usd == pytest.approx(0.004)
    assert usage.unpriced_calls == 0
    per_model = usage.per_model["claude-sonnet-5-5-20260401"]
    assert per_model.cost_usd == pytest.approx(0.004)


def test_cost_of_an_unknown_model_is_unknown_but_tokens_count() -> None:
    fake = FakeProvider(lambda _: "one two")
    guard = _guard(fake)
    asyncio.run(guard.complete(make_request("a b c", model="glm-unknown")))
    asyncio.run(guard.complete(make_request("a b c", model="claude-haiku-5-5")))
    usage = guard.usage
    assert usage.calls == 2
    assert usage.total_tokens == 10
    assert usage.unpriced_calls == 1
    assert usage.per_model["glm-unknown"].cost_usd is None
    assert usage.per_model["glm-unknown"].input_tokens == 3
    assert usage.cost_usd == pytest.approx((3 * 0.5 + 2 * 2.5) / 1e6)


def test_price_lookup() -> None:
    assert (
        price_for("claude-opus-4-5-20251101", DEFAULT_PRICES) == DEFAULT_PRICES["claude-opus-4-5"]
    )
    assert price_for("claude-opus-4", DEFAULT_PRICES) == ModelPrice(
        input_per_mtok=15.0, output_per_mtok=75.0
    )
    assert price_for("claude-opus-4-9", DEFAULT_PRICES) is None
    # The current lineup (list prices of the Claude models page, checked 2026-10-09).
    assert price_for("claude-sonnet-5-5", DEFAULT_PRICES) == ModelPrice(
        input_per_mtok=2.0, output_per_mtok=10.0
    )
    assert price_for("claude-opus-5-5", DEFAULT_PRICES) == ModelPrice(
        input_per_mtok=4.0, output_per_mtok=20.0
    )
    assert price_for("claude-haiku-5-5", DEFAULT_PRICES) is not None
    custom = {"glm-x": ModelPrice(input_per_mtok=0.5, output_per_mtok=2.0)}
    assert price_for("glm-x", {**DEFAULT_PRICES, **custom}) == custom["glm-x"]


def test_failed_tries_are_not_accounted() -> None:
    fake = FakeProvider(["ok"], fail=1, retryable=True)
    guard = _guard(fake)
    asyncio.run(guard.complete(make_request()))
    assert guard.usage.calls == 1
