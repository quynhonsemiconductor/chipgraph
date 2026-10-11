"""Best-of-N: N candidates of one rule inside one budget, chosen by its checks (DESIGN 5.3, F6).

```
BestOfNExecutor.execute_checked(rule, instance)
  N = the rule's opt-in (instance param `best_of`, or `select.rules[rule]`), capped at
      `select.max_n`; no opt-in -> the plain executor, unchanged
  plan_budget: ONE ledger = budget_factor x the rule's budget (tries, tokens, cost);
      each candidate gets a slice (total / N); N drops to what fits (1 -> a plain run)
  run_candidates (fanout.py): N workspaces, seeded identically, same inputs and outputs
      candidate i: the rule, sliced budget, variant hint i, its own seed
      every runtime call reserves one try of the ledger first; none left -> cancelled
  select_best: the rule's checks decide; the Critic only breaks exact ties
  merge only the winner onto the result branch; journal one event per candidate and
      one `select` event
```

The selection key, in order (lower is better; `select_best` is total-ordered):

| step     | key                                                                       |
|----------|---------------------------------------------------------------------------|
| passed   | every check of the rule passed (a failing candidate wins only if none did) |
| errors   | fewer errors: error issues, or 1 for a failing check without one           |
| warnings | fewer `warning` issues over all check results (`info` ignored)             |
| area     | smaller area, only when a `MetricProvider` gives one; lacking it sorts after |
| critic   | only among exact ties on the above: approve, fewer blocker, major, minor   |
| lowest_id| still tied (or no Critic, or the ledger cannot pay for it): lowest id      |

Budget arithmetic: the ledger holds `floor(tries x factor)` tries (at least 1),
`floor(tokens x factor)` tokens and `max_cost x factor` cost when those are capped; each
candidate's `AttemptBudget` gets `tries // N` tries, `tokens // N` tokens and
`cost / N`. A ledger try is one runtime call (an infra retry costs one too); a call
holds its token cap (lowered to what the ledger has left) while it runs, so the
candidates cannot overspend tokens together. When a call cannot be paid for, that
candidate is cancelled there (cooperatively, between calls) and the best candidate that
finished wins. The Critic costs one try per tied candidate it reviews, plus what it
reports, from the same ledger. Example, a rule with `tries: 3, tokens: 200000` and a
`max_cost` of 2 USD: the ledger holds 4 tries, 300000 tokens, 3 USD; with N=2 each
candidate gets 2 tries, 150000 tokens, 1.5 USD; with N=3, 1 try, 100000 tokens, 1 USD.

Candidate outputs never touch the user's working tree: the winner lands on a result
branch (`chipgraph/<run>/best-of-<instance>`). How that branch reaches the user's tree
(and the scheduler's output check and records), and the claude-code runtime's own
wiring (its runtime per workspace, the review rule as the Critic) are M2-11's job.

Generic: no project, tool, model or file format is used here; the agent runtime is
named only to say where its wiring belongs.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.config.models import FanoutCfg, SelectCfg
from chipgraph.core.contracts import AgentResult, Budget, CheckResult, Issue
from chipgraph.core.contracts.rule import RuleInstance, RuleSpec
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.engine.fanout import (
    BranchContext,
    BranchOutcome,
    BranchResult,
    FanoutBranch,
    FanoutVcs,
    JoinCheck,
    run_candidates,
)
from chipgraph.core.engine.scheduler import (
    CheckingExecutor,
    CheckRunner,
    ExecContext,
    ExecEventType,
    ExecOutcome,
    Executor,
    RewindAware,
)
from chipgraph.core.plugin_api.protocols import AgentRuntime
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime.roles.review import ReviewReport, Verdict
from chipgraph.core.state.layout import StateLayout, new_run_id

VARIANTS: Mapping[str, str] = {
    "a": "approach a: prefer the simplest structure that meets the spec",
    "b": "approach b: prefer the most explicit structure, every case spelled out",
    "c": "approach c: prefer the fewest registers and the least state that meet the spec",
}
"""The default hint per candidate, `AgentTask.context["variant"]`; the key is the id."""

BEST_OF_PARAM = "best_of"
"""The instance parameter a rule instance opts in with (`best_of: <n>`)."""

OUTPUTS_CHECK = "best_of.outputs"
"""The check `BestOfNExecutor` adds for a candidate that left an output missing."""

CRITIC = "critic"
"""The ledger account the Critic's spend is charged to."""


# --- the shared ledger ------------------------------------------------------------------

LedgerLimit = Literal["tries", "tokens", "cost"]
"""Which limit of the shared ledger stopped something."""


class Spend(BaseModel):
    """What was spent: tries (runtime calls), tokens and cost (USD)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tries: int = Field(default=0, ge=0, description="Runtime calls (each costs one try).")
    tokens: int = Field(default=0, ge=0, description="Tokens reported.")
    cost: float = Field(default=0.0, ge=0, description="Cost reported, USD.")

    def plus(
        self, *, tries: int = 0, tokens: int | None = None, cost: float | None = None
    ) -> Spend:
        """This spend with more added (`None`: nothing reported)."""
        return Spend(
            tries=self.tries + tries,
            tokens=self.tokens + (tokens or 0),
            cost=self.cost + (cost or 0.0),
        )


class LedgerLimits(BaseModel):
    """The limits of the one budget all candidates share."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tries: int = Field(ge=1, description="Runtime calls over all candidates and the Critic.")
    tokens: int | None = Field(default=None, ge=1, description="Token cap, if capped.")
    cost: float | None = Field(default=None, gt=0, description="Cost cap in USD, if capped.")


