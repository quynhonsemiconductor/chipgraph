"""`FakeProvider`: a deterministic, scripted LLM provider for tests and CI (no network)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from chipgraph.adapters.llm._errors import LlmError, ProviderError
from chipgraph.adapters.llm._nda import ensure_nda_allowed
from chipgraph.core.plugin_api.types import LlmRequest, LlmResponse

Script = Iterable[str] | Callable[[LlmRequest], str]
"""Either a queue of reply texts, used in order, or a function of the request."""


def count_tokens(text: str) -> int:
    """The fake's token count: the number of whitespace-separated words."""
    return len(text.split())


def _echo(request: LlmRequest) -> str:
    return request.messages[-1].content


class FakeProvider:
    """Replies from a script, counts tokens as words, and can fail on purpose.

    - `script`: a queue of reply texts (an `LlmError` is raised once it runs out) or a
      function of the request. By default it echoes the last message.
    - `fail`: the first `fail` calls raise a `ProviderError`, retryable (HTTP 503) when
      `retryable` is true, fatal (HTTP 400) otherwise.
    - `local`: whether to act as a self-hosted model, the only kind that may see 'nda'.
    - `model`: the model id reported in responses; by default the requested one.

    Input tokens are the words of every message, output tokens the words of the reply.
    Every request received (failed ones included) is kept in `requests`.
    """

    def __init__(
        self,
        script: Script | None = None,
        *,
        name: str = "fake",
        local: bool = False,
        fail: int = 0,
        retryable: bool = True,
        model: str | None = None,
    ) -> None:
        self.name = name
        self.local = local
        self.requests: list[LlmRequest] = []
        self._fail = fail
        self._retryable = retryable
        self._model = model
        self._reply: Callable[[LlmRequest], str]
        if script is None:
            self._reply = _echo
        elif callable(script):
            self._reply = script
        else:
            self._queue = deque(script)
            self._reply = self._next_in_queue

    def _next_in_queue(self, request: LlmRequest) -> str:
        if not self._queue:
            raise LlmError(f"fake provider {self.name!r}: the scripted replies ran out")
        return self._queue.popleft()

    async def complete(self, request: LlmRequest) -> LlmResponse:
        ensure_nda_allowed(self.name, self.local, request)
        self.requests.append(request)
        if self._fail > 0:
            self._fail -= 1
            status = 503 if self._retryable else 400
            kind = "retryable" if self._retryable else "fatal"
            raise ProviderError(
                f"fake provider {self.name!r}: scripted {kind} failure (HTTP {status})",
                retryable=self._retryable,
                status=status,
            )
        text = self._reply(request)
        return LlmResponse(
            text=text,
            input_tokens=sum(count_tokens(m.content) for m in request.messages),
            output_tokens=count_tokens(text),
            model=self._model or request.model,
        )
