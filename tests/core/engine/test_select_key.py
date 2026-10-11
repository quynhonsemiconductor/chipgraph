"""M2-08: `select_best` picks by the rule's checks; the Critic only breaks exact ties."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from select_helpers import Areas, Critic, cand, check, score

from chipgraph.core.engine.select import (
    Candidate,
    CostLedger,
    LedgerLimits,
    ReviewScore,
    Selection,
    error_count,
    select_best,
    warning_count,
)
from chipgraph.core.runtime.roles.review import ReviewComment, ReviewReport


def _ws(cid: str) -> Path:
    return Path("/nonexistent/ws") / cid


def _select(*candidates: Candidate, **kw: object) -> Selection:
    return asyncio.run(select_best(list(candidates), **kw))  # type: ignore[arg-type]


# --- the accept test: it picks the right candidate --------------------------------------


def test_picks_the_passing_candidate_with_the_fewest_warnings() -> None:
    """One failing, one passing with 3 warnings, one passing with 1 warning: the last."""
    selection = _select(
        cand("a", check("fail", errors=1)),
        cand("b", check("pass", warnings=3)),
        cand("c", check("pass", warnings=1)),
    )
    assert selection.winner == "c"
    assert selection.decided_by == "warnings"
    assert selection.order == ("c", "b", "a")
    assert not selection.none_passed and not selection.critic_used
    text = selection.explain()
    assert text.splitlines()[0] == "winner c: fewer warnings than b (1 < 3)"
    assert "3. a: failed, 1 error, 0 warnings" in text


def test_area_decides_an_exact_tie_on_the_checks() -> None:
    areas = Areas({"a": 30.0, "b": 12.5, "c": 20.0})
    selection = _select(
        cand("a", check("pass", warnings=1), workspace=_ws("a")),
        cand("b", check("pass", warnings=1), workspace=_ws("b")),
        cand("c", check("pass", warnings=1), workspace=_ws("c")),
        metrics=areas,
    )
    assert (selection.winner, selection.decided_by, selection.area_used) == ("b", "area", True)
    assert selection.order == ("b", "c", "a")
    assert "smaller area than c (12.5 < 20)" in selection.explain()


def test_the_critic_decides_an_exact_tie_on_checks_and_area() -> None:
    critic = Critic(
        {
            "a": score("changes_requested", major=1),
            "b": score("approve", minor=2),
            "c": score("approve", minor=1),
        }
    )
    selection = _select(
        cand("a", check("pass", warnings=1)),
        cand("b", check("pass", warnings=1)),
        cand("c", check("pass", warnings=1)),
        tiebreak=critic,
    )
    assert (selection.winner, selection.decided_by, selection.critic_used) == ("c", "critic", True)
    assert critic.asked == [("a", "b", "c")]
    assert selection.order == ("c", "b", "a")


# --- each step of the key -----------------------------------------------------------------


def test_pass_beats_fail_whatever_the_warnings() -> None:
    selection = _select(
        cand("a", check("fail", errors=1, warnings=0)),
        cand("b", check("pass", warnings=9)),
    )
    assert (selection.winner, selection.decided_by) == ("b", "passed")


def test_a_run_that_failed_does_not_pass_even_with_passing_checks() -> None:
    selection = _select(
        cand("a", check("pass"), passed=False), cand("b", check("pass", warnings=2))
    )
    assert (selection.winner, selection.decided_by) == ("b", "passed")


def test_fewer_errors_wins_among_failing_candidates_and_is_flagged_none_passed() -> None:
    selection = _select(
        cand("a", check("fail", errors=3)),
        cand("b", check("fail", errors=1, warnings=5)),
        cand("c", check("error", errors=0, warnings=6)),  # no error issue: counts as 1
    )
    assert selection.none_passed
    assert (selection.winner, selection.decided_by) == ("b", "warnings")
    assert selection.order == ("b", "c", "a")
    two = _select(
        cand("a", check("fail", errors=3)), cand("b", check("fail", errors=1, warnings=5))
    )
    assert (two.winner, two.decided_by, two.none_passed) == ("b", "errors", True)
    assert "the least bad one" in selection.explain()


def test_error_and_warning_counts() -> None:
    results = [
        check("fail", errors=2, warnings=1, infos=4),
        check("error", errors=0),
        check("pass", warnings=2, infos=1),
        check("skipped"),
    ]
    assert error_count(results) == 3
    assert warning_count(results) == 3  # info is ignored


def test_warnings_count_over_every_check_result() -> None:
    selection = _select(
        cand(
            "a", check("pass", warnings=1, check_id="lint"), check("pass", warnings=1, check_id="x")
        ),
        cand("b", check("pass", warnings=1, check_id="lint"), check("pass", check_id="x")),
    )
    assert (selection.winner, selection.decided_by) == ("b", "warnings")


def test_area_is_skipped_without_a_provider() -> None:
    selection = _select(
        cand("b", check("pass"), workspace=_ws("b")),
        cand("a", check("pass"), workspace=_ws("a")),
    )
    assert (selection.winner, selection.decided_by, selection.area_used) == (
        "a",
        "lowest_id",
        False,
    )


def test_area_is_skipped_when_the_provider_has_none() -> None:
    areas = Areas({})
    selection = _select(
        cand("a", check("pass"), workspace=_ws("a")),
        cand("b", check("pass"), workspace=_ws("b")),
        metrics=areas,
    )
    assert not selection.area_used and selection.decided_by == "lowest_id"


def test_a_candidate_lacking_the_area_sorts_after_those_that_have_it() -> None:
    selection = _select(
        cand("a", check("pass"), workspace=_ws("a")),
        cand("b", check("pass"), workspace=_ws("b")),
        cand("c", check("pass"), workspace=_ws("c")),
        metrics=Areas({"b": 50.0, "c": 40.0}),
    )
    assert selection.order == ("c", "b", "a")
    assert selection.decided_by == "area"
    assert "no area" in selection.explain()


def test_area_never_overrides_warnings() -> None:
    selection = _select(
        cand("a", check("pass", warnings=2), workspace=_ws("a")),
        cand("b", check("pass", warnings=1), workspace=_ws("b")),
        metrics=Areas({"a": 1.0, "b": 1000.0}),
    )
    assert (selection.winner, selection.decided_by) == ("b", "warnings")


# --- the Critic ---------------------------------------------------------------------------


def test_the_critic_is_called_only_for_the_tied_candidates() -> None:
    critic = Critic({"b": score("changes_requested", blocker=1), "c": score("approve")})
    selection = _select(
        cand("a", check("pass", warnings=2)),
        cand("b", check("pass")),
        cand("c", check("pass")),
        cand("d", check("fail")),
        tiebreak=critic,
    )
    assert critic.asked == [("b", "c")]
    assert (selection.winner, selection.decided_by) == ("c", "critic")
    assert selection.order == ("c", "b", "a", "d")


def test_the_critic_is_not_called_without_a_tie() -> None:
    critic = Critic({"a": score("approve"), "b": score("approve")})
    selection = _select(
        cand("a", check("pass", warnings=1)), cand("b", check("pass")), tiebreak=critic
    )
    assert critic.asked == [] and not selection.critic_used
    assert selection.winner == "b"


def test_a_critic_that_cannot_tell_them_apart_leaves_the_lowest_id() -> None:
    critic = Critic({"a": score("approve", minor=1), "b": score("approve", minor=1)})
    selection = _select(cand("b", check("pass")), cand("a", check("pass")), tiebreak=critic)
    assert (selection.winner, selection.decided_by, selection.critic_used) == (
        "a",
        "lowest_id",
        True,
    )


def test_no_tiebreak_means_the_lowest_id() -> None:
    selection = _select(
        cand("c", check("pass")), cand("b", check("pass")), cand("a", check("pass"))
    )
    assert (selection.winner, selection.decided_by) == ("a", "lowest_id")
    assert "no critic configured" in selection.critic_note


def test_a_failing_critic_leaves_the_lowest_id() -> None:
    async def broken(tied: tuple[Candidate, ...]) -> dict[str, ReviewScore]:
        raise RuntimeError("review crashed")

    selection = _select(cand("b", check("pass")), cand("a", check("pass")), tiebreak=broken)
    assert (selection.winner, selection.decided_by) == ("a", "lowest_id")
    assert "review crashed" in selection.critic_note


def test_the_critic_cost_is_charged_to_the_ledger() -> None:
    ledger = CostLedger(LedgerLimits(tries=6, tokens=1000, cost=2.0))
    ledger.reserve("a")
    critic = Critic(
        {
            "a": score("approve", tokens=30, cost=0.1),
            "b": score("changes_requested", minor=1, tokens=20, cost=0.2),
        }
    )
    selection = _select(
        cand("a", check("pass")), cand("b", check("pass")), tiebreak=critic, ledger=ledger
    )
    assert selection.critic_used and selection.winner == "a"
    critic_spend = ledger.spent("critic")
    assert (critic_spend.tries, critic_spend.tokens) == (2, 50)
    assert critic_spend.cost == pytest.approx(0.3)
    assert ledger.total.tries == 3


def test_a_critic_the_ledger_cannot_pay_for_is_skipped() -> None:
    ledger = CostLedger(LedgerLimits(tries=3))
    ledger.reserve("a", 2)  # one try left; the critic needs two (one per tied candidate)
    critic = Critic({"a": score("changes_requested", blocker=1), "b": score("approve")})
    selection = _select(
        cand("a", check("pass")), cand("b", check("pass")), tiebreak=critic, ledger=ledger
    )
    assert critic.asked == []
    assert (selection.winner, selection.decided_by, selection.critic_used) == (
        "a",
        "lowest_id",
        False,
    )
    assert "cannot pay for the critic" in selection.critic_note
    assert ledger.total.tries == 2 and ledger.limited_by == "tries"


def test_review_score_from_a_report() -> None:
    def comment(cid: str, severity: str) -> ReviewComment:
        return ReviewComment(
            id=cid,
            severity=severity,  # type: ignore[arg-type]
            category="style",
            file="a.sv",
            line=1,
            claim="c",
            evidence="a.sv:1 x",
        )

    report = ReviewReport(
        target="t",
        base="b",
        head="h",
        reviewed=("a.sv",),
        comments=(comment("R1", "major"), comment("R2", "minor"), comment("R3", "nit")),
        summary="s",
        verdict="changes_requested",
    )
    got = ReviewScore.from_report(report, tokens=5)
    assert (got.verdict, got.blocker, got.major, got.minor, got.tokens) == (
        "changes_requested",
        0,
        1,
        1,
        5,
    )
    assert got.key() > score("approve", minor=5).key()


# --- eligibility and determinism ----------------------------------------------------------


def test_unfinished_candidates_are_never_eligible() -> None:
    selection = _select(
        cand("a", check("pass"), finished=False),
        cand("b", check("fail")),
    )
    assert (selection.winner, selection.none_passed) == ("b", True)
    assert selection.decided_by == "only"
    key = selection.key_of("a")
    assert key is not None and not key.eligible and key.position is None


def test_no_finished_candidate_means_no_winner() -> None:
    selection = _select(cand("a", check("pass"), finished=False))
    assert (selection.winner, selection.decided_by) == (None, "none")
    assert selection.explain().startswith("no winner")


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        _select(cand("a", check("pass")), cand("a", check("pass")))


@pytest.mark.parametrize("n", [2, 3])
def test_the_order_does_not_depend_on_completion_order(n: int) -> None:
    pool = [
        cand("a", check("pass", warnings=2)),
        cand("b", check("pass", warnings=1)),
        cand("c", check("fail")),
    ][:n]
    expected = _select(*pool)
    rng = random.Random(n)
    for _ in range(10):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        assert _select(*shuffled) == expected
    assert expected.winner == "b"


def _random_candidate(rng: random.Random, cid: str) -> Candidate:
    status = rng.choice(["pass", "pass", "fail"])
    result = check(
        status,
        errors=rng.randint(1, 2) if status == "fail" else 0,
        warnings=rng.randint(0, 2),
    )
    return Candidate(
        id=cid,
        passed=rng.random() > 0.1,
        checks=(result,),
        finished=rng.random() > 0.1,
        workspace=_ws(cid),
    )


def test_property_the_winner_has_the_minimal_key_and_the_critic_only_orders_exact_ties() -> None:
    rng = random.Random(8)
    for _ in range(300):
        ids = rng.sample(["a", "b", "c", "d", "e"], rng.randint(1, 5))
        pool = [_random_candidate(rng, cid) for cid in ids]
        areas = Areas({cid: rng.choice([None, 1.0, 2.0]) for cid in ids})
        critic = Critic(
            {
                cid: score(rng.choice(["approve", "changes_requested"]), minor=rng.randint(0, 1))
                for cid in ids
            }
        )
        with_critic = _select(*pool, metrics=areas, tiebreak=critic)
        without = _select(*pool, metrics=areas)

        eligible = [c for c in pool if c.finished]
        if not eligible:
            assert with_critic.winner is None and without.winner is None
            continue
        keys = {k.id: k.key(with_critic.area_used) for k in with_critic.keys if k.eligible}
        best = min(keys.values())
        tied = sorted(cid for cid, key in keys.items() if key == best)
        # The winner always has the minimal key, with or without the Critic.
        assert keys[with_critic.winner] == best  # type: ignore[index]
        assert keys[without.winner] == best  # type: ignore[index]
        # Without the Critic the lowest id among the tied wins.
        assert without.winner == tied[0]
        # The Critic was asked only about an exact tie, and only about the tied ones.
        assert critic.asked == ([tuple(tied)] if len(tied) > 1 else [])
        if len(tied) > 1:
            expected = min(tied, key=lambda cid: (critic.scores[cid].key(), cid))
            assert with_critic.winner == expected
        # Outside the tie, the order is the same with or without the Critic.
        assert with_critic.order[len(tied) :] == without.order[len(tied) :]
        assert set(with_critic.order[: len(tied)]) == set(tied)
