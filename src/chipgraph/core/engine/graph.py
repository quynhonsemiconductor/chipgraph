"""The build graph: rule instances, their dependency edges, and staleness.

A `BuildGraph` is built from a set of `RuleSpec` (see `rules.py`) and a `ForeachResolver`
that expands `foreach` selectors into concrete parameter sets. Rules become `RuleInstance`
nodes once every output template and input selector is substituted with concrete params;
edges follow from one instance's inputs matching another instance's outputs (DESIGN 3.4,
6.3).
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    InputSpec,
    RuleInstance,
    RuleSpec,
    Sha256,
)
from chipgraph.core.state.artifacts import hash_inputs

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

_KIND_BY_SUFFIX: dict[str, ArtifactKind] = {
    ".sv": "rtl",
    ".v": "rtl",
    ".svh": "rtl",
    ".vh": "rtl",
    ".py": "script",
    ".md": "doc",
    ".yml": "config",
    ".yaml": "config",
    ".toml": "config",
    ".json": "report",
    ".log": "report",
    ".xml": "report",
    ".svg": "diagram",
    ".png": "diagram",
    ".drawio": "diagram",
}


class GraphError(Exception):
    """Raised for any problem building or querying a `BuildGraph`."""


def kind_for(path: str) -> ArtifactKind:
    """Return the `ArtifactKind` for a file path, based on its extension."""
    suffix = PurePosixPath(path).suffix
    return _KIND_BY_SUFFIX.get(suffix, "other")


@runtime_checkable
class ForeachResolver(Protocol):
    """Expands a rule's `foreach` selector into concrete parameter sets.

    The real resolver (M1) queries the Design Model, e.g. `model.plan(block).modules`.
    """

    def expand(self, expr: str) -> list[dict[str, str]]:
        """Return one dict of param values per instance the selector expands to."""
        ...


class StaticForeach:
    """A `ForeachResolver` backed by a fixed mapping, for tests and simple builds."""

    def __init__(self, values: Mapping[str, list[dict[str, str]]]) -> None:
        self._values = values

    def expand(self, expr: str) -> list[dict[str, str]]:
        try:
            param_sets = self._values[expr]
        except KeyError:
            raise GraphError(f"no foreach values registered for {expr!r}") from None
        return [dict(params) for params in param_sets]


def _substitute(template: str, params: Mapping[str, str], *, rule_id: str) -> str:
    """Substitute every `{name}` placeholder in `template` with a value from `params`.

    Supports dotted names such as `{module.name}` by looking up that literal key in
    `params` (no nested attribute traversal). Raises `GraphError` naming the rule and
    the placeholder if a value is missing.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in params:
            return params[key]
        # A `{KEY}` placeholder whose lower-cased name is a param means "the upper-cased
        # value", so a layout convention like `TINY_{BLOCK}_MAS.md` resolves from a
        # `block` param (matching `ingest`'s `_fill`, DESIGN.md 4.x) without adding a
        # second param that would change instance ids.
        lowered = key.lower()
        if key.isupper() and lowered in params:
            return params[lowered].upper()
        raise GraphError(f"rule {rule_id!r}: no value for placeholder '{{{key}}}' in {template!r}")

    return _PLACEHOLDER_RE.sub(_replace, template)


def _ref_key(ref: ArtifactRef) -> str:
    """The producer/staleness lookup key for an `ArtifactRef`: `"<repo>:<path-or-model_key>"`."""
    locator = ref.path if ref.path is not None else ref.model_key
    return f"{ref.repo}:{locator}"


def _param_sets(
    rule: RuleSpec,
    resolver: ForeachResolver,
    base_params: Mapping[str, str] | None,
) -> list[dict[str, str]]:
    base = dict(base_params or {})
    if rule.foreach is None:
        return [dict(base)]
    return [{**base, **params} for params in resolver.expand(rule.foreach)]


def _parse_target(target: str) -> tuple[str, dict[str, str]]:
    """Split a `select()` target into a rule id and the (possibly empty) params filter."""
    if "[" not in target:
        return target, {}
    rule_id, _, rest = target.partition("[")
    if not rest.endswith("]"):
        raise GraphError(f"malformed target {target!r}: expected a closing ']'")
    body = rest[:-1]
    params: dict[str, str] = {}
    if body:
        for pair in body.split(","):
            key, sep, value = pair.partition("=")
            if not sep:
                raise GraphError(f"malformed target {target!r}: expected 'key=value' in {pair!r}")
            params[key.strip()] = value.strip()
    return rule_id, params


