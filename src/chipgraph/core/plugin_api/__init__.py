"""The plugin API: protocols adapters implement, a registry, and pack loading.

`chipgraph.core.plugin_api` knows nothing about chips or tools: no project, bus, PDK or
tool names appear here. Concrete adapters live under `chipgraph.adapters`, which core
never imports.
"""

from chipgraph.core.plugin_api.pack import (
    Pack,
    PackManifest,
    PackProvides,
    PackRequires,
    discover_packs,
    load_pack,
    resolve_requires,
)
from chipgraph.core.plugin_api.protocols import (
    AgentRuntime,
    Check,
    Extractor,
    FormatAdapter,
    Generator,
    LlmProvider,
    LogParser,
    ReviewAdapter,
    Runner,
    ToolAdapter,
    VcsAdapter,
)
from chipgraph.core.plugin_api.registry import KINDS, PluginError, Registry
from chipgraph.core.plugin_api.types import (
    AgentTask,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    RunResult,
    ToolContext,
)

__all__ = [
    "KINDS",
    "AgentRuntime",
    "AgentTask",
    "Check",
    "Extractor",
    "FormatAdapter",
    "Generator",
    "LlmMessage",
    "LlmProvider",
    "LlmRequest",
    "LlmResponse",
    "LogParser",
    "Pack",
    "PackManifest",
    "PackProvides",
    "PackRequires",
    "PluginError",
    "Registry",
    "ReviewAdapter",
    "RunResult",
    "Runner",
    "ToolAdapter",
    "ToolContext",
    "VcsAdapter",
    "discover_packs",
    "load_pack",
    "resolve_requires",
]
