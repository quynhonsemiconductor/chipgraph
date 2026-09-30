"""Token and cost accounting for a run: prices, the `Usage` summary, and the `Ledger`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.llm._errors import BudgetExceeded
from chipgraph.core.contracts import Budget
from chipgraph.core.plugin_api.types import LlmResponse


class ModelPrice(BaseModel):
    """What a model costs, in USD per million input and output tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: float = Field(ge=0, description="USD per 1M input tokens.")
    output_per_mtok: float = Field(ge=0, description="USD per 1M output tokens.")

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """The cost of one call with these token counts."""
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


DEFAULT_PRICES: Mapping[str, ModelPrice] = {
    "claude-opus-4-5": ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-opus-4-1": ModelPrice(input_per_mtok=15.0, output_per_mtok=75.0),
    "claude-opus-4": ModelPrice(input_per_mtok=15.0, output_per_mtok=75.0),
    "claude-sonnet-4-5": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-sonnet-4": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0),
    "claude-3-5-haiku": ModelPrice(input_per_mtok=0.8, output_per_mtok=4.0),
}
"""Base list prices (no prompt caching, no batch discount) of the Claude models known when
this table was written. Newer models, GLM and self-hosted models are not listed: pass a
table of your own (e.g. `{**DEFAULT_PRICES, "glm-x": ModelPrice(...)}`) to price them."""

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def price_for(model: str, prices: Mapping[str, ModelPrice]) -> ModelPrice | None:
    """The price of `model`: an exact entry, else the entry without a `-YYYYMMDD` suffix."""
    if model in prices:
        return prices[model]
    return prices.get(_DATE_SUFFIX.sub("", model))


class ModelUsage(BaseModel):
    """Usage of one model within a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: int = Field(default=0, ge=0, description="Completed calls.")
    input_tokens: int = Field(default=0, ge=0, description="Input tokens of those calls.")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens of those calls.")
    cost_usd: float | None = Field(
        default=0.0, description="Cost in USD, or None when the model has no known price."
    )


class Usage(BaseModel):
    """A running summary of a run's LLM usage: calls, tokens and cost, in total and per model.

    `cost_usd` sums the calls whose model has a known price; `unpriced_calls` counts the
    others, whose tokens are still counted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    calls: int = Field(default=0, ge=0, description="Completed calls.")
    input_tokens: int = Field(default=0, ge=0, description="Input tokens of all calls.")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens of all calls.")
    cost_usd: float = Field(default=0.0, ge=0, description="Cost of the priced calls, in USD.")
    unpriced_calls: int = Field(default=0, ge=0, description="Calls of models with no price.")
    per_model: dict[str, ModelUsage] = Field(default={}, description="Usage by model id.")

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def add(
        self, model: str, input_tokens: int, output_tokens: int, price: ModelPrice | None
    ) -> Usage:
        """A new summary with one more call of `model` added."""
        cost = price.cost_usd(input_tokens, output_tokens) if price is not None else None
        before = self.per_model.get(model, ModelUsage())
        after = ModelUsage(
            calls=before.calls + 1,
            input_tokens=before.input_tokens + input_tokens,
            output_tokens=before.output_tokens + output_tokens,
            cost_usd=None if cost is None or before.cost_usd is None else before.cost_usd + cost,
        )
        return Usage(
            calls=self.calls + 1,
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
            cost_usd=self.cost_usd + (cost or 0.0),
            unpriced_calls=self.unpriced_calls + (cost is None),
            per_model={**self.per_model, model: after},
        )


class RunBudget(BaseModel):
    """Limits for a run: total tokens and/or USD. A limit left as None is not enforced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens: int | None = Field(default=None, ge=1, description="Input + output tokens.")
    max_usd: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="Cost of priced calls, USD."
    )

    @classmethod
    def from_budget(cls, budget: Budget) -> Self:
        """The run budget of a rule's `Budget` (its `tokens` cap)."""
        return cls(max_tokens=budget.tokens)


class Ledger:
    """Accounts every response of a run against its `RunBudget`.

    One ledger may be shared by several providers so that they spend one budget. The
    check runs before a call starts: once the budget is spent, no further call starts.
    A call already in flight is still accounted, so the last call may overshoot a limit.
    Calls of unpriced models count towards `max_tokens` but not `max_usd`.
    """

    def __init__(
        self,
        budget: RunBudget | None = None,
        prices: Mapping[str, ModelPrice] | None = None,
    ) -> None:
        self.budget = budget if budget is not None else RunBudget()
        self.prices: Mapping[str, ModelPrice] = prices if prices is not None else DEFAULT_PRICES
        self._usage = Usage()

    @property
    def usage(self) -> Usage:
        """The usage so far."""
        return self._usage

    def check(self) -> None:
        """Raise `BudgetExceeded` if the budget is already spent."""
        max_tokens, max_usd = self.budget.max_tokens, self.budget.max_usd
        used = self._usage
        if max_tokens is not None and used.total_tokens >= max_tokens:
            raise BudgetExceeded(
                f"token budget spent: {used.total_tokens} of {max_tokens} tokens used "
                f"in {used.calls} calls"
            )
        if max_usd is not None and used.cost_usd >= max_usd:
            raise BudgetExceeded(
                f"USD budget spent: ${used.cost_usd:.4f} of ${max_usd:.4f} used "
                f"in {used.calls} calls"
            )

    def record(self, response: LlmResponse) -> Usage:
        """Account `response` and return the new usage."""
        price = price_for(response.model, self.prices)
        self._usage = self._usage.add(
            response.model, response.input_tokens, response.output_tokens, price
        )
        return self._usage
