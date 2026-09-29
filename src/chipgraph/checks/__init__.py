"""Built-in checks: deterministic, no-external-tool verification of project conventions.

See DESIGN.md 7.3. Each check is registered under the `chipgraph.adapters.check` entry
point group (see `pyproject.toml`) and satisfies `chipgraph.core.plugin_api.Check`.
"""

from chipgraph.checks.filelist import FilelistCheck
from chipgraph.checks.generated import GeneratedCheck, stamp, verify
from chipgraph.checks.layout import LayoutCheck
from chipgraph.checks.naming import NamingCheck

BUILTIN_CHECKS: dict[str, type] = {
    "layout": LayoutCheck,
    "filelist": FilelistCheck,
    "generated": GeneratedCheck,
    "naming": NamingCheck,
}
"""Maps a check's registered name to its class, for entry-point-free wiring in tests."""

__all__ = [
    "BUILTIN_CHECKS",
    "FilelistCheck",
    "GeneratedCheck",
    "LayoutCheck",
    "NamingCheck",
    "stamp",
    "verify",
]
