"""M2-08: best-of-N in real workspaces: only the winner lands, the user's tree is untouched,
every workspace is removed on any exit, and the journal says what happened."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fanout_helpers import git, linked_worktrees, snapshot
from select_helpers import (
    OUTPUT,
    RULE_ID,
    Behaviour,
    Critic,
    Factory,
    Journal,
    best_of,
    check,
    instance,
    repo,
    rule,
    score,
)

from chipgraph.core.config.models import SelectCfg
from chipgraph.core.contracts import Budget, RuleInstance, RuleSpec
from chipgraph.core.engine.scheduler import ExecOutcome
from chipgraph.core.engine.select import (
    VARIANTS,
    BestOfNResult,
    Candidate,
    CandidateEnv,
    CandidateParts,
    ReviewScore,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _assert_clean(root: Path, tmp_root: Path) -> None:
    assert linked_worktrees(root) == []
    assert not tmp_root.exists() or not any(tmp_root.iterdir())


def _dirty(root: Path) -> Path:
    repo(root)
    (root / "spec.md").write_text("spec v2, not committed\n")
    (root / "notes.txt").write_text("untracked\n")
    return root


def _result(outcome: ExecOutcome) -> BestOfNResult:
    return BestOfNResult.model_validate(outcome.payload["best_of"])


def _three() -> Factory:
    return Factory(
        {
            "a": Behaviour(content="a\n", lint=(check("fail"),)),
            "b": Behaviour(content="b\n", lint=(check("pass", warnings=3),)),
            "c": Behaviour(content="c\n", lint=(check("pass", warnings=1),)),
        }
    )


def test_only_the_winners_content_lands_and_the_users_tree_is_untouched(tmp_path: Path) -> None:
    """The accept test in real workspaces: failing, 3 warnings, 1 warning -> the last."""
    root = _dirty(tmp_path / "repo")
    before = snapshot(root)
    journal = Journal()
    factory = _three()
    executor, plain = best_of(root, tmp_path / "t", factory, n=3)

    outcome = asyncio.run(
        executor.execute_checked(rule(Budget(tries=2)), instance(), journal.ctx())
    )

    result = _result(outcome)
    assert outcome.ok, outcome.message
    assert result.status == "done" and result.winner == "c"
    assert result.selection is not None and result.selection.decided_by == "warnings"
    assert result.branch is not None and result.branch.startswith("chipgraph/")
    assert git(root, "show", f"{result.branch}:{OUTPUT}") == "c\n"
    # The candidates read the user's uncommitted input, not HEAD's.
    for runtime in factory.runtimes.values():
        assert runtime.spec_seen == ["spec v2, not committed\n"]
        assert not runtime.root.exists()  # its workspace is gone
    assert snapshot(root) == before
    assert (root / OUTPUT).read_text() == "old\n" and plain.calls == []
    _assert_clean(root, tmp_path / "t")

    # Per candidate: its own hint and seed, the rule's tier, a slice of the budget.
    tasks = {cid: rt.tasks[0] for cid, rt in factory.runtimes.items()}
    assert {cid: t.context["variant"] for cid, t in tasks.items()} == dict(VARIANTS)
    assert len({t.context["seed"] for t in tasks.values()}) == 3
    assert {t.context["tier"] for t in tasks.values()} == {"medium"}
    assert {env.budget.tries for env in factory.envs} == {1}  # 2 x 1.5 = 3 tries, 1 each

    # The journal: the candidates' own turns, one event per candidate, one select event.
    summaries = journal.of("agent_turn", "phase", "candidate")
    assert [p["candidate"] for p in summaries] == ["a", "b", "c"]
    assert [p["status"] for p in summaries] == ["failed", "passed", "passed"]
    assert [p["warnings"] for p in summaries] == [0, 3, 1]
    tries = journal.of("agent_turn", "phase", "try")
    assert sorted(p["candidate"] for p in tries) == ["a", "b", "c"]
    [select] = journal.of("tool_call", "tool", "select")
    assert (select["winner"], select["decided_by"], select["branch"]) == (
        "c",
        "warnings",
        result.branch,
    )
    assert select["spend"]["tries"] == 3


def test_the_winner_does_not_depend_on_which_candidate_finishes_first(tmp_path: Path) -> None:
    winners = []
    for order, delays in enumerate(
        ({"a": 0.0, "b": 0.0, "c": 0.3}, {"a": 0.3, "b": 0.2, "c": 0.0})
    ):
        root = repo(tmp_path / f"repo{order}")
        factory = _three()
        for cid, delay in delays.items():
            factory.behaviours[cid].delay = delay  # type: ignore[index]
        executor, _ = best_of(root, tmp_path / f"t{order}", factory, n=3)
        result = _result(asyncio.run(executor.execute(rule(Budget(tries=2)), instance())))
        winners.append((result.winner, result.selection.order if result.selection else ()))
    assert winners[0] == winners[1] == ("c", ("c", "b", "a"))


class _AreaFromOutput:
    """Area = the number written in the candidate's output."""

    def area(self, workspace: Path) -> float | None:
        return float((workspace / OUTPUT).read_text())


