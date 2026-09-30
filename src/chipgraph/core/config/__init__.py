"""Layered configuration: the project `Profile`, personal `UserConfig`, and their loader.

`chipgraph.core.config` knows nothing about chips or tools: no project, bus, PDK or tool
names appear here. See DESIGN.md 8.3, 8.4, 8.6 and 12.6.
"""

from chipgraph.core.config.defaults import DEFAULT_PROFILE
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import (
    ConfigIssue,
    ResolvedProfile,
    SourceFetcher,
    SourceRef,
    builtin_data_dir,
    find_profile,
    load,
    resolve_data_ref,
)
from chipgraph.core.config.models import (
    AdapterCfg,
    BlockOverride,
    BlockSpecOverride,
    DataCfg,
    DecisionsCfg,
    EnvCfg,
    Level,
    ModelsCfg,
    NamingCfg,
    PathRule,
    PolicyCfg,
    Profile,
    RequirementsCfg,
    SourceCfg,
    SpecCfg,
    StateCfg,
    StyleCfg,
    TargetCfg,
    TemplatesCfg,
    UserConfig,
)

__all__ = [
    "DEFAULT_PROFILE",
    "AdapterCfg",
    "BlockOverride",
    "BlockSpecOverride",
    "ConfigError",
    "ConfigIssue",
    "DataCfg",
    "DecisionsCfg",
    "EnvCfg",
    "Level",
    "ModelsCfg",
    "NamingCfg",
    "PathRule",
    "PolicyCfg",
    "Profile",
    "RequirementsCfg",
    "ResolvedProfile",
    "SourceCfg",
    "SourceFetcher",
    "SourceRef",
    "SpecCfg",
    "StateCfg",
    "StyleCfg",
    "TargetCfg",
    "TemplatesCfg",
    "UserConfig",
    "builtin_data_dir",
    "find_profile",
    "load",
    "resolve_data_ref",
]
