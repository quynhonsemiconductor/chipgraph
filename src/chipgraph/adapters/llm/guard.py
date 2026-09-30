"""`GuardedProvider`: the nda rule, the budget and retry, wrapped around any provider."""

from __future__ import annotations

import asyncio
import random

from chipgraph.adapters.llm._budget import Ledger, Usage
from chipgraph.adapters.llm._nda import ensure_nda_allowed
from chipgraph.adapters.llm._retry import Rand, RetryPolicy, Sleep, call_with_retry
from chipgraph.core.config.models import DataCfg
from chipgraph.core.plugin_api.protocols import LlmProvider
from chipgraph.core.plugin_api.types import LlmRequest, LlmResponse


class GuardedProvider:
    """Wraps an `LlmProvider` with the checks every API-runtime call needs.

    For each `complete`, in order:

    1. **nda**: a request labeled 'nda' is refused with `NdaBlocked`, before any call,
       unless `inner.local` is true and (when `data` is given) `data.nda_model` is 'local'.
    2. **budget**: before each try, `ledger.check()` raises `BudgetExceeded` if the run's
       budget is spent, so the call does not start.
    3. **retry**: transient failures (429, 5xx, timeouts, connection errors) are retried
       under `retry` with exponential backoff and jitter; any other failure propagates at
       once, and `RetriesExhausted` is raised when every try failed.
    4. **accounting**: the response's tokens (and cost, when the model has a price) are
       added to `ledger.usage`.

    It satisfies `LlmProvider` itself, with the inner provider's `name` and `local`.
    """

    def __init__(
        self,
        inner: LlmProvider,
        *,
        ledger: Ledger | None = None,
        data: DataCfg | None = None,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        rand: Rand = random.random,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.local = inner.local
        self.ledger = ledger if ledger is not None else Ledger()
        self.data = data
        self.retry = retry if retry is not None else RetryPolicy()
        self._sleep = sleep
        self._rand = rand

    @property
    def usage(self) -> Usage:
        """The usage so far of this provider's ledger."""
        return self.ledger.usage

    async def complete(self, request: LlmRequest) -> LlmResponse:
        ensure_nda_allowed(self.inner.name, self.inner.local, request, self.data)
        response = await call_with_retry(
            lambda: self.inner.complete(request),
            self.retry,
            sleep=self._sleep,
            rand=self._rand,
            before_try=self.ledger.check,
        )
        self.ledger.record(response)
        return response
