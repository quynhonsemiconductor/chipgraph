"""M1-17: the `/triage` holdout set is true to `holdout.yml` and stays out of the rules.

The holdout (`evals/triage/holdout.yml`, logs in `evals/triage/logs-holdout/`) is graded
only: no triage rule may be written against it. Like the 22 samples of `faults.yml`, its
labels come from the injected faults, never from a model. CI cannot regenerate the logs
(they need Verilator), so these tests check the committed ones: ids, labels and class
counts; the logs match the faults file; no machine path; every fault still applies; and no
holdout id or file name appears anywhere under `src/`, so no rule can name one. The last
test regenerates a few logs where the same Verilator is installed, to show they are stable.
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
HOLDOUT = EVALS / "holdout.yml"
LOGS = EVALS / "logs-holdout"
TB = EVALS / "tb"
HOLDOUT_TB = TB / "holdout"
TINYSOC = REPO / "examples" / "tinysoc"
SRC = REPO / "src"
CLASSES = ("infra", "rtl", "tb", "spec")

_ABSOLUTE = re.compile(r"(?<![\w.$~])/(?:Users|home|private|tmp|var|opt|usr|nix|root|Library)/")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_logs = _load("holdout_gen_logs", EVALS / "gen_logs.py")
grade = _load("holdout_grade", EVALS / "grade.py")

SAMPLES = gen_logs.load_samples(HOLDOUT, prefix="hold")
DEFAULT_SAMPLES = gen_logs.load_samples()


# --- the set ------------------------------------------------------------------------------------


def test_about_ten_samples_with_at_least_two_per_class() -> None:
    assert 10 <= len(SAMPLES) <= 14
    counts = Counter(s.label for s in SAMPLES)
    assert set(counts) == set(CLASSES)
    assert min(counts.values()) >= 2, counts


def test_ids_are_neutral_and_numbered_in_order() -> None:
    assert [s.id for s in SAMPLES] == [f"hold-{n:02d}" for n in range(1, len(SAMPLES) + 1)]


def test_the_holdout_is_disjoint_from_the_22() -> None:
    assert not {s.id for s in SAMPLES} & {s.id for s in DEFAULT_SAMPLES}
    seen = {(e.path, e.find, e.replace) for s in DEFAULT_SAMPLES for e in s.edits}
    for sample in SAMPLES:
        for edit in sample.edits:
            assert (edit.path, edit.find, edit.replace) not in seen, sample.id


def test_the_grader_reads_the_same_ids_and_labels() -> None:
    graded = grade.load_samples(grade.SETS["holdout"])
    assert [(s.id, s.label) for s in graded] == [(s.id, s.label) for s in SAMPLES]
    assert grade.SETS["default"] == grade.DEFAULT_FAULTS


def test_the_holdout_file_rejects_default_style_ids(tmp_path: Path) -> None:
    text = HOLDOUT.read_text().replace("id: hold-01", "id: log-01", 1)
    bad = tmp_path / "holdout.yml"
    bad.write_text(text)
    with pytest.raises(gen_logs.FaultError, match="hold-01"):
        gen_logs.load_samples(bad, prefix="hold")


def test_a_ulimit_fault_must_be_a_single_shell_limit(tmp_path: Path) -> None:
    bad = tmp_path / "holdout.yml"
    bad.write_text(
        "samples:\n  - id: hold-01\n    label: infra\n    description: x\n    cmd: 'true'\n"
        "    fault:\n      ulimit: '-f 0; rm -rf x'\n"
    )
    with pytest.raises(gen_logs.FaultError, match="ulimit"):
        gen_logs.load_samples(bad, prefix="hold")
    ulimited = [s for s in SAMPLES if s.ulimit is not None]
    assert ulimited and all(s.label == "infra" for s in ulimited)


# --- the committed logs -------------------------------------------------------------------------


def test_the_committed_logs_match_holdout_yml() -> None:
    expected = {f"{s.id}{suffix}" for s in SAMPLES for suffix in (".log", ".json")}
    assert {p.name for p in LOGS.iterdir()} == expected
    for sample in SAMPLES:
        meta = json.loads((LOGS / f"{sample.id}.json").read_text())
        assert set(meta) == {"id", "cmd", "exit_code", "check", "verilator"}
        assert (meta["id"], meta["cmd"], meta["check"]) == (sample.id, sample.cmd, sample.check)
        assert meta["exit_code"] != 0, f"{sample.id}: a sample log must come from a failing run"
        assert (LOGS / f"{sample.id}.log").read_text().strip(), sample.id
        assert "label" not in meta


def test_no_holdout_file_holds_a_machine_path_or_a_temp_dir() -> None:
    files = [*sorted(LOGS.iterdir()), HOLDOUT, *sorted(HOLDOUT_TB.iterdir())]
    for path in files:
        text = path.read_text()
        assert not _ABSOLUTE.search(text), f"{path.name}: {_ABSOLUTE.search(text)}"
        assert "cg-triage-" not in text and str(Path.home()) not in text, path.name


def _tb_source(sample: gen_logs.Sample, copied: str) -> Path:
    """The eval testbench a `tb/<file>` path in the copy comes from."""
    by_name = {Path(name).name: TB / name for name in sample.tb}
    return by_name[copied.removeprefix("tb/")]


def test_every_fault_still_applies_to_tinysoc() -> None:
    for sample in SAMPLES:
        for name in sample.tb:
            assert (TB / name).is_file(), f"{sample.id}: no testbench {name}"
        for edit in sample.edits:
            if edit.path.startswith("tb/"):
                source = _tb_source(sample, edit.path)
            else:
                source = TINYSOC / edit.path
            assert source.read_text().count(edit.find) == 1, f"{sample.id}: {edit.path}"


def test_the_holdout_testbench_is_not_part_of_tinysoc() -> None:
    names = {p.name for p in HOLDOUT_TB.iterdir()}
    assert names and all(n.endswith(".sv") for n in names)
    assert not any(p.name in names for p in TINYSOC.rglob("*.sv"))


# --- the holdout stays out of the rules ---------------------------------------------------------


def _holdout_names() -> set[str]:
    names = {s.id for s in SAMPLES}
    names |= {HOLDOUT.name, LOGS.name}
    names |= {p.name for p in HOLDOUT_TB.iterdir()}
    names |= {p.stem for p in HOLDOUT_TB.iterdir()}
    return names


def test_no_holdout_id_or_file_name_appears_under_src() -> None:
    names = _holdout_names()
    pattern = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)))
    id_like = re.compile(r"\bhold-\d{2}\b")
    hits = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for match in (pattern.search(text), id_like.search(text)):
            if match:
                hits.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert not hits, hits


# --- grading the holdout ------------------------------------------------------------------------


def test_grade_cli_grades_the_holdout_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = tmp_path / "answers.jsonl"
    lines = [{"id": s.id, "label": s.label, "backend": "large"} for s in SAMPLES]
    answers.write_text("".join(json.dumps(a) + "\n" for a in lines))
    assert grade.main([str(answers), "--set", "holdout", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["correct"], report["total"]) == (len(SAMPLES), len(SAMPLES))
    # The same answers against the 22 answer none of them: a failing grade.
    assert grade.main([str(answers)]) == 1
    capsys.readouterr()
    assert grade.main([str(answers), "--faults", str(HOLDOUT)]) == 0


def test_grade_cli_takes_a_set_or_a_faults_file_not_both(tmp_path: Path) -> None:
    answers = tmp_path / "answers.jsonl"
    answers.write_text("")
    with pytest.raises(SystemExit):
        grade.main([str(answers), "--set", "holdout", "--faults", str(HOLDOUT)])


def test_gen_logs_set_holdout_writes_to_logs_holdout() -> None:
    assert gen_logs.SETS["holdout"] == (HOLDOUT, LOGS, "hold")
    assert gen_logs.SETS["default"] == (gen_logs.FAULTS, gen_logs.LOGS, "log")
    assert len(DEFAULT_SAMPLES) == 22


def test_the_black_box_runner_swaps_the_holdout_in_for_the_22(tmp_path: Path) -> None:
    runner = _load(
        "holdout_rules_only_runner", REPO / "docs" / "triage-claude-code" / "rules_only_holdout.py"
    )
    triage = tmp_path / "evals" / "triage"
    (triage / "logs").mkdir(parents=True)
    (triage / "logs" / "log-01.log").write_text("one of the 22\n")
    runner._swap_in_holdout(tmp_path)
    swapped = gen_logs.load_samples(triage / "faults.yml")  # the 22's loader: `log-NN` ids
    assert [(s.id.replace("log-", "hold-"), s.label) for s in swapped] == [
        (s.id, s.label) for s in SAMPLES
    ]
    for sample in SAMPLES:
        renamed = sample.id.replace("hold-", "log-")
        log = (triage / "logs" / f"{renamed}.log").read_bytes()
        assert log == (LOGS / f"{sample.id}.log").read_bytes()
        assert json.loads((triage / "logs" / f"{renamed}.json").read_text())["id"] == renamed
    assert len(list((triage / "logs").iterdir())) == 2 * len(SAMPLES)


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
def test_regenerated_holdout_logs_are_byte_identical(tmp_path: Path) -> None:
    picks = [
        next(s for s in SAMPLES if s.ulimit is not None),
        next(s for s in SAMPLES if any(t.startswith("holdout/") for t in s.tb)),
        next(s for s in SAMPLES if s.cmd.startswith("chipgraph check") and s.ingest),
    ]
    for sample in picks:
        gen_logs.write_sample(sample, tmp_path)
        for suffix in (".log", ".json"):
            name = sample.id + suffix
            assert (tmp_path / name).read_bytes() == (LOGS / name).read_bytes(), name
