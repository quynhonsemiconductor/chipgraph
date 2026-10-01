"""M1-14: the committed `/triage` sample logs are true to `faults.yml`, and what the rules
alone get right on them.

The labels come from the injected faults (`evals/triage/faults.yml`), never from a model.
`evals/triage/gen_logs.py` needs Verilator, so CI does not regenerate the logs; it checks
the committed ones instead: ids and labels match, every class has at least three, no log
holds a machine path, and every fault still applies to tinysoc. The last test regenerates
a few logs (only where the same Verilator version is installed) to show they are stable.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "evals" / "triage"
LOGS = EVALS / "logs"
TINYSOC = REPO / "examples" / "tinysoc"

_ABSOLUTE = re.compile(r"(?<![\w.$~])/(?:Users|home|private|tmp|var|opt|usr|nix|root|Library)/")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_logs = _load("triage_gen_logs", EVALS / "gen_logs.py")
grade = _load("triage_grade", EVALS / "grade.py")
rules_only = _load("triage_rules_only", EVALS / "rules_only.py")

SAMPLES = gen_logs.load_samples()


# --- the sample set -----------------------------------------------------------------------------


def test_at_least_fifteen_samples_and_three_per_class() -> None:
    assert len(SAMPLES) >= 15
    counts = Counter(s.label for s in SAMPLES)
    assert set(counts) == {"infra", "rtl", "tb", "spec"}
    assert min(counts.values()) >= 3, counts


def test_the_grader_reads_the_same_ids_and_labels() -> None:
    assert [(s.id, s.label) for s in grade.load_samples()] == [(s.id, s.label) for s in SAMPLES]


def test_ids_say_nothing_about_the_label() -> None:
    for sample in SAMPLES:
        assert re.fullmatch(r"log-\d{2}", sample.id)


def test_the_committed_logs_match_faults_yml() -> None:
    expected = {f"{s.id}{suffix}" for s in SAMPLES for suffix in (".log", ".json")}
    assert {p.name for p in LOGS.iterdir()} == expected
    for sample in SAMPLES:
        meta = json.loads((LOGS / f"{sample.id}.json").read_text())
        assert set(meta) == {"id", "cmd", "exit_code", "check", "verilator"}
        assert (meta["id"], meta["cmd"], meta["check"]) == (sample.id, sample.cmd, sample.check)
        assert meta["exit_code"] != 0, f"{sample.id}: a sample log must come from a failing run"
        assert (LOGS / f"{sample.id}.log").read_text().strip(), sample.id
        assert "label" not in meta


def test_no_log_holds_a_machine_path_or_a_temp_dir() -> None:
    for path in sorted(LOGS.iterdir()):
        text = path.read_text()
        assert not _ABSOLUTE.search(text), f"{path.name}: {_ABSOLUTE.search(text)}"
        assert "cg-triage-" not in text and str(Path.home()) not in text, path.name


def test_every_fault_still_applies_to_tinysoc() -> None:
    for sample in SAMPLES:
        for name in sample.tb:
            assert (EVALS / "tb" / name).is_file(), f"{sample.id}: no testbench {name}"
        for edit in sample.edits:
            if edit.path.startswith("tb/"):
                source = EVALS / "tb" / edit.path.removeprefix("tb/")
            else:
                source = TINYSOC / edit.path
            assert source.read_text().count(edit.find) == 1, f"{sample.id}: {edit.path}"
        for rel, _ in sample.chmod:
            assert (TINYSOC / rel).is_file(), f"{sample.id}: {rel}"


def test_the_simulation_samples_use_the_eval_testbenches_not_tinysoc() -> None:
    sims = [s for s in SAMPLES if "--binary" in s.cmd]
    assert sims and all(s.tb for s in sims)
    assert not any(p.name.startswith("tb_") for p in TINYSOC.rglob("*.sv"))


def test_normalise_makes_paths_relative_and_times_stable(tmp_path: Path) -> None:
    root = tmp_path / "tinysoc"
    text = (
        f"%Error: {root}/rtl/a.sv:3:1: oops\ncwd '{root}'\n"
        "/opt/vl/include/verilated_std.sv:1: x\n"
        "- Verilator: $finish at 1ns; walltime 0.012 s; speed 3.1 ms/s\n"
        "- Verilator: cpu 0.004 s on 1 threads; allocated 12 MB\n"
    )
    out = gen_logs.normalise(text, roots=[root], replacements={"/opt/vl": "$VERILATOR_ROOT"})
    assert "%Error: rtl/a.sv:3:1: oops" in out and "cwd '.'" in out
    assert "$VERILATOR_ROOT/include/verilated_std.sv" in out
    assert "walltime N s" in out and "cpu N s on N threads" in out and "allocated N MB" in out
    assert str(tmp_path) not in out


# --- what the rules alone get right ---------------------------------------------------------------


@pytest.fixture(scope="module")
def rule_answers(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    project = rules_only.copy_tinysoc(tmp_path_factory.mktemp("rules") / "tinysoc")
    return {a["id"]: a for a in rules_only.sample_answers(project)}


def _sim_mismatch(sample_id: str) -> bool:
    """A simulation that ran and failed a self-check (the RTL-vs-TB question)."""
    log = (LOGS / f"{sample_id}.log").read_text()
    return bool(re.search(r"^FAIL .*expected .* got ", log, re.MULTILINE))


def test_the_rules_are_never_wrong_on_the_samples(
    rule_answers: dict[str, dict[str, object]],
) -> None:
    for sample in SAMPLES:
        answer = rule_answers[sample.id]
        if answer["label"] is not None:
            assert answer["label"] == sample.label, (sample.id, answer)
            assert answer["backend"] == "rule"


def test_exactly_the_simulation_mismatches_need_the_model(
    rule_answers: dict[str, dict[str, object]],
) -> None:
    undecided = {sid for sid, a in rule_answers.items() if a["label"] is None}
    mismatches = {s.id for s in SAMPLES if _sim_mismatch(s.id)}
    assert mismatches and undecided == mismatches
    # Each such sample shows a run, its stimulus and the failing self-check: decidable
    # against the spec, which is what the model tiers get.
    assert {s.label for s in SAMPLES if s.id in mismatches} == {"rtl", "tb"}


def test_rules_only_accuracy_is_reported(
    rule_answers: dict[str, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    report = grade.grade(grade.load_samples(), rule_answers)
    by_label = Counter(s.label for s in SAMPLES)
    right = Counter(g.expected for g in report.grades if g.passed)
    need_model = sorted(s for s, a in rule_answers.items() if a["label"] is None)
    per_class = ", ".join(f"{c} {right[c]}/{by_label[c]}" for c in ("infra", "rtl", "tb", "spec"))
    with capsys.disabled():
        print(
            f"\n/triage rules only: {report.correct}/{report.total} ({report.accuracy:.0%}); "
            f"per class {per_class}; need the model: {need_model}"
        )
    # High for infra and spec (deterministic evidence); rtl/tb mismatches go to the model.
    assert right["infra"] == by_label["infra"] and right["spec"] == by_label["spec"]
    assert report.by_backend()["rule"]["accuracy"] == 1.0


# --- regeneration (needs the same Verilator) ---------------------------------------------


def _recorded_verilator() -> str | None:
    versions = {json.loads((LOGS / f"{s.id}.json").read_text())["verilator"] for s in SAMPLES}
    return versions.pop() if len(versions) == 1 else None


@pytest.mark.e2e
@pytest.mark.skipif(
    shutil.which("verilator") is None
    or shutil.which("make") is None
    or gen_logs.verilator_version() != _recorded_verilator(),
    reason="needs make and the Verilator version the logs were made with",
)
def test_regenerated_logs_are_byte_identical(tmp_path: Path) -> None:
    by_id = {s.id: s for s in SAMPLES}
    picks = [
        next(s.id for s in SAMPLES if s.cmd.startswith("make lint") and s.label == "rtl"),
        next(s.id for s in SAMPLES if "--binary" in s.cmd and _sim_mismatch(s.id)),
        next(s.id for s in SAMPLES if s.cmd.startswith("chipgraph check") and s.ingest),
    ]
    for sid in picks:
        gen_logs.write_sample(by_id[sid], tmp_path)
        for suffix in (".log", ".json"):
            name = sid + suffix
            assert (tmp_path / name).read_bytes() == (LOGS / name).read_bytes(), name