class BuildGraph:
    """An immutable build graph: rule instances, their dependency edges and producers."""

    def __init__(
        self,
        *,
        instances: dict[str, RuleInstance],
        rules: dict[str, RuleSpec],
        producers: dict[str, str],
        deps: dict[str, tuple[str, ...]],
        dependents: dict[str, tuple[str, ...]],
        external: frozenset[str],
        spec_fragments: dict[str, str],
    ) -> None:
        self.instances = MappingProxyType(dict(instances))
        self.rules = MappingProxyType(dict(rules))
        self.producers = MappingProxyType(dict(producers))
        self.spec_fragments = MappingProxyType(dict(spec_fragments))
        self._deps = MappingProxyType(dict(deps))
        self._dependents = MappingProxyType(dict(dependents))
        self._external = external

    def deps(self, instance_id: str) -> tuple[str, ...]:
        """The instance ids `instance_id` directly depends on."""
        return self._deps[instance_id]

    def dependents(self, instance_id: str) -> tuple[str, ...]:
        """The instance ids that directly depend on `instance_id`."""
        return self._dependents[instance_id]

    def topo_order(self) -> list[str]:
        """A deterministic topological order: among ready nodes, the lowest id goes first."""
        indegree = {iid: len(self._deps[iid]) for iid in self.instances}
        heap = sorted(iid for iid, count in indegree.items() if count == 0)
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            iid = heapq.heappop(heap)
            order.append(iid)
            for dependent in self._dependents[iid]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(heap, dependent)
        return order

    def ready(self, done: set[str], running: set[str] = frozenset()) -> list[str]:  # type: ignore[assignment]
        """Instances whose deps are all in `done`, that are not themselves done or running."""
        return sorted(
            iid
            for iid in self.instances
            if iid not in done
            and iid not in running
            and all(dep in done for dep in self._deps[iid])
        )

    def downstream(self, instance_id: str) -> set[str]:
        """Every instance transitively depending on `instance_id`."""
        return self._reachable(instance_id, self._dependents)

    def _upstream(self, instance_id: str) -> set[str]:
        """Every instance `instance_id` transitively depends on."""
        return self._reachable(instance_id, self._deps)

    @staticmethod
    def _reachable(start: str, edges: Mapping[str, tuple[str, ...]]) -> set[str]:
        seen: set[str] = set()
        stack = list(edges[start])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(edges[current])
        return seen

    def external_inputs(self) -> set[str]:
        """Ref keys for inputs no instance in this graph produces."""
        return set(self._external)

    def select(self, target: str) -> set[str]:
        """The instances needed for `target`, including all upstream dependencies.

        `target` is `"*"`, a rule id (`pack/name`), or a rule id with a params filter
        (`pack/name[block=timer]`); a params filter matches any instance whose params
        contain those key/value pairs (a subset match).
        """
        if target == "*":
            return set(self.instances)

        rule_id, params = _parse_target(target)
        if rule_id not in self.rules:
            raise GraphError(f"select: unknown rule id {rule_id!r} in target {target!r}")

        matched = {
            iid
            for iid, instance in self.instances.items()
            if instance.rule_id == rule_id and params.items() <= instance.params.items()
        }
        if not matched:
            raise GraphError(f"select: target {target!r} matched no instances")

        result = set(matched)
        for iid in matched:
            result |= self._upstream(iid)
        return result


