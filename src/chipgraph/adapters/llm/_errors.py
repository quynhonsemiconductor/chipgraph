"""Errors raised by LLM providers and by the `GuardedProvider` wrapper."""

from __future__ import annotations


class LlmError(Exception):
    """Base class of every error raised by the LLM adapters."""


class ProviderConfigError(LlmError):
    """A provider cannot run as configured: missing extra, missing key variable, bad option."""


class ProviderError(LlmError):
    """A provider call failed.

    `retryable` is true for transient failures only (HTTP 429 and 5xx, timeouts,
    connection errors); a 4xx other than 429 is never retryable. `retry_after_s` carries
    the server's `retry-after` hint, if it sent one.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after_s = retry_after_s


class RetriesExhausted(LlmError):
    """Every try of a call failed with a retryable error; `last_error` is the final one."""

    def __init__(self, message: str, *, tries: int, last_error: BaseException) -> None:
        super().__init__(message)
        self.tries = tries
        self.last_error = last_error


class NdaBlocked(LlmError):
    """A request labeled 'nda' was refused before any call, because the model is not local."""


class BudgetExceeded(LlmError):
    """The run's token or USD budget is spent; the call was not started."""
