"""The built-in roles: one human-readable file each under `data/` (DESIGN.md 5.1, D8).

`get_role(id)` and `list_roles()` read them once, through `importlib.resources`, so the
same files are used from a checkout and from an installed wheel.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from pathlib import Path

from chipgraph.core.runtime.roles.spec import RoleError, RoleSpec, parse_role_file


def roles_dir() -> Path:
    """The directory of the built-in role files."""
    root = resources.files("chipgraph.core.runtime.roles") / "data"
    if not isinstance(root, Path) or not root.is_dir():
        raise RoleError("the built-in role files are not on a real filesystem")
    return root


@cache
def _roles() -> dict[str, RoleSpec]:
    roles: dict[str, RoleSpec] = {}
    for path in sorted(roles_dir().glob("*.md")):
        role = parse_role_file(path)
        roles[role.id] = role
    return roles


def list_roles() -> tuple[RoleSpec, ...]:
    """Every built-in role, sorted by id."""
    return tuple(_roles()[key] for key in sorted(_roles()))


def find_role(role_id: str) -> RoleSpec | None:
    """The role `role_id` (a `<namespace>/` prefix is ignored), or `None` if unknown."""
    return _roles().get(role_id.rsplit("/", 1)[-1])


def get_role(role_id: str) -> RoleSpec:
    """The role `role_id`; raises `RoleError` naming the known roles if there is none."""
    role = find_role(role_id)
    if role is None:
        known = ", ".join(sorted(_roles()))
        raise RoleError(f"unknown role {role_id!r}; the roles are: {known}")
    return role


__all__ = ["find_role", "get_role", "list_roles", "roles_dir"]
