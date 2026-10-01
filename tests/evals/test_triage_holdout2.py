"""The second `/triage` holdout set (holdout2) is true to `holdout2.yml` and stays out of the rules.

`evals/triage/holdout2.yml` (logs in `evals/triage/logs-holdout2/`) is graded only. It
measures triage after holdout 1 was used to change it, so it must stay clean: labels come
from the injected faults, every fault is new (none repeats one of `faults.yml` or
`holdout.yml`), and no holdout2 id or file name appears under `src/`. Half the set is
simulation-only (`stage: sim`, the RTL lints clean), RTL and testbench faults alike, some
through `tiny_top` (`scope: top`); these tests check those counts from the YAML fields and
the committed logs. CI cannot regenerate the logs (they need Verilator); the e2e tests do
where the recorded Verilator is installed, and check that the sim-only faults lint clean.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "evals" / "triage"
HOLDOUT2 = EVALS / "holdout2.yml"
LOGS = EVALS / "logs-holdout2"
TB = EVALS / "tb"
HOLDOUT2_TB = TB / "holdout2"
TINYSOC = REPO / "examples" / "tinysoc"
SRC = REPO / "src"
CLASSES = ("infra", "rtl", "tb", "spec")
STAGES = ("lint", "build", "sim", "check")
SCOPES = ("block", "top", "chip")

_ABSOLUTE = re.compile(r"(?<![\w.$~])/(?:Users|home|private|tmp|var|opt|usr|nix|root|Library)/")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_logs = _load("holdout2_gen_logs", EVALS / "gen_logs.py")
grade = _load("holdout2_grade", EVALS / "grade.py")

SAMPLES = gen_logs.load_samples(HOLDOUT2, prefix="h2")
RAW = {item["id"]: item for item in yaml.safe_load(HOLDOUT2.read_text())["samples"]}
OTHERS = [
    *gen_logs.load_samples(),
    *gen_logs.load_samples(EVALS / "holdout.yml", prefix="hold"),
]


def _field(sample: Any, key: str) -> str:
    return str(RAW[sample.id][key])


def _sim(label: str) -> list[Any]:
    return [s for s in SAMPLES if s.label == label and _field(s, "stage") == "sim"]


# --- the set ------------------------------------------------------------------------------------


def test_twelve_samples_three_per_class_with_neutral_ids() -> None:
    assert [s.id for s in SAMPLES] == [f"h2-{n:02d}" for n in range(1, 13)]
    assert Counter(s.label for s in SAMPLES) == dict.fromkeys(CLASSES, 3)


def test_every_sample_declares_its_stage_and_scope() -> None:
    for sample in SAMPLES:
        assert _field(sample, "stage") in STAGES, sample.id
        assert _field(sample, "scope") in SCOPES, sample.id


def test_rtl_and_tb_are_told_apart_in_simulation_some_through_the_top() -> None:
    rtl, tb = _sim("rtl"), _sim("tb")
    assert len(rtl) >= 2 and len(tb) >= 2, (rtl, tb)
    on_top = [s for s in (*rtl, *tb) if _field(s, "scope") == "top"]
    assert len(on_top) >= 2, on_top
    # Both labels fail through tiny_top, so the scope alone does not give the label away.
    assert {s.label for s in on_top} == {"rtl", "tb"}


def test_sim_samples_run_a_holdout2_testbench_that_reached_its_checks() -> None:
    for sample in (s for s in SAMPLES if _field(s, "stage") == "sim"):
        [name] = sample.tb
        assert name.startswith("holdout2/"), sample.id
        module = Path(name).stem
        assert f"--top-module {module}" in sample.cmd and f"obj_dir/V{module}" in sample.cmd
        # The build passed and the simulation started: the testbench printed its banner.
        log = (LOGS / f"{sample.id}.log").read_text()
        assert f"{module}: start\n" in log, sample.id
        top = _field(sample, "scope") == "top"
        assert ("-f filelists/top.f" in sample.cmd) == top, sample.id
        assert ("tiny_top dut" in (TB / name).read_text()) == top, sample.id


def test_the_testbenches_report_in_different_styles() -> None:
    styles = {
        "tb_top_irq.sv": ("TIMEOUT", "$fatal"),
        "tb_top_scoreboard.sv": ("MISMATCH", "$stop"),
        "tb_timer_sva.sv": ("assert property", "$error"),
        "tb_gpio_pins.sv": ("$fatal",),
    }
    assert {p.name for p in HOLDOUT2_TB.iterdir()} == set(styles)
    for name, marks in styles.items():
        text = (HOLDOUT2_TB / name).read_text()
        assert all(m in text for m in marks), name
    assert "$fatal" not in (HOLDOUT2_TB / "tb_top_scoreboard.sv").read_text()
    assert "$fatal" not in (HOLDOUT2_TB / "tb_timer_sva.sv").read_text()


def test_each_label_matches_the_files_its_fault_touches() -> None:
    for sample in SAMPLES:
        paths = {e.path for e in sample.edits} | set(sample.delete)
        if sample.label == "rtl":
            assert paths and all(p.startswith("rtl/") for p in paths), sample.id
        elif sample.label == "tb":
            assert paths and all(p.startswith("tb/") for p in paths), sample.id
        elif sample.label == "spec":
            assert paths and all(p.startswith("doc/specs/") for p in paths), sample.id
        else:
            assert not any(p.startswith(("rtl/", "tb/", "doc/")) for p in paths), sample.id
            assert paths or sample.env or sample.path_only or sample.ulimit, sample.id


def test_no_fault_repeats_one_of_the_other_sets() -> None:
    assert len(OTHERS) == 34
    assert not {s.id for s in SAMPLES} & {s.id for s in OTHERS}
    seen_edits = {(e.path, e.find, e.replace) for s in OTHERS for e in s.edits}
    seen_faults = [json.dumps(s.raw.get("fault"), sort_keys=True) for s in OTHERS]
    seen_text = {s.description for s in OTHERS}
    for sample in SAMPLES:
        for edit in sample.edits:
            assert (edit.path, edit.find, edit.replace) not in seen_edits, sample.id
        assert json.dumps(sample.raw.get("fault"), sort_keys=True) not in seen_faults, sample.id
        assert sample.description not in seen_text, sample.id
    # Its testbenches are its own: none shares a file name with another set's.
    others_tb = {Path(name).name for s in OTHERS for name in s.tb}
    assert not {Path(name).name for s in SAMPLES for name in s.tb} & others_tb


def test_the_grader_reads_the_same_ids_and_labels() -> None:
    graded = grade.load_samples(grade.SETS["holdout2"])
    assert [(s.id, s.label) for s in graded] == [(s.id, s.label) for s in SAMPLES]
    assert grade.SETS["holdout2"] == grade.HOLDOUT2_FAULTS == HOLDOUT2


def test_gen_logs_set_holdout2_writes_to_logs_holdout2() -> None:
    assert gen_logs.SETS["holdout2"] == (HOLDOUT2, LOGS, "h2")
    assert gen_logs.SETS["holdout"] == (gen_logs.HOLDOUT, gen_logs.LOGS_HOLDOUT, "hold")
    assert gen_logs.SETS["default"] == (gen_logs.FAULTS, gen_logs.LOGS, "log")


def test_an_env_fault_sets_names_and_never_the_base_environment(tmp_path: Path) -> None:
    head = "samples:\n  - id: h2-01\n    label: infra\n    description: x\n    cmd: 'true'\n"
    bad = tmp_path / "holdout2.yml"
    for env in ("{lower: x}", "{PATH: /bin}", "{HOME: x}", "[A]", "{N: 1}"):
        bad.write_text(head + f"    fault:\n      env: {env}\n")
        with pytest.raises(gen_logs.FaultError, match="env"):
            gen_logs.load_samples(bad, prefix="h2")
    bad.write_text(head + "    fault:\n      env: {TOOL_ROOT: x}\n")
    [ok] = gen_logs.load_samples(bad, prefix="h2")
    assert ok.env == (("TOOL_ROOT", "x"),)
    with_env = [s for s in SAMPLES if s.env]
    assert with_env and all(s.label == "infra" for s in with_env)


# --- the committed logs -------------------------------------------------------------------------


def test_the_committed_logs_match_holdout2_yml() -> None:
    expected = {f"{s.id}{suffix}" for s in SAMPLES for suffix in (".log", ".json")}
    assert {p.name for p in LOGS.iterdir()} == expected
    for sample in SAMPLES:
        meta = json.loads((LOGS / f"{sample.id}.json").read_text())
        assert set(meta) == {"id", "cmd", "exit_code", "check", "verilator"}
        assert (meta["id"], meta["cmd"], meta["check"]) == (sample.id, sample.cmd, sample.check)
        assert meta["exit_code"] != 0, f"{sample.id}: a sample log must come from a failing run"
        assert (LOGS / f"{sample.id}.log").read_text().strip(), sample.id
        assert "label" not in meta and "stage" not in meta


def test_no_holdout2_file_holds_a_machine_path_or_a_temp_dir() -> None:
    files = [*sorted(LOGS.iterdir()), HOLDOUT2, *sorted(HOLDOUT2_TB.iterdir())]
    for path in files:
        text = path.read_text()
        assert not _ABSOLUTE.search(text), f"{path.name}: {_ABSOLUTE.search(text)}"
        assert "cg-triage-" not in text and str(Path.home()) not in text, path.name


def _tb_source(sample: Any, copied: str) -> Path:
    by_name = {Path(name).name: TB / name for name in sample.tb}
    return by_name[copied.removeprefix("tb/")]


def test_every_fault_still_applies_to_tinysoc() -> None:
    for sample in SAMPLES:
        for name in sample.tb:
            assert (TB / name).is_file(), f"{sample.id}: no testbench {name}"
        for edit in sample.edits:
            source = _tb_source(sample, edit.path) if edit.path.startswith("tb/") else None
            source = source or TINYSOC / edit.path
            assert source.read_text().count(edit.find) == 1, f"{sample.id}: {edit.path}"


def test_the_holdout2_testbenches_are_not_part_of_tinysoc() -> None:
    names = {p.name for p in HOLDOUT2_TB.iterdir()}
    assert names and all(n.endswith(".sv") for n in names)
    assert not any(p.name in names for p in TINYSOC.rglob("*.sv"))


# --- holdout2 stays out of the rules ------------------------------------------------------------


def test_no_holdout2_id_or_file_name_appears_under_src() -> None:
    names = {s.id for s in SAMPLES} | {HOLDOUT2.name, LOGS.name}
    names |= {p.name for p in HOLDOUT2_TB.iterdir()} | {p.stem for p in HOLDOUT2_TB.iterdir()}
    pattern = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)))
    id_like = re.compile(r"\bh2-\d{2}\b")
    hits = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for match in (pattern.search(text), id_like.search(text)):
            if match:
                hits.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert not hits, hits


# --- grading and running it ---------------------------------------------------------------------


def test_grade_cli_grades_the_holdout2_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = tmp_path / "answers.jsonl"
    lines = [{"id": s.id, "label": s.label, "backend": "large"} for s in SAMPLES]
    answers.write_text("".join(json.dumps(a) + "\n" for a in lines))
    assert grade.main([str(answers), "--set", "holdout2", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["correct"], report["total"]) == (12, 12)
    assert grade.main([str(answers), "--set", "holdout"]) == 1  # other ids: all unanswered
    capsys.readouterr()


def test_the_suite_triage_holdout2_reads_holdout2() -> None:
    suites = _load("holdout2_suites", REPO / "evals" / "harness" / "suites.py")
    suite = suites.SUITES["triage-holdout2"]
    assert (suite.kind, suite.data, suite.logs) == ("triage", HOLDOUT2, LOGS)
    assert suite.has_data()
    assert [s.id for s in suite.items()] == [s.id for s in SAMPLES]
    check = next(s for s in SAMPLES if s.check)
    assert suite.prompt(check) == f"logs/{check.id}.log {check.check}"


def test_the_shell_runners_know_holdout2() -> None:
    run_sh = (REPO / "docs" / "triage-claude-code" / "run.sh").read_text()
    assert 'holdout2) LOG_DIR="$REPO/evals/triage/logs-holdout2"' in run_sh
    report = _load("holdout2_report", REPO / "docs" / "triage-claude-code" / "report.py")
    assert report.LOGS_BY_SET["holdout2"] == LOGS
    script = (REPO / "evals" / "run-claude-code.sh").read_text()
    assert 'SUITES="${SUITES:-ask triage triage-holdout triage-holdout2}"' in script


def test_the_black_box_runner_swaps_holdout2_in_for_the_22(tmp_path: Path) -> None:
    runner = _load(
        "holdout2_rules_only_runner",
        REPO / "docs" / "triage-claude-code" / "rules_only_holdout2.py",
    )
    triage = tmp_path / "evals" / "triage"
    (triage / "logs").mkdir(parents=True)
    (triage / "logs" / "log-01.log").write_text("one of the 22\n")
    renamed = runner._swap_in(tmp_path, "holdout2")
    assert renamed == {f"log-{n:02d}": f"h2-{n:02d}" for n in range(1, 13)}
    swapped = gen_logs.load_samples(triage / "faults.yml")  # the 22's loader: `log-NN` ids
    assert [(renamed[s.id], s.label, s.cmd) for s in swapped] == [
        (s.id, s.label, s.cmd) for s in SAMPLES
    ]
    for new, old in renamed.items():
        assert (triage / "logs" / f"{new}.log").read_bytes() == (LOGS / f"{old}.log").read_bytes()
        assert json.loads((triage / "logs" / f"{new}.json").read_text())["id"] == new
    assert len(list((triage / "logs").iterdir())) == 24
    answers = tmp_path / "answers.jsonl"
    answers.write_text('{"id": "log-02", "label": "infra"}\n{"id": "log-99", "label": "tb"}\n')
    assert runner._answers_back(answers, renamed) == [{"id": "h2-02", "label": "infra"}]


# --- regeneration (needs the same Verilator) ---------------------------------------------


def _recorded_verilator() -> str | None:
    versions = {json.loads((LOGS / f"{s.id}.json").read_text())["verilator"] for s in SAMPLES}
    return versions.pop() if len(versions) == 1 else None


_NEEDS_TOOLS = pytest.mark.skipif(
    shutil.which("verilator") is None
    or shutil.which("make") is None
    or gen_logs.verilator_version() != _recorded_verilator(),
    reason="needs make and the Verilator version the logs were made with",
)


@pytest.mark.e2e
@_NEEDS_TOOLS
def test_the_sim_only_faults_lint_clean(tmp_path: Path) -> None:
    for sample in (s for s in SAMPLES if _field(s, "stage") == "sim"):
        root = tmp_path / sample.id
        gen_logs._prepare(sample, root)
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LC_ALL": "C"}
        done = subprocess.run(
            ["make", "lint-all"], cwd=root, env=env, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"{sample.id}:\n{done.stdout}{done.stderr}"


@pytest.mark.e2e
@_NEEDS_TOOLS
def test_regenerated_holdout2_logs_are_byte_identical(tmp_path: Path) -> None:
    picks = [
        next(s for s in SAMPLES if s.env),
        next(s for s in SAMPLES if _field(s, "stage") == "sim" and _field(s, "scope") == "top"),
        next(s for s in SAMPLES if _field(s, "stage") == "sim" and _field(s, "scope") == "block"),
        next(s for s in SAMPLES if s.cmd.startswith("chipgraph check") and s.ingest),
    ]
    for sample in picks:
        gen_logs.write_sample(sample, tmp_path)
        for suffix in (".log", ".json"):
            name = sample.id + suffix
            assert (tmp_path / name).read_bytes() == (LOGS / name).read_bytes(), name
