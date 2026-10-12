"""M2-13: problems.yml's schema, the interface-only stimulus generator, and the metrics
arithmetic (pass@1, pass@k, repair turns, cost). No simulator needed."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError
from rtl_eval_helpers import rtl

problems_mod = rtl("problems")
stimulus = rtl("stimulus")
metrics = rtl("metrics")
PROBLEMS = problems_mod.load_problems()
RAW = yaml.safe_load(problems_mod.PROBLEMS.read_text())


# --- problems.yml ---------------------------------------------------------------------


def test_problems_yml_validates_and_names_its_rejections() -> None:
    assert [p.id for p in PROBLEMS.problems] == ["rom_wr_resp", "syscsr_dbg_en"]
    rejected = {r.block: r.reason for r in PROBLEMS.rejected}
    assert "vendor" in rejected["pwm"] and "teacher's AI RTL flow" in rejected["intmap"]
    for problem in PROBLEMS.problems:
        assert problem.plan_path.is_file() and problem.reference_path.is_file()
        assert problem.provenance and problem.why and problem.team_testbench
        assert problem.fake.rtl and problem.fake.tb
    schema = json.loads(problems_mod.schema_json())
    assert "Problem" in schema["$defs"]


def _with(change) -> dict:
    data = json.loads(json.dumps(RAW))
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d["problems"].append(d["problems"][0]), "duplicate problem ids"),
        (lambda d: d["problems"][0].update(clock="nope"), "1-bit input"),
        (lambda d: d["problems"][0].update(extra=1), "Extra inputs"),
        (lambda d: d["problems"][0].update(commit="abc"), "pattern"),
        (lambda d: d["problems"][0]["fake"].update(rtl=["perfect"]), "rtl"),
        (lambda d: d["problems"][0].update(interface=d["problems"][0]["interface"][:2]), "output"),
    ],
)
def test_problems_yml_schema_rejects(change, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        problems_mod.ProblemSet.model_validate(_with(change))


def test_edit_must_match_once() -> None:
    edit = problems_mod.Edit(find="a", replace="b")
    assert edit.apply("xa") == "xb"
    with pytest.raises(ValueError, match="2 times"):
        edit.apply("aa")


# --- stimulus -------------------------------------------------------------------------


@pytest.mark.parametrize("problem", PROBLEMS.problems, ids=lambda p: p.id)
def test_stimulus_is_deterministic_and_interface_only(problem) -> None:
    first = stimulus.generate(problem, 7)
    assert stimulus.generate(problem, 7) == first
    assert stimulus.generate(problem, 8) != first
    assert len(first) == problem.stimulus.cycles
    # Only the interface is read: a problem with no oracle, spec or plan gives the same.
    blind = problem.model_copy(
        update={
            "files": problems_mod.Files(spec="none", oracle="none", filelist="none"),
            "plan": "none",
        }
    )
    assert stimulus.generate(blind, 7) == first
    names = {p.name for p in problem.inputs}
    assert problem.clock not in names
    for vector in first:
        assert set(vector) == names
        for port in problem.inputs:
            assert 0 <= vector[port.name] < (1 << port.width)


@pytest.mark.parametrize("problem", PROBLEMS.problems, ids=lambda p: p.id)
def test_stimulus_resets_first_and_now_and_then(problem) -> None:
    vectors = stimulus.generate(problem, 1)
    active = 0 if problem.reset.active_low else 1
    resets = [v[problem.reset.name] == active for v in vectors]
    assert all(resets[: problem.stimulus.reset_cycles])
    assert not resets[problem.stimulus.reset_cycles]
    later = sum(resets[problem.stimulus.reset_cycles :])
    assert 0 < later < len(vectors) * 0.05


def test_pack_concatenates_inputs_msb_first() -> None:
    problem = PROBLEMS.problem("syscsr_dbg_en")  # inputs: rst_n, dbg_en, dbg_en_mask
    text = stimulus.pack(
        problem,
        [
            {"i_rst_n_por": 1, "i_dbg_en": 0, "i_dbg_en_mask": 1},
            dict.fromkeys(("i_rst_n_por", "i_dbg_en", "i_dbg_en_mask"), 1),
        ],
    )
    assert text == "5\n7\n"
    rom = PROBLEMS.problem("rom_wr_resp")
    width = stimulus.packed_width(rom)
    assert width == sum(p.width for p in rom.inputs) == 90
    line = stimulus.pack(rom, stimulus.generate(rom, 3, cycles=1)).strip()
    assert len(line) == (width + 3) // 4


# --- metrics --------------------------------------------------------------------------


def _pm(pid, attempts, match, cost=0.0, usage=None):
    return metrics.ProblemMetrics(
        id=pid,
        tries_allowed=3,
        attempts=tuple(metrics.Attempt(*a) for a in attempts),
        matches_oracle=match,
        cost_usd=cost,
        usage=usage or {},
        wall_s=10.0,
    )


def test_pass_at_1_pass_at_k_and_repair_turns() -> None:
    first = _pm("a", [(True,)], True)
    repaired = _pm("b", [(False, "constraint"), (True,)], True)
    wrong = _pm("c", [(True,)], False)
    stopped = _pm("d", [(False, "verification"), (False, "verification")], False)
    assert (first.pass_at_1, first.pass_at_k, first.repair_turns) == (True, True, 1)
    assert (repaired.pass_at_1, repaired.pass_at_k, repaired.repair_turns) == (False, True, 2)
    assert repaired.first_rejection == "constraint" and first.first_rejection is None
    assert (wrong.pass_at_1, wrong.pass_at_k) == (False, False)  # accepted, not the oracle
    assert (stopped.accepted, stopped.repair_turns) == (False, 2)
    suite = metrics.suite_metrics([first, repaired, wrong, stopped])
    assert suite["pass_at_1"] == 0.25 and suite["pass_at_k"] == 0.5
    assert suite["repair_turns_mean"] == 1.5
    assert suite["first_rejections"] == {"b": "constraint", "d": "verification"}
    assert suite["wall_s"] == 40.0
    assert metrics.suite_metrics([])["pass_at_1"] is None


def test_cost_and_tokens_per_model() -> None:
    usage = metrics.usage_from_model_usage(
        {
            "claude-sonnet": {
                "inputTokens": 100,
                "outputTokens": 50,
                "cacheReadInputTokens": 1000,
                "cacheCreationInputTokens": 10,
                "costUSD": 0.25,
            },
            "claude-haiku": {"inputTokens": 5, "outputTokens": 5, "costUSD": 0.01},
            "bad": "x",
        }
    )
    assert usage["claude-sonnet"] == {
        "input": 100.0,
        "output": 50.0,
        "cache_read": 1000.0,
        "cache_write": 10.0,
        "cost_usd": 0.25,
    }
    assert "bad" not in usage
    a = _pm("a", [(True,)], True, cost=0.26, usage=usage)
    b = _pm(
        "b", [(True,)], True, cost=0.5, usage={"claude-sonnet": {"output": 10.0, "cost_usd": 0.5}}
    )
    suite = metrics.suite_metrics([a, b])
    assert suite["cost_usd"] == 0.76
    assert suite["usage_by_model"]["claude-sonnet"]["output"] == 60.0
    assert suite["usage_by_model"]["claude-sonnet"]["cost_usd"] == 0.75
    assert suite["tokens"] == 100 + 60 + 1000 + 10 + 10
    assert a.to_json()["tokens"] == 1170


def test_cost_estimate_scales_with_the_text_exchanged() -> None:
    one = metrics.estimate_usd(
        [{"rounds": 1, "author_input_tokens": 5000, "author_output_tokens": 500}]
    )
    two = metrics.estimate_usd(
        [{"rounds": 2, "author_input_tokens": 10000, "author_output_tokens": 1000}]
    )
    assert 0 < one["usd"] < two["usd"] <= 2 * one["usd"] + 1e-9
    assert one["rounds"] == 1 and "sonnet" in one["basis"]