class LedgerExhausted(BaseException):
    """Raised by a guarded runtime when the shared ledger cannot pay for another call.

    A `BaseException`, so a loop that retries every `Exception` as an infra error stops
    instead: the candidate is cancelled cooperatively, at its next call.
    """

    def __init__(self, limit: LedgerLimit) -> None:
        super().__init__(f"the shared budget is spent ({limit})")
        self.limit: LedgerLimit = limit


class CostLedger:
    """One budget for N candidates (and the Critic): tries, tokens and cost, per account.

    A call reserves its try, and holds its token cap, before it starts (`reserve`), so
    tries never exceed the limit and, as long as runtimes keep to the token cap of their
    task, neither do tokens, however the calls interleave. What a call reported is
    charged when it ends (`settle`). Cost cannot be held (a task carries no cost cap): a
    call starts only while cost is under the limit, so it can be passed by the calls
    already running. `limited_by` is the first limit that refused a call or was reached.
    """

    def __init__(self, limits: LedgerLimits) -> None:
        self.limits = limits
        self.total = Spend()
        self.accounts: dict[str, Spend] = {}
        self.held_tokens = 0
        self.limited_by: LedgerLimit | None = None

    @property
    def remaining_tries(self) -> int:
        """Tries left."""
        return max(self.limits.tries - self.total.tries, 0)

    @property
    def remaining_tokens(self) -> int | None:
        """Tokens neither spent nor held by a running call; `None` when not capped."""
        if self.limits.tokens is None:
            return None
        return max(self.limits.tokens - self.total.tokens - self.held_tokens, 0)

    @property
    def remaining_cost(self) -> float | None:
        """Cost left, or `None` when cost is not capped."""
        if self.limits.cost is None:
            return None
        return max(self.limits.cost - self.total.cost, 0.0)

    @property
    def exhausted(self) -> LedgerLimit | None:
        """The limit that is spent, or `None` while another call may run."""
        if self.total.tries >= self.limits.tries:
            return "tries"
        if self.limits.tokens is not None and self.total.tokens >= self.limits.tokens:
            return "tokens"
        if self.limits.cost is not None and self.total.cost >= self.limits.cost:
            return "cost"
        return None

    def why_not(self, tries: int = 1, tokens: int = 1) -> LedgerLimit | None:
        """The limit that keeps `tries` calls (holding `tokens`) from starting, if any."""
        spent = self.exhausted
        if spent is not None:
            return spent
        if self.remaining_tries < tries:
            return "tries"
        left = self.remaining_tokens
        if left is not None and left < max(tokens, 1):
            return "tokens"
        return None

    def can_afford(self, tries: int = 1, tokens: int = 1) -> bool:
        """Whether `tries` more calls, holding `tokens`, may start now."""
        return self.why_not(tries, tokens) is None

    def refuse(self, tries: int = 1, tokens: int = 1) -> LedgerLimit:
        """Record (once) and return the limit that refuses `tries` calls."""
        limit = self.why_not(tries, tokens) or "tries"
        if self.limited_by is None:
            self.limited_by = limit
        return limit

    def reserve(self, account: str, tries: int = 1, tokens: int | None = None) -> bool:
        """Take `tries` tries for `account` and hold `tokens`, before its calls start;
        `False` (nothing taken) when the ledger cannot pay for them."""
        if not self.can_afford(tries, tokens or 1):
            self.refuse(tries, tokens or 1)
            return False
        self._add(account, tries=tries)
        if tokens is not None and self.limits.tokens is not None:
            self.held_tokens += tokens
        return True

    def settle(
        self,
        account: str,
        *,
        held: int | None = None,
        tokens: int | None = None,
        cost: float | None = None,
    ) -> None:
        """A call of `account` ended: release what it held, charge what it reported."""
        if held is not None and self.limits.tokens is not None:
            self.held_tokens = max(self.held_tokens - held, 0)
        self.charge(account, tokens=tokens, cost=cost)

    def charge(
        self, account: str, *, tries: int = 0, tokens: int | None = None, cost: float | None = None
    ) -> None:
        """Add what `account` spent (tokens and cost as reported, maybe a try)."""
        self._add(account, tries=tries, tokens=tokens, cost=cost)
        reached = self.exhausted
        if reached is not None and reached != "tries" and self.limited_by is None:
            self.limited_by = reached

    def spent(self, account: str) -> Spend:
        """What `account` spent so far."""
        return self.accounts.get(account, Spend())

    def _add(
        self, account: str, *, tries: int = 0, tokens: int | None = None, cost: float | None = None
    ) -> None:
        self.total = self.total.plus(tries=tries, tokens=tokens, cost=cost)
        self.accounts[account] = self.spent(account).plus(tries=tries, tokens=tokens, cost=cost)


# --- planning N inside one budget --------------------------------------------------------

CapReason = Literal["max_n", "variants", "budget"]
"""Why fewer candidates run than were asked for."""


