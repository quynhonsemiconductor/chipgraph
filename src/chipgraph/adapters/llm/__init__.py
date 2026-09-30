"""LLM providers for the API runtime, and the guard every call goes through.

- `FakeProvider` (`fake`): deterministic scripted replies for tests and CI; never a network call.
- `AnthropicProvider` (`anthropic`, `anthropic-compatible`): the Anthropic Messages API, at
  Anthropic or at another `base_url` (e.g. GLM); needs the extra `chipgraph[llm]`.
- `GuardedProvider`: wraps any provider with the 'nda' rule, a run `Ledger` (token and cost
  accounting against a `RunBudget`) and retry with backoff (`RetryPolicy`).
"""

from __future__ import annotations

from chipgraph.adapters.llm._budget import (
    DEFAULT_PRICES,
    Ledger,
    ModelPrice,
    ModelUsage,
    RunBudget,
    Usage,
    price_for,
)
from chipgraph.adapters.llm._errors import (
    BudgetExceeded,
    LlmError,
    NdaBlocked,
    ProviderConfigError,
    ProviderError,
    RetriesExhausted,
)
from chipgraph.adapters.llm._nda import ensure_nda_allowed
from chipgraph.adapters.llm._retry import RetryPolicy, call_with_retry, is_retryable
from chipgraph.adapters.llm.anthropic import AnthropicProvider, anthropic_compatible
from chipgraph.adapters.llm.fake import FakeProvider, count_tokens
from chipgraph.adapters.llm.guard import GuardedProvider

__all__ = [
    "DEFAULT_PRICES",
    "AnthropicProvider",
    "BudgetExceeded",
    "FakeProvider",
    "GuardedProvider",
    "Ledger",
    "LlmError",
    "ModelPrice",
    "ModelUsage",
    "NdaBlocked",
    "ProviderConfigError",
    "ProviderError",
    "RetriesExhausted",
    "RetryPolicy",
    "RunBudget",
    "Usage",
    "anthropic_compatible",
    "call_with_retry",
    "count_tokens",
    "ensure_nda_allowed",
    "is_retryable",
    "price_for",
]
