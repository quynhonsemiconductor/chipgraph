"""The 'nda' rule: data labeled 'nda' only ever goes to a local (self-hosted) model."""

from __future__ import annotations

from chipgraph.adapters.llm._errors import NdaBlocked
from chipgraph.core.config.models import DataCfg
from chipgraph.core.plugin_api.types import LlmRequest


def ensure_nda_allowed(
    provider: str, local: bool, request: LlmRequest, data: DataCfg | None = None
) -> None:
    """Raise `NdaBlocked` if `request` carries 'nda' and may not go to `provider`.

    A request labeled 'nda' is allowed only when the provider is local and, when a
    profile's `data` section is given, its `nda_model` is 'local' (DESIGN 9, 12.6).
    """
    if "nda" not in request.labels:
        return
    if not local:
        raise NdaBlocked(
            f"request is labeled 'nda' and provider {provider!r} is not local; 'nda' data "
            "only goes to a self-hosted model (set local: 'true' on such a provider)"
        )
    if data is not None and data.nda_model != "local":
        raise NdaBlocked(
            f"request is labeled 'nda' and the profile sets data.nda_model to "
            f"{data.nda_model!r}; set it to 'local' to send 'nda' data to local provider "
            f"{provider!r}"
        )