class BudgetPlan(BaseModel):
    """How many candidates run, the shared limits, and each candidate's slice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: int = Field(ge=1, description="The N the rule asked for.")
    n: int = Field(ge=1, description="The N that runs (1: a plain run).")
    capped_by: CapReason | None = Field(default=None, description="Why n < requested.")
    factor: float = Field(gt=0, description="The shared budget, in single-run budgets.")
    limits: LedgerLimits = Field(description="The shared ledger's limits.")
    slice: Budget = Field(description="Each candidate's budget (tries, tier, tokens).")
    slice_cost: float | None = Field(default=None, description="Each candidate's cost cap.")

    @property
    def plain(self) -> bool:
        """True when only one candidate fits: then it is a plain run of the rule."""
        return self.n == 1


def plan_budget(
    budget: Budget,
    *,
    requested: int,
    cfg: SelectCfg | None = None,
    max_cost: float | None = None,
    variants: int | None = None,
) -> BudgetPlan:
    """N, the shared ledger and the slices for a rule with `budget` asking for `requested`.

    N is capped at `cfg.max_n`, at `variants` (the hints there are), and at what the
    ledger can give at least one try (and one token) each. The candidates start at the
    rule's tier and keep its escalation rule; nothing escalates across candidates.
    """
    cfg = cfg if cfg is not None else SelectCfg()
    factor = cfg.budget_factor
    asked = max(1, requested)
    n = cfg.effective_n(asked)
    capped: CapReason | None = "max_n" if n < asked else None
    if variants is not None and n > variants:
        n, capped = max(1, variants), "variants"
    tries = max(1, math.floor(budget.tries * factor + 1e-9))
    tokens = max(1, math.floor(budget.tokens * factor + 1e-9)) if budget.tokens else None
    cost = max_cost * factor if max_cost is not None and max_cost > 0 else None
    fits = min(tries, tokens) if tokens is not None else tries
    if n > fits:
        n, capped = fits, "budget"
    slice_budget = Budget(
        tries=max(1, tries // n),
        tier=budget.tier,
        escalate=budget.escalate,
        tokens=max(1, tokens // n) if tokens is not None else None,
    )
    return BudgetPlan(
        requested=asked,
        n=n,
        capped_by=capped,
        factor=factor,
        limits=LedgerLimits(tries=tries, tokens=tokens, cost=cost),
        slice=slice_budget,
        slice_cost=cost / n if cost is not None else None,
    )


# --- selection ----------------------------------------------------------------------------


class ReviewScore(BaseModel):
    """The Critic's score of one tied candidate (from its `ReviewReport`), and its spend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Verdict = Field(description="approve, or changes_requested.")
    blocker: int = Field(default=0, ge=0)
    major: int = Field(default=0, ge=0)
    minor: int = Field(default=0, ge=0)
    tokens: int | None = Field(default=None, ge=0, description="Tokens the review reported.")
    cost: float | None = Field(default=None, ge=0, description="Cost the review reported.")

    @classmethod
    def from_report(
        cls, report: ReviewReport, *, tokens: int | None = None, cost: float | None = None
    ) -> ReviewScore:
        """The score of a review (nits do not count)."""

        def count(severity: str) -> int:
            return sum(1 for c in report.comments if c.severity == severity)

        return cls(
            verdict=report.verdict,
            blocker=count("blocker"),
            major=count("major"),
            minor=count("minor"),
            tokens=tokens,
            cost=cost,
        )

    def key(self) -> tuple[int, int, int, int]:
        """Lower is better: approve first, then fewer blocker, major, minor comments."""
        return (0 if self.verdict == "approve" else 1, self.blocker, self.major, self.minor)


@dataclass(frozen=True)
class Candidate:
    """One finished (or not) candidate as the selection sees it."""

    id: str
    passed: bool
    """The candidate's own run succeeded (the agent loop, or the plain executor)."""
    checks: tuple[CheckResult, ...] = ()
    """The final result of each of the rule's checks on the candidate's outputs."""
    finished: bool = True
    """False for a candidate that was cancelled, skipped or crashed: never eligible."""
    workspace: Path | None = None
    """Its workspace, while it exists (for a `MetricProvider` and the Critic)."""

    @property
    def all_passed(self) -> bool:
        """Its run succeeded and every check result is ok."""
        return self.passed and all(c.ok for c in self.checks)


@runtime_checkable
class MetricProvider(Protocol):
    """Measures a candidate's workspace; optional (no provider: the area step is skipped)."""

    def area(self, workspace: Path) -> float | None:
        """The candidate's area (lower is better), or `None` when it cannot be had."""
        ...


Tiebreak = Callable[[tuple[Candidate, ...]], Awaitable[Mapping[str, ReviewScore]]]
"""The Critic: scores the tied candidates (candidate id -> score). In real use it runs
the review rule on each tied candidate's workspace; any async callable will do."""

SelectStep = Literal["none", "only", "passed", "errors", "warnings", "area", "critic", "lowest_id"]
"""Which step of the key decided the winner (`none`: no candidate finished)."""

_STEPS: tuple[SelectStep, ...] = ("passed", "errors", "warnings", "area")


def error_count(checks: Sequence[CheckResult]) -> int:
    """Error issues over `checks`; a failing check with no error issue counts as one."""
    total = 0
    for result in checks:
        errors = sum(1 for i in result.issues if i.severity == "error")
        total += errors if errors or result.ok else 1
    return total


def warning_count(checks: Sequence[CheckResult]) -> int:
    """Issues of severity `warning` over `checks` (`info` is ignored)."""
    return sum(1 for r in checks for i in r.issues if i.severity == "warning")


