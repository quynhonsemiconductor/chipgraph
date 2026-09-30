"""Retry with exponential backoff and jitter, for transient provider failures only."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chipgraph.adapters.llm._errors import ProviderError, RetriesExhausted

Sleep = Callable[[float], Awaitable[None]]
"""An async sleep, `asyncio.sleep` by default; injectable for tests."""

Rand = Callable[[], float]
"""A uniform random number in [0, 1), `random.random` by default; injectable for tests."""


class RetryPolicy(BaseModel):
    """How many times to try a call and how long to wait between tries.

    The wait before retry `n` (1-based) is drawn from `[c/2, c]` with
    `c = min(max_delay_s, base_delay_s * 2 ** (n - 1))` ("equal jitter"), and never less
    than the server's `retry-after` hint (itself capped at `max_delay_s`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tries: int = Field(default=4, ge=1, description="Tries in total, the first included.")
    base_delay_s: float = Field(default=1.0, ge=0, description="Ceiling of the first wait.")
    max_delay_s: float = Field(default=30.0, ge=0, description="Largest wait between tries.")

    @model_validator(mode="after")
    def _check_delays(self) -> Self:
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be at least base_delay_s")
        return self

    def delay_s(self, retry: int, rand: float, retry_after_s: float | None = None) -> float:
        """The wait before retry number `retry` (1-based), given a random draw `rand`."""
        ceiling = min(self.max_delay_s, self.base_delay_s * 2.0 ** (retry - 1))
        delay = ceiling / 2 + rand * ceiling / 2
        if retry_after_s is not None:
            delay = max(delay, min(retry_after_s, self.max_delay_s))
        return delay


def is_retryable(exc: BaseException) -> bool:
    """True for transient failures: a retryable `ProviderError`, a timeout, a lost connection."""
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, TimeoutError | ConnectionError)


async def call_with_retry[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Sleep = asyncio.sleep,
    rand: Rand = random.random,
    before_try: Callable[[], None] | None = None,
) -> T:
    """Await `call()`, retrying transient failures under `policy`.

    `before_try` runs before every try (e.g. a budget check) and may raise to stop.
    A non-retryable error propagates at once; when every try failed with a retryable
    error, `RetriesExhausted` is raised from the last one.
    """
    for attempt in range(1, policy.max_tries + 1):
        if before_try is not None:
            before_try()
        try:
            return await call()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt == policy.max_tries:
                raise RetriesExhausted(
                    f"gave up after {attempt} tries: {exc}", tries=attempt, last_error=exc
                ) from exc
            retry_after = exc.retry_after_s if isinstance(exc, ProviderError) else None
            await sleep(policy.delay_s(attempt, rand(), retry_after))
    raise AssertionError("unreachable: max_tries >= 1")  # pragma: no cover
