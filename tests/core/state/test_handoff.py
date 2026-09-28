"""Tests for `chipgraph.core.state.handoff`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from chipgraph.core.contracts.event import Event
from chipgraph.core.state.handoff import (
    FailedItem,
    Handoff,
    WaitingItem,
    build_handoff,
    render_markdown,
    render_status,
    write_handoff,
)
from chipgraph.core.state.journal import replay
from chipgraph.core.state.layout import StateLayout

RUN_ID = "20260928T000000Z-abcdef"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)

RTL_TIMER = "digital-rtl/rtl_module[block=timer]"
LINT_TIMER = "spec-core/lint[block=timer]"
SIM_TIMER = "digital-rtl/sim[block=timer]"
GATE_SPEC = "gen/spec[block=uart]"
DIVERGED = "digital-rtl/rtl_module[block=gpio]"
FAILED_SIM = "digital-rtl/sim[block=spi]"
BLOCKED_CHECK = "spec-core/lint[block=spi]"
FAILED_PLAN = "agent/plan[block=dma]"


def _ev(seq: int, type_: str, rule_instance: str | None = None, **kwargs: object) -> Event:
    return Event(
        run_id=RUN_ID,
        seq=seq,
        ts=NOW,
        type=type_,  # type: ignore[arg-type]
        rule_instance=rule_instance,
        **kwargs,  # type: ignore[arg-type]
    )


def _full_events() -> list[Event]:
    """A realistic mixed journal: a done chain, a fresh skip, a gate wait, a
    verification failure with a message, a constraint (diverged) failure, a blocked
    dependent, and an open question on a planning failure.
    """
    events = [
        _ev(0, "run_start", payload={"target": "*"}),
        # A done chain: two rules that actually ran.
        _ev(1, "rule_start", RTL_TIMER, payload={}),
        _ev(2, "rule_done", RTL_TIMER, payload={}),
        _ev(3, "rule_start", LINT_TIMER, payload={}),
        _ev(4, "rule_done", LINT_TIMER, payload={}),
        # A fresh (skipped) rule.
        _ev(5, "rule_done", SIM_TIMER, payload={"skipped": "fresh"}),
        # A gate wait.
        _ev(6, "gate_wait", GATE_SPEC, payload={"gate": "spec:uart"}),
        # A constraint failure (hand-edited output).
        _ev(
            7,
            "rule_fail",
            DIVERGED,
            failure_label="constraint",
            payload={"message": "output edited by hand; approve or revert first"},
        ),
        # A verification failure with a message.
        _ev(
            8,
            "rule_fail",
            FAILED_SIM,
            failure_label="verification",
            payload={"message": "check dv/lint failed"},
        ),
        # A planning failure carrying an open question.
        _ev(
            9,
            "rule_fail",
            FAILED_PLAN,
            failure_label="planning",
            payload={
                "message": "gate rejected",
                "open_questions": ["which clock domain owns the DMA FIFO?"],
            },
        ),
        _ev(
            10,
            "run_stop",
            payload={
                "done": 2,
                "skipped_fresh": 1,
                "failed": 3,
                "waiting_gate": 1,
                "blocked": [BLOCKED_CHECK],
            },
        ),
    ]
    return events


def test_build_handoff_derives_all_fields() -> None:
    h = build_handoff(
        _full_events(),
        target="uart",
        approvers={"spec:uart": ["nghia", "nam"]},
        now=NOW,
    )

    assert h.run_id == RUN_ID
    assert h.target == "uart"
    assert h.generated_at == NOW
    assert h.done == (RTL_TIMER, LINT_TIMER)
    assert h.skipped_fresh == (SIM_TIMER,)
    assert h.blocked == (BLOCKED_CHECK,)
    assert h.open_questions == ("which clock domain owns the DMA FIFO?",)

    assert h.waiting_gate == (
        WaitingItem(instance=GATE_SPEC, gate_id="spec:uart", who=("nghia", "nam")),
    )

    # Sorted by instance id: "agent/..." < "digital-rtl/rtl_module..." < "digital-rtl/sim...".
    assert h.failed == (
        FailedItem(instance=FAILED_PLAN, label="planning", message="gate rejected"),
        FailedItem(
            instance=DIVERGED,
            label="constraint",
            message="output edited by hand; approve or revert first",
        ),
        FailedItem(instance=FAILED_SIM, label="verification", message="check dv/lint failed"),
    )


def test_next_steps_order_and_dedup() -> None:
    h = build_handoff(_full_events(), target="uart", now=NOW)

    # Waiting gates first, then failures by label, in instance order; duplicate step
    # templates (two verification-style resumes would collapse) are de-duplicated.
    assert h.next_steps == (
        "`chipgraph approve spec:uart --instance gen/spec[block=uart]`",
        "answer the open questions, then `chipgraph resume 20260928T000000Z-abcdef`",
        "revert the hand edit or approve it, then `chipgraph build uart`",
        "fix the failing check, then `chipgraph resume 20260928T000000Z-abcdef`",
    )


def test_two_verification_failures_collapse_to_one_next_step() -> None:
    events = [
        _ev(0, "run_start", payload={"target": "*"}),
        _ev(
            1,
            "rule_fail",
            FAILED_SIM,
            failure_label="verification",
            payload={"message": "check a failed"},
        ),
        _ev(
            2,
            "rule_fail",
            BLOCKED_CHECK,
            failure_label="verification",
            payload={"message": "check b failed"},
        ),
    ]
    h = build_handoff(events, target="spi", now=NOW)
    assert h.next_steps == (f"fix the failing check, then `chipgraph resume {RUN_ID}`",)


def test_empty_journal() -> None:
    h = build_handoff([], target="uart", now=NOW)
    assert h.run_id == ""
    assert h.done == ()
    assert h.failed == ()
    assert h.next_steps == ("nothing to do: the target is up to date.",)


def test_run_start_only_journal() -> None:
    h = build_handoff([_ev(0, "run_start", payload={"target": "uart"})], target="uart", now=NOW)
    assert h.run_id == RUN_ID
    assert h.done == ()
    assert h.next_steps == ("nothing to do: the target is up to date.",)


def test_everything_done_means_nothing_to_do() -> None:
    events = [
        _ev(0, "run_start", payload={"target": "timer"}),
        _ev(1, "rule_start", RTL_TIMER, payload={}),
        _ev(2, "rule_done", RTL_TIMER, payload={}),
        _ev(3, "rule_done", SIM_TIMER, payload={"skipped": "fresh"}),
        _ev(4, "run_stop", payload={"done": 1, "skipped_fresh": 1}),
    ]
    h = build_handoff(events, target="timer", now=NOW)
    assert h.failed == ()
    assert h.waiting_gate == ()
    assert h.blocked == ()
    assert h.next_steps == ("nothing to do: the target is up to date.",)


def test_render_markdown_snapshot() -> None:
    h = build_handoff(
        _full_events(),
        target="uart",
        approvers={"spec:uart": ["nghia", "nam"]},
        now=NOW,
    )
    text = render_markdown(h)
    assert text == (
        "# Handoff — uart\n"
        "Run `20260928T000000Z-abcdef` — generated 2026-09-28T12:00:00+00:00\n"
        "\n"
        "## Done\n"
        "\n"
        "3 done (1 already up to date, skipped).\n"
        "\n"
        "- digital-rtl/rtl_module[block=timer]\n"
        "- spec-core/lint[block=timer]\n"
        "\n"
        "## Waiting for people\n"
        "\n"
        "- gate `spec:uart` for `gen/spec[block=uart]` — waiting on: nghia, nam\n"
        "\n"
        "## Failed\n"
        "\n"
        "- `agent/plan[block=dma]` [planning]: gate rejected\n"
        "- `digital-rtl/rtl_module[block=gpio]` [constraint]: "
        "output edited by hand; approve or revert first\n"
        "- `digital-rtl/sim[block=spi]` [verification]: check dv/lint failed\n"
        "\n"
        "## Blocked\n"
        "\n"
        "1 blocked:\n"
        "- spec-core/lint[block=spi]\n"
        "\n"
        "## Open questions\n"
        "\n"
        "- which clock domain owns the DMA FIFO?\n"
        "\n"
        "## Next step\n"
        "\n"
        "1. `chipgraph approve spec:uart --instance gen/spec[block=uart]`\n"
        "2. answer the open questions, then `chipgraph resume 20260928T000000Z-abcdef`\n"
        "3. revert the hand edit or approve it, then `chipgraph build uart`\n"
        "4. fix the failing check, then `chipgraph resume 20260928T000000Z-abcdef`\n"
    )


def test_render_markdown_nothing_to_do_omits_empty_sections() -> None:
    h = Handoff(
        run_id=RUN_ID,
        target="timer",
        generated_at=NOW,
        next_steps=("nothing to do: the target is up to date.",),
    )
    text = render_markdown(h)
    assert text == (
        "# Handoff — timer\n"
        "Run `20260928T000000Z-abcdef` — generated 2026-09-28T12:00:00+00:00\n"
        "\n"
        "## Next step\n"
        "\n"
        "1. nothing to do: the target is up to date.\n"
    )


def test_render_status_snapshot() -> None:
    state = replay(_full_events())
    text = render_status(state, target="uart")
    assert text == (
        "FAILED       agent/plan[block=dma]\n"
        "FAILED       digital-rtl/rtl_module[block=gpio]\n"
        "DONE         digital-rtl/rtl_module[block=timer]\n"
        "FAILED       digital-rtl/sim[block=spi]\n"
        "DONE         digital-rtl/sim[block=timer]\n"
        "WAITING_GATE gen/spec[block=uart]\n"
        "DONE         spec-core/lint[block=timer]\n"
        "7 rule(s) for uart: 3 done, 3 failed, 1 waiting_gate\n"
    )


def test_write_handoff_is_atomic_and_returns_path(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    h = build_handoff(_full_events(), target="uart", now=NOW)

    path = write_handoff(layout, h)

    assert path == layout.run_dir(RUN_ID) / "HANDOFF.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == render_markdown(h)
    # No leftover tmp file after a successful write.
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()


def test_write_handoff_overwrites_previous_content(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    h1 = build_handoff([_ev(0, "run_start", payload={"target": "timer"})], target="timer", now=NOW)
    write_handoff(layout, h1)

    h2 = build_handoff(_full_events(), target="uart", now=NOW)
    path = write_handoff(layout, h2)

    assert path.read_text(encoding="utf-8") == render_markdown(h2)
