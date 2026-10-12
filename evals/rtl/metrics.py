"""The metrics of the `rtl` and `tb` suites: pass@1, pass@k, repair turns, cost.

Per problem, from the run's attempts (one per `submit` the engine counted) and the
grade against the oracle:

- `pass@1`: the first attempt was accepted by the rule's own checks **and** the work
  matches the oracle (RTL: identical outputs on every cycle of every seed; testbench:
  every test passes on the oracle);
- `pass@k`, k = the rule's tries: accepted on some attempt (after repairs) and matching;
- `repair turns`: the tries used (1 = accepted at once; k with nothing accepted);
- the failure label of the first rejection (the engine's, `loop.judge`);
- cost: USD and tokens, per model, from the stream's `result` event (`modelUsage`);
  the fake runtime counts none and estimates tokens from the text it exchanged;
- wall time.

Per suite: the means of the per-problem values, the summed cost and tokens per model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class Attempt:
    """One counted try: was it accepted, and if not, its failure label and checks."""

    accepted: bool
    label: str | None = None
    failed_checks: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Attempt:
        return cls(
            accepted=bool(data.get("accepted")),
            label=data.get("label"),
            failed_checks=tuple(str(c) for c in data.get("failed_checks") or ()),
        )


@dataclass(frozen=True)
class ProblemMetrics:
    id: str
    tries_allowed: int
    attempts: tuple[Attempt, ...]
    matches_oracle: bool
    cost_usd: float = 0.0
    usage: dict[str, dict[str, float]] = field(default_factory=dict)
    wall_s: float = 0.0

    @property
    def accepted(self) -> bool:
        return any(a.accepted for a in self.attempts)

    @property
    def pass_at_1(self) -> bool:
        return bool(self.attempts) and self.attempts[0].accepted and self.matches_oracle

    @property
    def pass_at_k(self) -> bool:
        return self.accepted and self.matches_oracle

    @property
    def repair_turns(self) -> int:
        """Tries used: the attempts up to and including the accepted one."""
        for index, attempt in enumerate(self.attempts, start=1):
            if attempt.accepted:
                return index
        return len(self.attempts)

    @property
    def first_rejection(self) -> str | None:
        first = next((a for a in self.attempts if not a.accepted), None)
        if first is None:
            return None
        return first.label or "rejected"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pass_at_1": self.pass_at_1,
            "pass_at_k": self.pass_at_k,
            "k": self.tries_allowed,
            "accepted": self.accepted,
            "matches_oracle": self.matches_oracle,
            "repair_turns": self.repair_turns,
            "first_rejection": self.first_rejection,
            "cost_usd": round(self.cost_usd, 4),
            "tokens": tokens_total(self.usage),
            "usage": self.usage,
            "wall_s": round(self.wall_s, 1),
        }


def usage_from_model_usage(model_usage: Mapping[str, Any] | None) -> dict[str, dict[str, float]]:
    """`modelUsage` of a stream's `result` event, per model: tokens and cost."""
    out: dict[str, dict[str, float]] = {}
    for model, raw in (model_usage or {}).items():
        if not isinstance(raw, Mapping):
            continue
        out[str(model)] = {
            "input": float(raw.get("inputTokens", 0) or 0),
            "output": float(raw.get("outputTokens", 0) or 0),
            "cache_read": float(raw.get("cacheReadInputTokens", 0) or 0),
            "cache_write": float(raw.get("cacheCreationInputTokens", 0) or 0),
            "cost_usd": float(raw.get("costUSD", 0) or 0),
        }
    return out


def merge_usage(usages: Iterable[Mapping[str, Mapping[str, float]]]) -> dict[str, dict[str, float]]:
    """Per-model sums of several usages."""
    out: dict[str, dict[str, float]] = {}
    for usage in usages:
        for model, values in usage.items():
            into = out.setdefault(model, {})
            for key, value in values.items():
                into[key] = round(into.get(key, 0.0) + float(value), 6)
    return out


def tokens_total(usage: Mapping[str, Mapping[str, float]]) -> int:
    return int(sum(v.get(k, 0.0) for v in usage.values() for k in TOKEN_KEYS))


PRICES_USD_PER_MTOK = {"input": 3.0, "output": 15.0, "cache_read": 0.30}
"""Sonnet list prices (USD per million tokens) at the time of writing: the estimate's
basis, both sessions on sonnet. Check the provider's pricing page; this is not a bill."""
AUTHOR_OVERHEAD_TOKENS = 15_000
"""System prompt, tool schemas and the agent file an author subagent reads besides its context."""
AUTHOR_TURNS = 3
"""get_context, the write, the report: the context is read once in full, then from cache."""
AUTHOR_EXTRA_OUTPUT = 2_000
MAIN_TURNS_PER_ROUND = 4
"""next_task, the Agent call, submit, the next decision."""
MAIN_CONTEXT_TOKENS = 30_000
MAIN_OUTPUT_PER_ROUND = 1_500


def estimate_usd(estimates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A real run's cost, estimated from the text a fake run exchanged (`estimate` of each
    fake answer: rounds and the author's context and written tokens, chars / 4), with the
    constants above. An order of magnitude, not a measurement."""
    p = PRICES_USD_PER_MTOK
    usd = 0.0
    rounds = author_in = author_out = 0
    for e in estimates:
        rounds += int(e.get("rounds", 0))
        author_in += int(e.get("author_input_tokens", 0))
        author_out += int(e.get("author_output_tokens", 0))
    per_round_read = AUTHOR_OVERHEAD_TOKENS * AUTHOR_TURNS
    usd += (author_in + AUTHOR_OVERHEAD_TOKENS * rounds) * p["input"]
    usd += (author_in * (AUTHOR_TURNS - 1) + per_round_read * rounds) * p["cache_read"]
    usd += (author_out + AUTHOR_EXTRA_OUTPUT * rounds) * p["output"]
    main_read = MAIN_TURNS_PER_ROUND * MAIN_CONTEXT_TOKENS * rounds
    usd += main_read * (0.1 * p["input"] + 0.9 * p["cache_read"])
    usd += MAIN_OUTPUT_PER_ROUND * rounds * p["output"]
    return {
        "usd": round(usd / 1_000_000, 4),
        "rounds": rounds,
        "author_input_tokens": author_in,
        "author_output_tokens": author_out,
        "basis": "sonnet list prices, chars/4, fixed session overheads",
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def suite_metrics(problems: Sequence[ProblemMetrics]) -> dict[str, Any]:
    """pass@1, pass@k, mean repair turns, cost and tokens over the problems."""
    usage = merge_usage(p.usage for p in problems)
    return {
        "problems": len(problems),
        "pass_at_1": _mean([float(p.pass_at_1) for p in problems]),
        "pass_at_k": _mean([float(p.pass_at_k) for p in problems]),
        "repair_turns_mean": _mean([float(p.repair_turns) for p in problems]),
        "first_rejections": {p.id: p.first_rejection for p in problems if p.first_rejection},
        "cost_usd": round(sum(p.cost_usd for p in problems), 4),
        "tokens": tokens_total(usage),
        "usage_by_model": usage,
        "wall_s": round(sum(p.wall_s for p in problems), 1),
    }


__all__ = [
    "PRICES_USD_PER_MTOK",
    "Attempt",
    "ProblemMetrics",
    "estimate_usd",
    "merge_usage",
    "suite_metrics",
    "tokens_total",
    "usage_from_model_usage",
]