class CandidateKey(BaseModel):
    """A candidate's place in the selection: every component of its key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    eligible: bool = Field(description="It finished, so it took part.")
    passed: bool = Field(default=False, description="Its run and every check passed.")
    errors: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    area: float | None = Field(default=None, description="When a MetricProvider gave one.")
    critic: ReviewScore | None = Field(default=None, description="When the Critic scored it.")
    position: int | None = Field(default=None, ge=1, description="1 = the winner.")

    def key(self, area_used: bool) -> tuple[int, int, int, tuple[int, float]]:
        """Steps 1-4 as a tuple: lower is better."""
        if not area_used:
            area: tuple[int, float] = (0, 0.0)
        elif self.area is None:
            area = (1, 0.0)
        else:
            area = (0, self.area)
        return (0 if self.passed else 1, self.errors, self.warnings, area)


class Selection(BaseModel):
    """The outcome of `select_best`: the winner, every key, the deciding step, why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    winner: str | None = Field(description="The chosen candidate; None if none finished.")
    decided_by: SelectStep = Field(description="The step that set the winner apart.")
    none_passed: bool = Field(
        default=False, description="No candidate passed: the winner is only the least bad."
    )
    area_used: bool = Field(default=False, description="Whether the area step applied.")
    critic_used: bool = Field(default=False, description="Whether the Critic was called.")
    critic_note: str = Field(default="", description="Why the Critic was not used, if tied.")
    order: tuple[str, ...] = Field(default=(), description="Eligible candidates, best first.")
    keys: tuple[CandidateKey, ...] = Field(
        default=(), description="Every candidate: the eligible in order, then the rest by id."
    )

    def key_of(self, candidate_id: str) -> CandidateKey | None:
        """The key of `candidate_id`."""
        return next((k for k in self.keys if k.id == candidate_id), None)

    def explain(self) -> str:
        """The choice and its reasons, one candidate per line."""
        lines: list[str] = []
        if self.winner is None:
            lines.append("no winner: no candidate finished")
        else:
            lines.append(f"winner {self.winner}: {self._reason()}")
        if self.none_passed:
            lines.append("no candidate passed every check: the winner is the least bad one")
        if self.critic_note:
            lines.append(f"critic: {self.critic_note}")
        for k in self.keys:
            if not k.eligible:
                lines.append(f"-  {k.id}: did not finish, not eligible")
                continue
            parts = [
                "passed" if k.passed else "failed",
                _plural(k.errors, "error"),
                _plural(k.warnings, "warning"),
            ]
            if self.area_used:
                parts.append(f"area {k.area:g}" if k.area is not None else "no area")
            if k.critic is not None:
                c = k.critic
                parts.append(
                    f"critic {c.verdict} ({c.blocker} blocker, {c.major} major, {c.minor} minor)"
                )
            lines.append(f"{k.position}. {k.id}: " + ", ".join(parts))
        return "\n".join(lines)

    def _reason(self) -> str:
        if self.decided_by == "only" or len(self.order) < 2:
            return "the only candidate that finished"
        win, other = self.key_of(self.order[0]), self.key_of(self.order[1])
        assert win is not None and other is not None
        step = self.decided_by
        if step == "passed":
            return f"it passed every check and {other.id} did not"
        if step == "errors":
            return f"fewer errors than {other.id} ({win.errors} < {other.errors})"
        if step == "warnings":
            return f"fewer warnings than {other.id} ({win.warnings} < {other.warnings})"
        if step == "area":
            if other.area is None:
                return f"it has an area and {other.id} has none"
            return f"smaller area than {other.id} ({win.area:g} < {other.area:g})"
        if step == "critic":
            return f"tied with {other.id} on checks, warnings and area; the critic preferred it"
        return f"tied with {other.id} on every step; the lowest id wins"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _area(metrics: MetricProvider | None, candidate: Candidate) -> float | None:
    if metrics is None or candidate.workspace is None:
        return None
    try:
        value = metrics.area(candidate.workspace)
    except Exception:  # a metric that cannot be had sorts the candidate after
        return None
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def rank(
    candidates: Sequence[Candidate], metrics: MetricProvider | None = None
) -> tuple[tuple[CandidateKey, ...], bool]:
    """Steps 1-4: the eligible candidates' keys, best first (ties by id), and whether
    the area step applied. Pure apart from the metric provider."""
    eligible = sorted((c for c in candidates if c.finished), key=lambda c: c.id)
    keys = [
        CandidateKey(
            id=c.id,
            eligible=True,
            passed=c.all_passed,
            errors=error_count(c.checks),
            warnings=warning_count(c.checks),
            area=_area(metrics, c),
        )
        for c in eligible
    ]
    area_used = any(k.area is not None for k in keys)
    keys.sort(key=lambda k: (k.key(area_used), k.id))
    return tuple(keys), area_used


