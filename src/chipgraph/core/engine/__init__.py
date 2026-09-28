"""The engine: rule loading, the build graph, and staleness.

`chipgraph.core.engine` knows how rules become a graph of concrete instances and
how that graph's artifacts go stale when their inputs change (DESIGN 3.4, 6.3). It
does not run anything: the scheduler (M0-08) drives execution over this graph.
"""

from chipgraph.core.engine.gate import (
    GateError,
    GateEvaluator,
    GateStatus,
    approve,
    baseline,
    gate_id_for,
)
from chipgraph.core.engine.graph import (
    BuildGraph,
    ForeachResolver,
    GraphError,
    ProductionRecord,
    Staleness,
    StalenessState,
    StaticForeach,
    build_graph,
    compute_staleness,
    kind_for,
)
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.engine.rules import RuleLoadError, load_pack_rules, load_rule_file
from chipgraph.core.engine.scheduler import (
    AgentStub,
    CheckRunner,
    ExecOutcome,
    Executor,
    GateChecker,
    RunSummary,
    Scheduler,
    SchedulerError,
)

__all__ = [
    "AgentStub",
    "BuildGraph",
    "CheckRunner",
    "ExecOutcome",
    "Executor",
    "ForeachResolver",
    "GateChecker",
    "GateError",
    "GateEvaluator",
    "GateStatus",
    "GraphError",
    "ProductionRecord",
    "RecordStore",
    "RuleLoadError",
    "RunSummary",
    "Scheduler",
    "SchedulerError",
    "Staleness",
    "StalenessState",
    "StaticForeach",
    "approve",
    "baseline",
    "build_graph",
    "compute_staleness",
    "gate_id_for",
    "kind_for",
    "load_pack_rules",
    "load_rule_file",
]
