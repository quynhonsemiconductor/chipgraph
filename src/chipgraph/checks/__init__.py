"""Built-in checks: deterministic, no-external-tool verification of project conventions.

See DESIGN.md 7.3. Each check is registered under the `chipgraph.adapters.check` entry
point group (see `pyproject.toml`) and satisfies `chipgraph.core.plugin_api.Check`.
"""

from chipgraph.checks.connect import ConnectCheck
from chipgraph.checks.cross_chip import CrossChipCheck
from chipgraph.checks.duplicate import DuplicateCheck
from chipgraph.checks.filelist import FilelistCheck
from chipgraph.checks.generated import GeneratedCheck, stamp, verify
from chipgraph.checks.hardcode import HardcodeCheck
from chipgraph.checks.layout import LayoutCheck
from chipgraph.checks.naming import NamingCheck
from chipgraph.checks.ports_diff import PortsDiffCheck
from chipgraph.checks.spec_schema import SpecSchemaCheck
from chipgraph.checks.trace import TraceCheck

BUILTIN_CHECKS: dict[str, type] = {
    "layout": LayoutCheck,
    "filelist": FilelistCheck,
    "generated": GeneratedCheck,
    "naming": NamingCheck,
    "spec_schema": SpecSchemaCheck,
    "cross_chip": CrossChipCheck,
    "ports_diff": PortsDiffCheck,
    "duplicate": DuplicateCheck,
    "trace": TraceCheck,
    "connect": ConnectCheck,
    "hardcode": HardcodeCheck,
}
"""Maps a check's registered name to its class, for entry-point-free wiring in tests."""

__all__ = [
    "BUILTIN_CHECKS",
    "ConnectCheck",
    "CrossChipCheck",
    "DuplicateCheck",
    "FilelistCheck",
    "GeneratedCheck",
    "HardcodeCheck",
    "LayoutCheck",
    "NamingCheck",
    "PortsDiffCheck",
    "SpecSchemaCheck",
    "TraceCheck",
    "stamp",
    "verify",
]