def test_area_decides_an_exact_tie_on_the_checks(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="30\n", lint=(check("pass", warnings=1),)),
            "b": Behaviour(content="12\n", lint=(check("pass", warnings=1),)),
        }
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2, metrics=_AreaFromOutput())
    result = _result(asyncio.run(executor.execute(rule(), instance())))
    assert result.winner == "b"
    assert result.selection is not None and result.selection.decided_by == "area"
    assert git(root, "show", f"{result.branch}:{OUTPUT}") == "12\n"


def test_the_critic_decides_an_exact_tie_and_sees_the_workspaces(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="a\n", lint=(check("pass"),), tokens=10),
            "b": Behaviour(content="b\n", lint=(check("pass"),), tokens=10),
            "c": Behaviour(content="c\n", lint=(check("fail"),), tokens=10),
        }
    )
    seen: dict[str, str] = {}
    critic = Critic(
        {"a": score("changes_requested", major=1, tokens=7), "b": score("approve", tokens=5)}
    )

    async def tiebreak(tied: tuple[Candidate, ...]) -> dict[str, ReviewScore]:
        for candidate in tied:
            assert candidate.workspace is not None
            seen[candidate.id] = (candidate.workspace / OUTPUT).read_text()
        return dict(await critic(tied))

    executor, _ = best_of(root, tmp_path / "t", factory, n=3, tiebreak=tiebreak)
    result = _result(asyncio.run(executor.execute(rule(Budget(tries=4)), instance())))
    assert seen == {"a": "a\n", "b": "b\n"}  # only the tied ones, never `c`
    assert result.winner == "b"
    assert result.selection is not None
    assert (result.selection.decided_by, result.selection.critic_used) == ("critic", True)
    assert git(root, "show", f"{result.branch}:{OUTPUT}") == "b\n"
    assert (result.critic_spend.tries, result.critic_spend.tokens) == (2, 12)
    # 4 x 1.5 = 6 shared tries, 2 per candidate: a and b pass at once, c retries once.
    assert [c.spend.tries for c in result.candidates] == [1, 1, 2]
    assert (result.spend.tries, result.spend.tokens) == (4 + 2, 40 + 12)


def test_no_candidate_passed_means_no_branch_and_the_least_bad_is_reported(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="a\n", lint=(check("fail", errors=3),)),
            "b": Behaviour(content="b\n", lint=(check("fail", errors=1),)),
        }
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2)
    outcome = asyncio.run(executor.execute(rule(Budget(tries=2)), instance()))
    result = _result(outcome)
    assert not outcome.ok and outcome.failure_label == "verification"
    assert result.status == "failed" and result.winner == "b" and result.branch is None
    assert result.selection is not None and result.selection.none_passed
    assert "no candidate passed" in outcome.message
    assert git(root, "branch", "--list", "chipgraph/*") == ""
    _assert_clean(root, tmp_path / "t")


