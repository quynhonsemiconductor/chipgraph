"""Roles and skills as data (DESIGN.md 5.1, 5.4; D8).

- `RoleSpec` (`spec`): one role's prompt and tool table: capabilities, write scope,
  read policy, model tier and escalation tier. The built-in roles are files under
  `data/` (`registry`: `get_role`, `list_roles`).
- `SkillSpec`, `load_skills` (`skills`): skills the packs provide, checked per role.
- `policy`: the tool table applied to rules (`check_agent_rules`), to a task's model
  tiers (`model_ladder`) and to what its agent may read (`denied_reads`).

Generic: no harness, tool or project names. A runtime maps the abstract capabilities to
its own tools (runtime `claude-code`: `chipgraph.adapters.runtime.claude_code.agents`).
"""

from chipgraph.core.runtime.roles.policy import (
    LayoutValue,
    check_agent_rules,
    covers_denied,
    denied_reads,
    model_ladder,
    path_denied,
    static_prefix,
)
from chipgraph.core.runtime.roles.registry import find_role, get_role, list_roles, roles_dir
from chipgraph.core.runtime.roles.skills import (
    SkillError,
    SkillId,
    SkillSet,
    SkillSpec,
    load_skills,
    parse_skill_file,
)
from chipgraph.core.runtime.roles.spec import (
    CAPABILITIES,
    Capability,
    ReadPolicy,
    RoleError,
    RoleId,
    RoleSpec,
    WriteScope,
    parse_role_file,
    split_frontmatter,
)

__all__ = [
    "CAPABILITIES",
    "Capability",
    "LayoutValue",
    "ReadPolicy",
    "RoleError",
    "RoleId",
    "RoleSpec",
    "SkillError",
    "SkillId",
    "SkillSet",
    "SkillSpec",
    "WriteScope",
    "check_agent_rules",
    "covers_denied",
    "denied_reads",
    "find_role",
    "get_role",
    "list_roles",
    "load_skills",
    "model_ladder",
    "parse_role_file",
    "parse_skill_file",
    "path_denied",
    "roles_dir",
    "split_frontmatter",
    "static_prefix",
]