async def select_best(
    candidates: Sequence[Candidate],
    metrics: MetricProvider | None = None,
    tiebreak: Tiebreak | None = None,
    *,
    ledger: CostLedger | None = None,
) -> Selection:
    """Choose one candidate: deterministic checks first, the Critic only for exact ties.

    The key (see the module docstring) orders every finished candidate; the order is
    total (equal keys fall back to the id), so the winner does not depend on the order
    the candidates are given or finished in. `tiebreak` is called only for the
    candidates tied for first place after steps 1-4, so it can never override a
    difference there; its cost is charged to `ledger` (one try per tied candidate, plus
    what it reports), and it is skipped when the ledger cannot pay for it. Still tied,
    no tiebreak, or skipped: the lowest id wins.
    """
    by_id = {c.id: c for c in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("candidate ids must be unique")
    keys, area_used = rank(candidates, metrics)
    rest = tuple(
        CandidateKey(id=c.id, eligible=False)
        for c in sorted(by_id.values(), key=lambda c: c.id)
        if not c.finished
    )
    if not keys:
        return Selection(winner=None, decided_by="none", keys=rest)

    top = keys[0].key(area_used)
    tied = [k for k in keys if k.key(area_used) == top]
    ordered = list(keys)
    critic_used = False
    note = ""
    scores: Mapping[str, ReviewScore] = {}
    if len(tied) > 1:
        if tiebreak is None:
            note = "no critic configured: the lowest id wins the tie"
        elif ledger is not None and not ledger.can_afford(len(tied)):
            note = (
                f"the shared budget cannot pay for the critic ({len(tied)} tries needed, "
                f"{ledger.remaining_tries} left, the {ledger.refuse(len(tied))} limit): "
                "the lowest id wins the tie"
            )
        else:
            critic_used = True
            if ledger is not None:
                ledger.reserve(CRITIC, len(tied))
            try:
                scores = dict(await tiebreak(tuple(by_id[k.id] for k in tied)))
            except Exception as exc:
                note = f"the critic failed ({type(exc).__name__}: {exc}): the lowest id wins"
                scores = {}
            if ledger is not None:
                ledger.charge(
                    CRITIC,
                    tokens=sum(s.tokens or 0 for s in scores.values()) or None,
                    cost=sum(s.cost or 0.0 for s in scores.values()) or None,
                )

            def critic_key(k: CandidateKey) -> tuple[int, tuple[int, int, int, int], str]:
                score = scores.get(k.id)
                return (0, score.key(), k.id) if score is not None else (1, (0, 0, 0, 0), k.id)

            tied_sorted = sorted(tied, key=critic_key)
            ordered = [*tied_sorted, *keys[len(tied) :]]
    ordered = [
        k.model_copy(update={"position": i + 1, "critic": scores.get(k.id)})
        for i, k in enumerate(ordered)
    ]

    decided: SelectStep
    if len(ordered) == 1:
        decided = "only"
    else:
        win, other = ordered[0], ordered[1]
        wk, ok = win.key(area_used), other.key(area_used)
        if wk != ok:
            decided = next(_STEPS[i] for i in range(4) if wk[i] != ok[i])
        elif win.critic is not None and (
            other.critic is None or win.critic.key() < other.critic.key()
        ):
            decided = "critic"
        else:
            decided = "lowest_id"
    return Selection(
        winner=ordered[0].id,
        decided_by=decided,
        none_passed=not ordered[0].passed,
        area_used=area_used,
        critic_used=critic_used,
        critic_note=note,
        order=tuple(k.id for k in ordered),
        keys=(*ordered, *rest),
    )


# --- the executor ------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateEnv:
    """What `inner_factory` gets to build one candidate's executor.

    Build it to work in `workspace` (its store, runtime and checks rooted there), with
    `budget`/`max_cost` as its caps, and without remembered stops (no state layout:
    candidates share the instance id). Wrap its runtime with `guard`: every call then
    reserves a try of the shared ledger first (none left: the candidate is cancelled),
    has its token cap lowered to what the ledger has left, carries `variant`, `seed` and
    the candidate id in `AgentTask.context`, and is charged what it reports.
    """

    candidate_id: str
    variant: str
    seed: str
    workspace: Path
    budget: Budget
    max_cost: float | None
    guard: Callable[[AgentRuntime], AgentRuntime]


@dataclass(frozen=True)
class CandidateParts:
    """One candidate's executor, and the check runner for its workspace."""

    executor: Executor
    checks: CheckRunner | None = None


InnerFactory = Callable[[CandidateEnv], CandidateParts]


class _GuardedRuntime:
    """An `AgentRuntime` that pays for every call from the shared ledger first."""

    def __init__(
        self, inner: AgentRuntime, ledger: CostLedger, account: str, context: Mapping[str, str]
    ) -> None:
        self.inner = inner
        self.ledger = ledger
        self.account = account
        self.context = dict(context)
        self.name: str = getattr(inner, "name", "runtime")

    async def run_task(self, task: AgentTask) -> AgentResult:
        budget = task.budget
        left = self.ledger.remaining_tokens
        hold: int | None = None
        if left is not None:
            hold = min(budget.tokens, left) if budget.tokens is not None else left
            if hold >= 1:
                budget = budget.model_copy(update={"tokens": hold})
        if not self.ledger.reserve(self.account, tokens=hold):
            raise LedgerExhausted(self.ledger.why_not(1, hold or 1) or "tries")
        sent = task.model_copy(
            update={"context": {**task.context, **self.context}, "budget": budget}
        )
        try:
            result = await self.inner.run_task(sent)
        except BaseException:
            self.ledger.settle(self.account, held=hold)
            raise
        self.ledger.settle(self.account, held=hold, tokens=result.tokens, cost=result.cost)
        return result


CandidateStatus = Literal["passed", "failed", "rejected", "cancelled", "skipped", "error"]


class CandidateResult(BaseModel):
    """One candidate's outcome and spend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    variant: str
    status: CandidateStatus
    message: str = ""
    errors: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    checks: tuple[CheckResult, ...] = ()
    files: tuple[str, ...] = Field(default=(), description="Files it changed.")
    spend: Spend = Field(default_factory=Spend)
    agent: AgentResult | None = None


BestOfNStatus = Literal["done", "failed", "plain"]


class BestOfNResult(BaseModel):
    """The outcome of one best-of-N run (the `best_of` payload of the rule's event)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    status: BestOfNStatus = Field(description="done; failed; plain (only one fitted).")
    instance: str
    message: str = ""
    plan: BudgetPlan
    winner: str | None = None
    candidates: tuple[CandidateResult, ...] = Field(default=(), description="By id.")
    selection: Selection | None = None
    spend: Spend = Field(default_factory=Spend, description="Total, Critic included.")
    critic_spend: Spend = Field(default_factory=Spend)
    limited_by: LedgerLimit | None = Field(
        default=None, description="The shared limit that stopped a call, if any."
    )
    fanout: str | None = Field(default=None, description="The candidates' fan-out run id.")
    branch: str | None = Field(default=None, description="The result branch, when created.")
    commit: str | None = None


@dataclass
class _State:
    id: str
    variant: str
    status: CandidateStatus = "skipped"
    message: str = "not started"
    outcome: ExecOutcome | None = None
    checks: dict[str, CheckResult] = field(default_factory=dict)
    guarded: bool = False


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(instance_id: str) -> str:
    slug = re.sub(r"\.{2,}", ".", _SLUG_RE.sub("-", instance_id.lower())).strip("-._")
    return f"best-of-{slug}"[:80].rstrip("-._") if slug else "best-of"


def _synthetic(check_id: str, issues: Sequence[Issue]) -> CheckResult:
    digest = hashlib.sha256("|".join(i.msg for i in issues).encode("utf-8")).hexdigest()
    return CheckResult(
        check_id=check_id,
        status="fail" if issues else "pass",
        issues=tuple(issues),
        duration_s=0.0,
        idempotency_key=f"{check_id}:{digest}",
    )


def _noop_emit(_type: ExecEventType, _payload: dict[str, Any]) -> None:
    return None


class BestOfNExecutor:
    """An executor that runs an opted-in rule as N candidates and keeps the best.

    A rule opts in with the instance parameter `best_of: <n>` or `select.rules` in the
    profile (`cfg`); every other rule goes to `default`, unchanged. `inner_factory`
    builds each candidate's executor for its workspace (`CandidateEnv`); `vcs` and
    `root` are the fan-out's. `metrics` adds the area step, `tiebreak` the Critic.

    It is a `CheckingExecutor`: for candidates it runs the rule's checks itself, in each
    candidate's workspace; for a plain run it lets `default` do it, or runs them through
    the scheduler's context. Wiring this into the claude-code runtime (a runtime per
    workspace, the review rule as `tiebreak`, landing the result branch) is M2-11's job.
    """

    def __init__(
        self,
        inner_factory: InnerFactory,
        vcs: FanoutVcs,
        *,
        root: Path,
        default: Executor,
        cfg: SelectCfg | None = None,
        fanout: FanoutCfg | None = None,
        max_cost: float | None = None,
        metrics: MetricProvider | None = None,
        tiebreak: Tiebreak | None = None,
        variants: Mapping[str, str] | None = None,
        join_checks: Sequence[JoinCheck] = (),
        tmp_root: Path | None = None,
        layout: StateLayout | None = None,
        make_branch: bool = True,
    ) -> None:
        self.inner_factory = inner_factory
        self.vcs = vcs
        self.root = root
        self.default = default
        self.cfg = cfg if cfg is not None else SelectCfg()
        self.fanout = fanout if fanout is not None else FanoutCfg()
        self.max_cost = max_cost
        self.metrics = metrics
        self.tiebreak = tiebreak
        self.variants = dict(variants if variants is not None else VARIANTS)
        if not self.variants:
            raise ValueError("best-of-N needs at least one variant")
        self.join_checks = tuple(join_checks)
        self.tmp_root = tmp_root
        self.layout = layout
        self.make_branch = make_branch
        self.results: dict[str, BestOfNResult] = {}

    # --- opting in ------------------------------------------------------------------

    def requested_n(self, rule: RuleSpec, instance: RuleInstance) -> int | None:
        """The N `instance` asks for, or `None` when its rule did not opt in.

        Raises `ValueError` for a `best_of` parameter that is not a positive integer.
        """
        raw = instance.params.get(BEST_OF_PARAM)
        if raw is not None:
            try:
                n = int(raw)
            except ValueError:
                raise ValueError(f"{BEST_OF_PARAM}={raw!r} is not a positive integer") from None
            if n < 1:
                raise ValueError(f"{BEST_OF_PARAM}={raw!r} is not a positive integer")
            return n
        return self.cfg.rules.get(rule.id)

    def forget(self, instance_id: str) -> None:
        """Pass a rewind on to the plain executor."""
        if isinstance(self.default, RewindAware):
            self.default.forget(instance_id)

    # --- the executor protocols ----------------------------------------------------------

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        """Outside a scheduler: no journal, no check runner for a plain run."""
        ctx = ExecContext(run_id="", run_check=None, emit=_noop_emit)
        return await self.execute_checked(rule, instance, ctx)

    async def execute_checked(
        self, rule: RuleSpec, instance: RuleInstance, ctx: ExecContext
    ) -> ExecOutcome:
        try:
            requested = self.requested_n(rule, instance)
        except ValueError as exc:
            return ExecOutcome(ok=False, failure_label="planning", message=str(exc))
        if requested is None:
            return await self._plain(rule, instance, ctx)
        plan = plan_budget(
            rule.budget,
            requested=requested,
            cfg=self.cfg,
            max_cost=self.max_cost,
            variants=len(self.variants),
        )
        if plan.plain:
            outcome = await self._plain(rule, instance, ctx)
            why = f" ({plan.capped_by})" if plan.capped_by else ""
            result = BestOfNResult(
                status="plain",
                instance=instance.instance_id,
                message=f"best-of-{plan.requested}: only one candidate fits{why}; a plain run",
                plan=plan,
            )
            self.results[instance.instance_id] = result
            return outcome.model_copy(
                update={"payload": {**outcome.payload, "best_of": result.model_dump(mode="json")}}
            )
        return await _BestOfRun(self, rule, instance, ctx, plan).run()

    async def _plain(self, rule: RuleSpec, instance: RuleInstance, ctx: ExecContext) -> ExecOutcome:
        if isinstance(self.default, CheckingExecutor):
            return await self.default.execute_checked(rule, instance, ctx)
        outcome = await self.default.execute(rule, instance)
        if outcome.ok and ctx.run_check is not None:
            for check_id in rule.checks:
                checked = await ctx.run_check(check_id)
                if not checked.ok:
                    return ExecOutcome(
                        ok=False, failure_label="verification", message=f"check {check_id} failed"
                    )
        return outcome


class _BestOfRun:
    """One best-of-N run of one instance."""

    def __init__(
        self,
        executor: BestOfNExecutor,
        rule: RuleSpec,
        instance: RuleInstance,
        ctx: ExecContext,
        plan: BudgetPlan,
    ) -> None:
        self.ex = executor
        self.rule = rule
        self.instance = instance
        self.ctx = ctx
        self.plan = plan
        self.ledger = CostLedger(plan.limits)
        self.run_id = new_run_id()
        ids = list(executor.variants)[: plan.n]
        self.states = {cid: _State(id=cid, variant=executor.variants[cid]) for cid in ids}
        self.writes = tuple(r.path for r in instance.outputs if r.path is not None)
        self.inputs = tuple(dict.fromkeys(r.path for r in instance.inputs if r.path is not None))
        self.selection: Selection | None = None
        self.sliced = rule.model_copy(update={"budget": plan.slice})

    def seed(self, cid: str) -> str:
        digest = hashlib.sha256(f"{self.instance.instance_id}\0{cid}".encode()).hexdigest()
        return f"{cid}-{digest[:12]}"

    # --- one candidate -------------------------------------------------------------------

    def candidate_ctx(self, state: _State, checks: CheckRunner | None) -> ExecContext:
        async def run_check(check_id: str) -> CheckResult:
            assert checks is not None
            checked = await checks.run(check_id, self.instance)
            state.checks[check_id] = checked  # the last result of each check counts
            return checked

        def emit(type_: ExecEventType, payload: dict[str, Any]) -> None:
            if type_ == "agent_turn" and not state.guarded:
                # An executor whose runtime is not guarded is charged after the fact.
                self.ledger.charge(
                    state.id, tries=1, tokens=payload.get("tokens"), cost=payload.get("cost")
                )
            self.ctx.emit(type_, {**payload, "candidate": state.id, "best_of": self.run_id})

        return ExecContext(
            run_id=self.ctx.run_id, run_check=run_check if checks is not None else None, emit=emit
        )

    def branch_fn(self, cid: str) -> Callable[[BranchContext], Awaitable[BranchOutcome]]:
        state = self.states[cid]

        async def run(bctx: BranchContext) -> BranchOutcome:
            if not self.ledger.can_afford(1):
                self.ledger.refuse()
                state.status, state.message = "skipped", "not started: the shared budget is spent"
                return BranchOutcome(ok=False, message=state.message)
            context = {"variant": state.variant, "seed": self.seed(cid), "candidate": cid}

            def guard(runtime: AgentRuntime) -> AgentRuntime:
                state.guarded = True
                return _GuardedRuntime(runtime, self.ledger, cid, context)

            env = CandidateEnv(
                candidate_id=cid,
                variant=state.variant,
                seed=context["seed"],
                workspace=bctx.path,
                budget=self.plan.slice,
                max_cost=self.plan.slice_cost,
                guard=guard,
            )
            state.status, state.message = "error", "did not finish"
            parts = self.ex.inner_factory(env)
            cctx = self.candidate_ctx(state, parts.checks)
            try:
                outcome = await self._run_inner(parts, cctx)
            except LedgerExhausted as stop:
                state.status = "cancelled"
                state.message = f"cancelled: the shared budget is spent ({stop.limit})"
                return BranchOutcome(ok=False, message=state.message)
            state.outcome = outcome
            missing = [
                Issue(file=p, rule="output.missing", msg=f"output {p} was not written")
                for p in self.writes
                if not (bctx.path / p).exists()
            ]
            if missing:
                state.checks[OUTPUTS_CHECK] = _synthetic(OUTPUTS_CHECK, missing)
            unchecked = [c for c in self.rule.checks if c not in state.checks]
            ok = outcome.ok and not unchecked and all(c.ok for c in state.checks.values())
            state.status = "passed" if ok else "failed"
            state.message = outcome.message or (
                f"checks not run: {', '.join(unchecked)}" if unchecked else ""
            )
            return BranchOutcome(ok=outcome.ok, message=state.message, agent=_agent_of(outcome))

        return run

    async def _run_inner(self, parts: CandidateParts, cctx: ExecContext) -> ExecOutcome:
        inner = parts.executor
        if isinstance(inner, CheckingExecutor):
            return await inner.execute_checked(self.sliced, self.instance, cctx)
        outcome = await inner.execute(self.sliced, self.instance)
        if outcome.ok and cctx.run_check is not None:
            for check_id in self.rule.checks:
                await cctx.run_check(check_id)
        return outcome

    # --- choosing ----------------------------------------------------------------------

    async def choose(
        self, results: tuple[BranchResult, ...], workspaces: Mapping[str, Path]
    ) -> str | None:
        branches = {r.id: r for r in results}
        candidates: list[Candidate] = []
        for cid, state in sorted(self.states.items()):
            branch = branches.get(cid)
            if branch is not None and branch.status == "rejected":
                state.status, state.message = "rejected", branch.message
            elif branch is not None and branch.status in ("error", "timeout"):
                state.status, state.message = "error", branch.message
            finished = state.status in ("passed", "failed", "rejected")
            candidates.append(
                Candidate(
                    id=cid,
                    passed=state.status == "passed",
                    checks=tuple(state.checks[k] for k in sorted(state.checks)),
                    finished=finished,
                    workspace=workspaces.get(cid),
                )
            )
        self.selection = await select_best(
            candidates, self.ex.metrics, self.ex.tiebreak, ledger=self.ledger
        )
        winner = self.selection.winner
        if winner is None or self.selection.none_passed:
            return None
        return winner

    # --- the run ----------------------------------------------------------------------

    async def run(self) -> ExecOutcome:
        branches = [
            FanoutBranch(id=cid, run=self.branch_fn(cid), write_set=self.writes, inputs=self.inputs)
            for cid in self.states
        ]
        fan = await run_candidates(
            branches,
            choose=self.choose,
            root=self.ex.root,
            vcs=self.ex.vcs,
            name=_slug(self.instance.instance_id),
            run_id=self.run_id,
            join_checks=self.ex.join_checks,
            max_parallel=self.ex.fanout.effective_max_parallel(),
            branch_timeout_s=self.ex.fanout.branch_timeout_s,
            tmp_root=self.ex.tmp_root,
            layout=self.ex.layout,
            ctx=self.ctx,
            make_branch=self.ex.make_branch,
        )
        by_branch = {b.id: b for b in fan.branches}
        candidates: list[CandidateResult] = []
        for cid, state in sorted(self.states.items()):
            checks = tuple(state.checks[k] for k in sorted(state.checks))
            branch = by_branch.get(cid)
            cand = CandidateResult(
                id=cid,
                variant=state.variant,
                status=state.status,
                message=state.message,
                errors=error_count(checks),
                warnings=warning_count(checks),
                checks=checks,
                files=tuple(c.path for c in branch.files) if branch is not None else (),
                spend=self.ledger.spent(cid),
                agent=_agent_of(state.outcome) if state.outcome is not None else None,
            )
            candidates.append(cand)
            self.ctx.emit(
                "agent_turn",
                {
                    "phase": "candidate",
                    "best_of": self.run_id,
                    "candidate": cid,
                    "variant": state.variant,
                    "status": state.status,
                    "message": state.message,
                    "errors": cand.errors,
                    "warnings": cand.warnings,
                    "failed_checks": [c.check_id for c in checks if not c.ok],
                    "spend": cand.spend.model_dump(mode="json"),
                },
            )

        selection = self.selection
        ok = fan.ok and selection is not None and fan.chosen == selection.winner
        if ok:
            assert selection is not None
            message = (
                f"best-of-{self.plan.n}: candidate {selection.winner} chosen by "
                f"{selection.decided_by}; result on branch {fan.branch or fan.commit}"
            )
        elif selection is None or selection.winner is None:
            message = f"best-of-{self.plan.n}: no candidate finished ({fan.message})"
        elif selection.none_passed:
            message = (
                f"best-of-{self.plan.n}: no candidate passed; the least bad is "
                f"{selection.winner}, nothing merged"
            )
        else:
            message = f"best-of-{self.plan.n}: {fan.message}"
        result = BestOfNResult(
            status="done" if ok else "failed",
            instance=self.instance.instance_id,
            message=message,
            plan=self.plan,
            winner=selection.winner if selection is not None else None,
            candidates=tuple(candidates),
            selection=selection,
            spend=self.ledger.total,
            critic_spend=self.ledger.spent(CRITIC),
            limited_by=self.ledger.limited_by,
            fanout=fan.run_id,
            branch=fan.branch if ok else None,
            commit=fan.commit if ok else None,
        )
        self.ex.results[self.instance.instance_id] = result
        self.ctx.emit(
            "tool_call",
            {
                "tool": "select",
                "event": "select",
                "best_of": self.run_id,
                "status": result.status,
                "winner": result.winner,
                "decided_by": selection.decided_by if selection is not None else "none",
                "critic_used": selection.critic_used if selection is not None else False,
                "spend": result.spend.model_dump(mode="json"),
                "limited_by": result.limited_by,
                "branch": result.branch,
            },
        )
        payload = {"best_of": result.model_dump(mode="json")}
        if ok:
            return ExecOutcome(ok=True, message=message, payload=payload)
        return ExecOutcome(
            ok=False, failure_label=self._failure_label(selection), message=message, payload=payload
        )

    def _failure_label(self, selection: Selection | None) -> FailureLabel:
        if selection is not None and selection.winner is not None:
            outcome = self.states[selection.winner].outcome
            if outcome is not None and outcome.failure_label is not None:
                return outcome.failure_label
        return "verification"


def _agent_of(outcome: ExecOutcome) -> AgentResult | None:
    """The `AgentResult` an agent rule loop put in its outcome's payload, if any."""
    raw = outcome.payload.get("result")
    if not isinstance(raw, Mapping):
        return None
    with contextlib.suppress(ValueError):
        return AgentResult.model_validate(dict(raw))
    return None


__all__ = [
    "BEST_OF_PARAM",
    "CRITIC",
    "OUTPUTS_CHECK",
    "VARIANTS",
    "BestOfNExecutor",
    "BestOfNResult",
    "BestOfNStatus",
    "BudgetPlan",
    "Candidate",
    "CandidateEnv",
    "CandidateKey",
    "CandidateParts",
    "CandidateResult",
    "CandidateStatus",
    "CapReason",
    "CostLedger",
    "InnerFactory",
    "LedgerExhausted",
    "LedgerLimit",
    "LedgerLimits",
    "MetricProvider",
    "ReviewScore",
    "SelectStep",
    "Selection",
    "Spend",
    "Tiebreak",
    "error_count",
    "plan_budget",
    "rank",
    "select_best",
    "warning_count",
]
