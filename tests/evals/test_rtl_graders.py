"""M2-13: the graders against the oracle, with real simulators.

- mutants: deterministic, capped, excludes honoured, comments never edited, each
  differs from the oracle and compiles under Verilator;
- the differential grader (Verilator): the oracle against itself passes; every mutant,
  the broken variant, a width-wrong module and one that does not compile fail;
- the testbench grader (cocotb on Verilator): the reference test passes on the oracle
  and kills every mutant; the broken test fails on the oracle.

Skipped, with the reason, without Verilator (and, for the testbench grader, without the
`sim` extra).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from rtl_eval_helpers import needs_sim, needs_verilator, rtl

problems_mod = rtl("problems")
fixtures = rtl("fixtures")
mutants = rtl("mutants")
diffsim = rtl("diffsim")
PROBLEMS = problems_mod.load_problems().problems
IDS = [p.id for p in PROBLEMS]


def _oracle(problem) -> str:
    return fixtures.strip_header(problem.fixture(problem.files.oracle))


@pytest.mark.parametrize("problem", PROBLEMS, ids=IDS)
def test_mutants_are_deterministic_capped_and_outside_comments(problem) -> None:
    oracle = _oracle(problem)
    found = mutants.generate(problem, oracle)
    assert [m.id for m in found] == [m.id for m in mutants.generate(problem, oracle)]
    assert 4 <= len(found) <= problem.mutants.cap
    assert len({m.id for m in found}) == len(found)
    assert not {m.id for m in found} & set(problem.mutants.exclude)
    assert {m.operator for m in found} >= {"and_or"}
    masked = mutants.mask(oracle)
    for mutant in found:
        assert mutant.text != oracle
        assert re.fullmatch(rf"{re.escape(mutant.operator)}-\d+", mutant.id)
        # the edit is in code: the comment text of the oracle is all still there
        comments = re.findall(r"//[^\n]*", oracle)
        assert all(c in mutant.text for c in comments)
        start = next(i for i, (a, b) in enumerate(zip(oracle, mutant.text, strict=False)) if a != b)
        assert masked[start] == oracle[start], f"{mutant.id} edits a comment"


def test_excluded_mutants_are_reviewed_ids() -> None:
    for problem in PROBLEMS:
        every = {
            m.id for found in mutants.candidates(problem, _oracle(problem)).values() for m in found
        }
        assert set(problem.mutants.exclude) <= every
        for reason in problem.mutants.exclude.values():
            assert len(reason) > 20


@needs_verilator
@pytest.mark.parametrize("problem", PROBLEMS, ids=IDS)
def test_each_mutant_compiles_under_verilator(problem, tmp_path: Path) -> None:
    for mutant in mutants.oracle_mutants(problem):
        path = tmp_path / f"{mutant.id}.sv"
        path.write_text(mutant.text)
        proc = subprocess.run(
            ["verilator", "--lint-only", "-Wno-fatal", "-Wno-lint", "-Wno-style", path.name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{mutant.describe()}: {proc.stderr[-500:]}"


def _width_wrong(problem, oracle: str) -> str:
    out = problem.outputs[0]
    declared = re.search(rf"output\s+logic\s*(\[[^\]]*\])?\s*{out.name}\b", oracle)
    assert declared is not None
    wider = f"output logic [{out.width}:0] {out.name}"
    return oracle.replace(declared[0], wider, 1)


@needs_verilator
@pytest.mark.parametrize("problem", PROBLEMS, ids=IDS)
def test_differential_grader(problem, tmp_path: Path) -> None:
    oracle = _oracle(problem)
    found = mutants.oracle_mutants(problem)
    candidates = {
        "oracle": oracle,
        "broken": problem.fake.broken_rtl.apply(oracle),
        "width": _width_wrong(problem, oracle),
        **{m.id: m.text for m in found},
    }
    results = diffsim.differential(problem, candidates, tmp_path / "diff")
    same = results["oracle"]
    assert same.passed and same.match_fraction == 1.0 and same.seed_fraction == 1.0
    assert len(same.seeds) == len(problem.stimulus.seeds)
    assert sum(s.cycles for s in same.seeds) == problem.stimulus.cycles * len(same.seeds)
    for mutant in found:
        got = results[mutant.id]
        assert got.status == "mismatch", f"{mutant.describe()} survives the stimulus"
        assert got.match_fraction < 1.0
        first = got.to_json()["first_mismatch"]
        assert first["port"] in {p.name for p in problem.outputs}
    assert results["broken"].status == "mismatch"
    assert results["width"].status == "interface"
    assert "bits wide" in results["width"].note

    syntax = diffsim.differential(
        problem, {"bad": oracle.rpartition("endmodule")[0]}, tmp_path / "syntax"
    )["bad"]
    assert syntax.status == "compile_error" and "%Error" in syntax.note


@needs_sim
@pytest.mark.parametrize("problem", PROBLEMS, ids=IDS)
def test_tb_grader(problem, tmp_path: Path) -> None:
    tbsim = rtl("tbsim")
    reference = problem.reference_path.read_text()
    good = tbsim.run_tb(problem, reference, tmp_path / "good")
    assert good.oracle.status == "pass", good.to_json()
    assert good.total_tests >= 3 and good.soundness == 1.0
    assert len(good.mutants) == len(mutants.oracle_mutants(problem))
    assert good.mutation_score == 1.0, good.to_json()["survived"]

    broken = tbsim.run_tb(problem, problem.fake.broken_tb.apply(reference), tmp_path / "bad")
    assert broken.oracle.status == "fail"
    assert 0 < broken.soundness < 1.0
    assert broken.to_json()["failing_on_oracle"]
    # a mutant is killed only by a test that passes on the oracle
    for run in broken.mutants:
        if broken.killed(run):
            assert any(run.tests.get(n) != "pass" for n in broken.passing_on_oracle)