def build_graph(
    rules: Iterable[RuleSpec],
    resolver: ForeachResolver,
    *,
    base_params: Mapping[str, str] | None = None,
    repo: str = ".",
) -> BuildGraph:
    """Build a `BuildGraph` from `rules`, expanding `foreach` with `resolver`.

    Raises `GraphError` for a missing placeholder value, two instances writing the
    same output (the write-set rule, F2), or a dependency cycle.
    """
    rules_by_id: dict[str, RuleSpec] = {}
    for rule in rules:
        if rule.id in rules_by_id:
            raise GraphError(f"duplicate rule id {rule.id!r}")
        rules_by_id[rule.id] = rule

    instances: dict[str, RuleInstance] = {}
    producers: dict[str, str] = {}
    spec_fragments: dict[str, str] = {}

    for rule in rules_by_id.values():
        for params in _param_sets(rule, resolver, base_params):
            output_refs = tuple(
                ArtifactRef(
                    repo=repo,
                    kind=kind_for(path := _substitute(template, params, rule_id=rule.id)),
                    path=path,
                )
                for template in rule.outputs
            )
            input_refs = tuple(
                _resolve_input(
                    input_spec, params, rule_id=rule.id, repo=repo, spec_fragments=spec_fragments
                )
                for input_spec in rule.inputs
            )

            instance_id = RuleInstance.make_id(rule.id, params)
            if instance_id in instances:
                raise GraphError(
                    f"duplicate instance id {instance_id!r}: 'foreach' for rule {rule.id!r} "
                    "produced the same params twice"
                )
            instances[instance_id] = RuleInstance(
                rule_id=rule.id,
                params=params,
                inputs=input_refs,
                outputs=output_refs,
                instance_id=instance_id,
            )

            for ref in output_refs:
                key = _ref_key(ref)
                if key in producers:
                    other = producers[key]
                    raise GraphError(
                        f"write-set conflict on {key!r}: both {other!r} and "
                        f"{instance_id!r} produce it"
                    )
                producers[key] = instance_id

    deps: dict[str, tuple[str, ...]] = {}
    dependents: dict[str, list[str]] = {iid: [] for iid in instances}
    external: set[str] = set()

    for iid, instance in instances.items():
        dep_ids: list[str] = []
        for ref in instance.inputs:
            key = _ref_key(ref)
            producer = producers.get(key)
            if producer is None:
                external.add(key)
                continue
            if producer == iid or producer in dep_ids:
                continue
            dep_ids.append(producer)
        deps[iid] = tuple(sorted(dep_ids))
        for producer in dep_ids:
            dependents[producer].append(iid)

    frozen_dependents = {iid: tuple(sorted(ids)) for iid, ids in dependents.items()}

    _check_cycles(instances, deps)

    return BuildGraph(
        instances=instances,
        rules=rules_by_id,
        producers=producers,
        deps=deps,
        dependents=frozen_dependents,
        external=frozenset(external),
        spec_fragments=spec_fragments,
    )


def _resolve_input(
    input_spec: InputSpec,
    params: Mapping[str, str],
    *,
    rule_id: str,
    repo: str,
    spec_fragments: dict[str, str],
) -> ArtifactRef:
    """Resolve one `InputSpec` (with `params` substituted) to an `ArtifactRef`.

    `model` inputs become a model-keyed ref; `spec` inputs split off the `#fragment`
    (kept in `spec_fragments`, keyed by the resulting ref key, for later use); `path`
    and `artifact` inputs become a path-keyed ref.
    """
    selector = _substitute(input_spec.selector, params, rule_id=rule_id)
    if input_spec.source == "model":
        return ArtifactRef(repo=repo, kind="model", model_key=selector)
    if input_spec.source == "spec":
        path, _, fragment = selector.partition("#")
        ref = ArtifactRef(repo=repo, kind="spec", path=path)
        if fragment:
            spec_fragments[_ref_key(ref)] = fragment
        return ref
    # source in ("path", "artifact")
    return ArtifactRef(repo=repo, kind=kind_for(selector), path=selector)


def _check_cycles(
    instances: Mapping[str, RuleInstance], deps: Mapping[str, tuple[str, ...]]
) -> None:
    """Raise `GraphError` naming the cycle, in order, if `deps` contains one."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(instances, WHITE)
    stack: list[str] = []

    def visit(iid: str) -> None:
        color[iid] = GRAY
        stack.append(iid)
        for dep in deps[iid]:
            if color[dep] == GRAY:
                cycle_start = stack.index(dep)
                cycle = [*stack[cycle_start:], dep]
                raise GraphError(f"cycle detected: {' -> '.join(cycle)}")
            if color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[iid] = BLACK

    for iid in sorted(instances):
        if color[iid] == WHITE:
            visit(iid)


# --- Staleness -------------------------------------------------------------------


class ProductionRecord(BaseModel):
    """What the scheduler records when an instance finishes: its hashes at that point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(description="The instance this record is for.")
    inputs_hash: Sha256 = Field(
        description="hash_inputs() over this instance's input ref keys and their hashes."
    )
    output_hashes: dict[str, Sha256] = Field(
        default_factory=dict, description="Ref key -> content hash, for each output produced."
    )