def test_a_rule_that_did_not_opt_in_runs_plain(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory({})
    executor, plain = best_of(root, tmp_path / "t", factory, cfg=SelectCfg())
    outcome = asyncio.run(executor.execute(rule(), instance()))
    assert outcome.ok and outcome.payload == {} and plain.calls == [instance().instance_id]
    assert factory.envs == []


def test_an_instance_opts_in_with_best_of(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = _three()
    executor, plain = best_of(root, tmp_path / "t", factory, cfg=SelectCfg())
    result = _result(asyncio.run(executor.execute(rule(), instance({"best_of": "2"}))))
    assert [c.id for c in result.candidates] == ["a", "b"]
    assert result.plan.n == 2 and result.winner == "b" and plain.calls == []

    bad = asyncio.run(executor.execute(rule(), instance({"best_of": "many"})))
    assert not bad.ok and bad.failure_label == "planning" and "positive integer" in bad.message


def test_n_above_max_n_runs_max_n_candidates(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = _three()
    executor, _ = best_of(root, tmp_path / "t", factory, cfg=SelectCfg(rules={RULE_ID: 9}))
    result = _result(asyncio.run(executor.execute(rule(Budget(tries=4)), instance())))
    assert (result.plan.requested, result.plan.n, result.plan.capped_by) == (9, 3, "max_n")
    assert len(result.candidates) == 3


class _WritesOutside:
    """A plain (non-checking) inner executor that also writes a file it may not."""

    def __init__(self, workspace: Path, content: str, outside: bool) -> None:
        self.workspace, self.content, self.outside = workspace, content, outside

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        (self.workspace / OUTPUT).write_text(self.content)
        if self.outside:
            (self.workspace / "spec.md").write_text("rewritten\n")
        return ExecOutcome(ok=True)


def test_a_candidate_writing_outside_its_outputs_is_rejected(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    behaviours = {
        "a": Behaviour(lint=(check("pass"),)),
        "b": Behaviour(lint=(check("pass", warnings=1),)),
    }

    def factory(env: CandidateEnv) -> CandidateParts:
        from select_helpers import ScriptedChecks

        inner = _WritesOutside(env.workspace, f"{env.candidate_id}\n", env.candidate_id == "a")
        return CandidateParts(executor=inner, checks=ScriptedChecks(behaviours[env.candidate_id]))

    executor, _ = best_of(root, tmp_path / "t", factory, n=2)  # type: ignore[arg-type]
    result = _result(asyncio.run(executor.execute(rule(), instance())))
    statuses = {c.id: c.status for c in result.candidates}
    assert statuses == {"a": "rejected", "b": "passed"}
    assert result.winner == "b"  # a's checks passed, but it is rejected: it did not pass
    assert git(root, "show", f"{result.branch}:spec.md") == "spec v1\n"


# --- cleanup on any exit ------------------------------------------------------------------


def test_cleanup_after_an_exception_in_a_candidate(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = _three()
    factory.fail_for = frozenset({"c"})
    executor, _ = best_of(root, tmp_path / "t", factory, n=3)
    result = _result(asyncio.run(executor.execute(rule(Budget(tries=2)), instance())))
    by_id = {c.id: c for c in result.candidates}
    assert by_id["c"].status == "error" and "cannot build candidate c" in by_id["c"].message
    assert result.winner == "b" and result.status == "done"
    _assert_clean(root, tmp_path / "t")


def test_cleanup_after_a_cancel(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    started = asyncio.Event()
    factory = Factory(
        {cid: Behaviour(content=f"{cid}\n", delay=600, started=started) for cid in "ab"}
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2)

    async def main() -> None:
        task = asyncio.create_task(executor.execute(rule(), instance()))
        await started.wait()
        assert any(Path(env.workspace).is_dir() for env in factory.envs)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert not any(Path(env.workspace).exists() for env in factory.envs)
    _assert_clean(root, tmp_path / "t")


def test_cleanup_after_ctrl_c_in_a_candidate(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    factory = Factory(
        {
            "a": Behaviour(content="a\n", delay=0.05, raises=KeyboardInterrupt()),
            "b": Behaviour(content="b\n", delay=600),
        }
    )
    executor, _ = best_of(root, tmp_path / "t", factory, n=2)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(executor.execute(rule(), instance()))
    assert git(root, "branch", "--list", "chipgraph/*") == ""
    _assert_clean(root, tmp_path / "t")


# --- under the scheduler ------------------------------------------------------------------


def test_the_scheduler_runs_an_opted_in_rule_as_best_of_n(tmp_path: Path) -> None:
    from chipgraph.core.engine.graph import StaticForeach, build_graph
    from chipgraph.core.engine.scheduler import CheckingExecutor, Scheduler
    from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
    from chipgraph.core.state.journal import read
    from chipgraph.core.state.layout import StateLayout

    root = repo(tmp_path / "repo")
    executor, _ = best_of(root, tmp_path / "t", _three(), n=3)
    assert isinstance(executor, CheckingExecutor)
    layout = StateLayout(tmp_path / "state-root")
    scheduler = Scheduler(
        build_graph([rule(Budget(tries=2))], StaticForeach({})),
        layout=layout,
        store=ArtifactStore(root, LabelRules()),
        executors={"agent": executor},
    )
    summary = asyncio.run(scheduler.run("*"))
    assert summary.done == ("p/write[]",)
    events = read(layout.journal(summary.run_id)).events
    [done] = [e for e in events if e.type == "rule_done"]
    assert done.payload["best_of"]["winner"] == "c"
    candidates = [
        e for e in events if e.type == "agent_turn" and e.payload.get("phase") == "candidate"
    ]
    assert [e.payload["candidate"] for e in candidates] == ["a", "b", "c"]
    assert all(e.rule_instance == "p/write[]" for e in candidates)
    selects = [e for e in events if e.type == "tool_call" and e.payload.get("tool") == "select"]
    assert len(selects) == 1
    assert (root / OUTPUT).read_text() == "old\n"  # the winner is on its branch only
