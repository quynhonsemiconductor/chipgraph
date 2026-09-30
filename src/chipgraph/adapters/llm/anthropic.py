"""`AnthropicProvider`: the Anthropic Messages API, for `anthropic` and `anthropic-compatible`.

`anthropic-compatible` is the same Messages API served at another `base_url`, e.g. GLM's
Anthropic-compatible endpoint or a self-hosted gateway (DECISIONS D24). The SDK is the
optional extra `chipgraph[llm]` and is imported only when the first call is made.

Configuration comes from `models.providers.<name>` in the profile, all values strings:

- `base_url`: the API endpoint; required for `anthropic-compatible`. For `anthropic` it
  defaults to Anthropic's own endpoint (the `ANTHROPIC_BASE_URL` variable is not read).
- `api_key_env`: the *name* of the environment variable holding the key, never the key
  itself. Defaults to `ANTHROPIC_API_KEY` for `anthropic`; required for
  `anthropic-compatible`, so an Anthropic key is never sent to another endpoint by default.
- `auth`: `x-api-key` (default) sends the key as `x-api-key`; `bearer` as
  `Authorization: Bearer`.
- `local`: `"true"` only for a self-hosted endpoint; only then may it see 'nda' data.
- `timeout_s`: request timeout in seconds (default 600).

The SDK's own retries are turned off: retry belongs to `GuardedProvider`.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Mapping
from types import ModuleType
from typing import TYPE_CHECKING, Any

from chipgraph.adapters.llm._errors import LlmError, ProviderConfigError, ProviderError
from chipgraph.adapters.llm._nda import ensure_nda_allowed
from chipgraph.core.config.models import ModelsCfg
from chipgraph.core.plugin_api.types import LlmRequest, LlmResponse

if TYPE_CHECKING:
    import httpx2

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_TIMEOUT_S = 600.0

_KEYS = frozenset({"base_url", "api_key_env", "auth", "local", "timeout_s"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BOOLS = {"true": True, "false": False}
_AUTHS = frozenset({"x-api-key", "bearer"})


def _import_sdk() -> ModuleType:
    try:
        return importlib.import_module("anthropic")
    except ImportError as exc:
        raise ProviderConfigError(
            "the anthropic provider needs the 'anthropic' package, which is not installed; "
            "install the extra: pip install 'chipgraph[llm]' (or uv sync --extra llm)"
        ) from exc


class AnthropicProvider:
    """Completes `LlmRequest`s through the Anthropic Messages API (see the module docstring).

    Construction only validates the configuration; the SDK import, the `base_url` and key
    requirements are checked at the first `complete`, so an unconfigured instance (as the
    plugin registry makes) is harmless. `env` defaults to `os.environ`; `http_client` is
    passed to the SDK (tests inject one with a mock transport).
    """

    def __init__(
        self,
        config: Mapping[str, str] | None = None,
        *,
        name: str = "anthropic",
        compatible: bool = False,
        env: Mapping[str, str] | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        config = dict(config or {})
        unknown = sorted(set(config) - _KEYS)
        if unknown:
            raise ProviderConfigError(
                f"provider {name!r}: unknown option(s) {unknown}; known: {sorted(_KEYS)}"
            )
        self.name = name
        self.compatible = compatible
        self.base_url = config.get("base_url") or None
        key_env = config.get("api_key_env") or (None if compatible else DEFAULT_KEY_ENV)
        if key_env is not None and not _ENV_NAME.fullmatch(key_env):
            # Never echo the value: it may be a key pasted in by mistake.
            raise ProviderConfigError(
                f"provider {name!r}: api_key_env must be the name of an environment "
                "variable (letters, digits, '_'), not the key itself"
            )
        self.api_key_env = key_env
        self.auth = config.get("auth", "x-api-key")
        if self.auth not in _AUTHS:
            raise ProviderConfigError(
                f"provider {name!r}: auth must be one of {sorted(_AUTHS)}, got {self.auth!r}"
            )
        local = config.get("local", "false").strip().lower()
        if local not in _BOOLS:
            raise ProviderConfigError(
                f"provider {name!r}: local must be 'true' or 'false', got {local!r}"
            )
        self.local = _BOOLS[local]
        try:
            self.timeout_s = float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        except ValueError:
            raise ProviderConfigError(
                f"provider {name!r}: timeout_s must be a number of seconds"
            ) from None
        self._env = env
        self._http_client = http_client
        self._sdk: ModuleType | None = None
        self._client: Any = None

    @classmethod
    def from_models(
        cls,
        models: ModelsCfg,
        name: str,
        *,
        compatible: bool | None = None,
        env: Mapping[str, str] | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> AnthropicProvider:
        """The provider configured by `models.providers[name]`.

        `compatible` defaults to true for any name other than 'anthropic'.
        """
        return cls(
            models.providers.get(name, {}),
            name=name,
            compatible=name != "anthropic" if compatible is None else compatible,
            env=env,
            http_client=http_client,
        )

    def _get_client(self) -> tuple[ModuleType, Any]:
        if self._client is not None and self._sdk is not None:
            return self._sdk, self._client
        sdk = _import_sdk()
        where = f"models.providers.{self.name}"
        if self.compatible and self.base_url is None:
            raise ProviderConfigError(
                f"provider {self.name!r}: anthropic-compatible needs {where}.base_url"
            )
        if self.api_key_env is None:
            raise ProviderConfigError(
                f"provider {self.name!r}: set {where}.api_key_env to the name of the "
                "environment variable holding the API key"
            )
        env = self._env if self._env is not None else os.environ
        key = env.get(self.api_key_env, "")
        if not key:
            raise ProviderConfigError(
                f"provider {self.name!r}: environment variable {self.api_key_env} is not set; "
                f"it should hold the API key ({where}.api_key_env)"
            )
        credential = {"api_key": key} if self.auth == "x-api-key" else {"auth_token": key}
        self._client = sdk.AsyncAnthropic(
            **credential,
            base_url=self.base_url or ANTHROPIC_BASE_URL,
            timeout=self.timeout_s,
            max_retries=0,
            http_client=self._http_client,
        )
        self._sdk = sdk
        return sdk, self._client

    async def complete(self, request: LlmRequest) -> LlmResponse:
        ensure_nda_allowed(self.name, self.local, request)
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if not messages:
            raise LlmError(f"provider {self.name!r}: a request needs a user or assistant message")
        sdk, client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        try:
            message = await client.messages.create(**kwargs)
        except sdk.APIStatusError as exc:
            status = exc.status_code
            raise ProviderError(
                f"provider {self.name!r}: HTTP {status}: {exc.message}",
                retryable=status == 429 or status >= 500,
                status=status,
                retry_after_s=_retry_after(exc.response.headers.get("retry-after")),
            ) from exc
        except sdk.APIConnectionError as exc:  # includes APITimeoutError
            raise ProviderError(
                f"provider {self.name!r}: {type(exc).__name__}: {exc.message}", retryable=True
            ) from exc
        except sdk.AnthropicError as exc:
            raise ProviderError(f"provider {self.name!r}: {exc}", retryable=False) from exc
        return _to_response(message, request.model)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # an HTTP date: ignored, the backoff applies
    return seconds if seconds >= 0 else None


def _to_response(message: Any, requested_model: str) -> LlmResponse:
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = message.usage
    # Cache writes and reads are input tokens too; count them all against the budget.
    input_tokens = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    return LlmResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=usage.output_tokens,
        model=message.model or requested_model,
    )


def anthropic_compatible(
    config: Mapping[str, str] | None = None,
    *,
    name: str = "anthropic-compatible",
    env: Mapping[str, str] | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> AnthropicProvider:
    """An `AnthropicProvider` for an Anthropic-compatible endpoint (the entry-point factory)."""
    return AnthropicProvider(config, name=name, compatible=True, env=env, http_client=http_client)