StalenessState = Literal["fresh", "stale", "never_built", "diverged"]


class Staleness(BaseModel):
    """The staleness state of one instance, with human-readable reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: StalenessState = Field(description="fresh, stale, never_built, or diverged.")
    reasons: tuple[str, ...] = Field(default=(), description="Readable reasons for this state.")


_PROPAGATING_STATES = ("stale", "never_built")


def compute_staleness(
    graph: BuildGraph,
    records: Mapping[str, ProductionRecord],
    current: Mapping[str, str],
) -> dict[str, Staleness]:
    """Compute the `Staleness` of every instance in `graph`.

    `records` is what the scheduler recorded the last time each instance finished;
    `current` maps ref key -> current content hash (a missing key means the file is
    absent). Processed in topological order, so a dependency's staleness is known
    before it is used to decide the dependent's (DESIGN 6.3: stale propagates down).
    """
    result: dict[str, Staleness] = {}

    for iid in graph.topo_order():
        instance = graph.instances[iid]
        record = records.get(iid)

        if record is None:
            result[iid] = Staleness(state="never_built", reasons=(f"{iid} was never built",))
            continue

        dep_problems = [dep for dep in graph.deps(iid) if result[dep].state in _PROPAGATING_STATES]
        if dep_problems:
            reasons = tuple(
                f"depends on {dep} which is {result[dep].state}" for dep in dep_problems
            )
            result[iid] = Staleness(state="stale", reasons=reasons)
            continue

        missing_outputs = [ref for ref in instance.outputs if _ref_key(ref) not in current]
        if missing_outputs:
            reasons = tuple(
                f"output {ref.path or ref.model_key} missing" for ref in missing_outputs
            )
            result[iid] = Staleness(state="stale", reasons=reasons)
            continue

        inputs_hash_now = hash_inputs(
            (_ref_key(ref), current.get(_ref_key(ref), "")) for ref in instance.inputs
        )
        if inputs_hash_now != record.inputs_hash:
            reasons = tuple(_changed_input_reasons(graph, instance, records, current))
            result[iid] = Staleness(state="stale", reasons=reasons or ("an input changed",))
            continue

        diverged_outputs = [
            ref
            for ref in instance.outputs
            if current.get(_ref_key(ref)) != record.output_hashes.get(_ref_key(ref))
        ]
        if diverged_outputs and graph.rules[instance.rule_id].kind == "human":
            # A person writes a `human` rule's outputs, so a changed output is that
            # person's new work, not a hand edit of generated content: rebuild it.
            reasons = tuple(
                f"output {ref.path or ref.model_key} was edited" for ref in diverged_outputs
            )
            result[iid] = Staleness(state="stale", reasons=reasons)
            continue
        if diverged_outputs:
            reasons = tuple(
                f"output {ref.path or ref.model_key} changed after it was produced"
                for ref in diverged_outputs
            )
            result[iid] = Staleness(state="diverged", reasons=reasons)
            continue

        result[iid] = Staleness(state="fresh", reasons=())

    return result


def _changed_input_reasons(
    graph: BuildGraph,
    instance: RuleInstance,
    records: Mapping[str, ProductionRecord],
    current: Mapping[str, str],
) -> list[str]:
    """Best-effort attribution of which inputs changed, for a readable staleness reason.

    An input produced by another instance in the graph can be checked against that
    producer's own recorded output hash. An external input (no producer in this
    graph) has no prior hash recorded anywhere reachable from here, so it is always
    named when the aggregate `inputs_hash` no longer matches.
    """
    reasons: list[str] = []
    for ref in instance.inputs:
        key = _ref_key(ref)
        label = ref.path or ref.model_key or key
        producer = graph.producers.get(key)
        if producer is not None:
            producer_record = records.get(producer)
            if producer_record is not None and producer_record.output_hashes.get(
                key
            ) == current.get(key):
                continue
        reasons.append(f"input {label} changed")
    return reasons
