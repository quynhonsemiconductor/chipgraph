"""M1-17: the evals framework end to end with the fake runtime (dataset -> solver -> scorer
-> report), no model and no network: the run CI does on every PR."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from inspect_ai.log import read_eval_log

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "evals" / "harness"


def _load_harness() -> ModuleType:
    if "chipgraph_evals" in sys.modules:
        return sys.modules["chipgraph_evals"]
    spec = importlib.util.spec_from_file_location(
        "chipgraph_evals", HARNESS / "__init__.py", submodule_search_locations=[str(HARNESS)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chipgraph_evals"] = module
    spec.loader.exec_module(module)
    return module


evals = _load_harness()
MODEL_DECIDED = ("log-05", "log-07", "log-12", "log-16", "log-18")
"""The simulation mismatches no rule decides (evals/README.md): the model tiers answer them."""


def _summary(out: Path) -> dict[str, object]:
    return json.loads((out / "summary.json").read_text())


def _inspect_log(out: Path) -> object:
    [path] = (out / "logs").glob("*.json")
    return read_eval_log(str(path))


# --- ask -------------------------------------------------------------------------------


def test_fake_ask_suite_passes_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "ask"
    result = evals.run_suite("ask", out)
    assert (result.status, result.exit_code) == ("pass", 0)

    summary = _summary(out)
    metrics = summary["metrics"]
    assert (metrics["answerable"], metrics["correct"], metrics["correct_citation_rate"]) == (
        15,
        15,
        1.0,
    )
    assert (metrics["unanswerable"], metrics["said_unknown"], metrics["invented"]) == (5, 5, 0)
    assert summary["verdict"] == "PASS" and summary["runtime"] == "fake"
    assert summary["inspect_ai"] == importlib.metadata.version("inspect-ai")
    assert summary["samples"] == {"total": 20, "run": 20, "not_run": {}}
    assert "PASS" in (out / "summary.md").read_text()

    # The Inspect log holds every sample, scored by the grader.
    log = _inspect_log(out)
    assert log.status == "success" and log.eval.task == "chipgraph_ask"
    assert len(log.samples) == 20
    assert all(s.scores["grade_scorer"].value == "C" for s in log.samples)
    assert log.results.scores[0].metrics["accuracy"].value == 1.0
    assert summary["inspect"]["accuracy"] == 1.0

    # answers.jsonl is in the grader's own format: grade.py passes it too.
    grade = evals.suites.load_grader("ask")
    assert grade.main([str(out / "answers.jsonl")]) == 0


def test_fake_ask_wrong_and_invented_answers_fail(tmp_path: Path) -> None:
    overrides = {
        # a valid citation, but the fact is wrong (the digit 8 is elsewhere)
        "q05": {
            "answer": "pin_out is 32 bits wide; DIR has 8 entries.",
            "citations": ["rtl/tiny_gpio.sv:22"],
            "unknown": False,
        },
        # a citation ask_check accepts, but not one of the expected ones
        "q07": {"answer": "addr is 4 bits wide.", "citations": ["chip.yml:1"], "unknown": False},
        # an invented answer to a question tinysoc has no source for
        "q16": {"answer": "115200 baud.", "citations": ["chip.yml:1"], "unknown": False},
    }
    out = tmp_path / "ask"
    result = evals.run_suite("ask", out, evals.EvalOptions(fake_overrides=overrides))
    assert (result.status, result.exit_code) == ("fail", 1)
    summary = _summary(out)
    assert summary["metrics"]["correct"] == 13 and summary["metrics"]["invented"] == 1
    items = {i["id"]: i for i in summary["items"]}
    assert items["q05"]["missing_facts"] and not items["q05"]["passed"]
    assert items["q07"]["note"] == "no expected citation"
    assert items["q16"]["invented"]
    md = (out / "summary.md").read_text()
    assert "FAIL" in md and "| q16 |" in md


# --- triage ----------------------------------------------------------------------------


def test_fake_triage_suite_passes_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "triage"
    result = evals.run_suite("triage", out)
    assert (result.status, result.exit_code) == ("pass", 0)
    summary = _summary(out)
    assert (summary["metrics"]["correct"], summary["metrics"]["total"]) == (22, 22)
    by_backend = summary["metrics"]["by_backend"]
    assert (by_backend["rule"]["answered"], by_backend["small"]["answered"]) == (17, 5)
    assert {k: v["total"] for k, v in summary["per_class"].items()} == {
        "infra": 6,
        "rtl": 6,
        "tb": 5,
        "spec": 5,
    }
    answers = [json.loads(line) for line in (out / "answers.jsonl").read_text().splitlines()]
    assert sorted(a["id"] for a in answers if a["backend"] == "small") == list(MODEL_DECIDED)
    log = _inspect_log(out)
    assert log.eval.task == "chipgraph_triage" and len(log.samples) == 22


def test_fake_triage_wrong_model_labels_fail(tmp_path: Path) -> None:
    # The fake model answers every simulation mismatch wrongly: 17/22 < 80 %.
    overrides = dict.fromkeys(MODEL_DECIDED, "infra")
    out = tmp_path / "triage"
    result = evals.run_suite("triage", out, evals.EvalOptions(fake_overrides=overrides))
    assert (result.status, result.exit_code) == ("fail", 1)
    summary = _summary(out)
    assert summary["metrics"]["accuracy"] == round(17 / 22, 4)
    # Two of them are RTL bugs, three testbench bugs.
    assert summary["metrics"]["confusion"]["rtl"]["infra"] == 2
    assert summary["metrics"]["confusion"]["tb"]["infra"] == 3
    assert summary["per_class"]["infra"]["accuracy"] == 1.0
    assert (summary["per_class"]["tb"]["correct"], summary["per_class"]["tb"]["total"]) == (2, 5)


def test_only_runs_the_selected_ids(tmp_path: Path) -> None:
    out = tmp_path / "triage"
    options = evals.EvalOptions(only=("log-01", "log-05"))
    result = evals.run_suite("triage", out, options)
    assert result.status == "pass"
    assert [i["id"] for i in _summary(out)["items"]] == ["log-01", "log-05"]
    with pytest.raises(evals.EvalError, match="no such id"):
        evals.run_suite("triage", tmp_path / "x", evals.EvalOptions(only=("log-99",)))
    with pytest.raises(evals.EvalError, match="unknown suite"):
        evals.run_suite("nope", tmp_path / "y")


# --- triage-holdout ----------------------------------------------------------------------


def test_the_holdout_suite_reads_holdout_yml() -> None:
    suite = evals.SUITES["triage-holdout"]
    assert suite.data == REPO / "evals" / "triage" / "holdout.yml"
    assert suite.logs == REPO / "evals" / "triage" / "logs-holdout"
    assert suite.kind == "triage"


def test_a_missing_data_file_is_reported_as_no_data(tmp_path: Path) -> None:
    suite = evals.Suite(
        "triage-holdout", "triage", tmp_path / "holdout.yml", tmp_path / "logs-holdout"
    )
    out = tmp_path / "out"
    result = evals.run_suite(suite, out)
    assert (result.status, result.exit_code) == ("no data", 0)
    summary = _summary(out)
    assert summary["verdict"] == "NO DATA" and "holdout.yml" in summary["reason"]
    assert "NO DATA" in (out / "summary.md").read_text()


def test_a_holdout_file_in_the_faults_schema_runs(tmp_path: Path) -> None:
    # Two held-out samples, made from committed logs under new ids.
    logs = tmp_path / "logs-holdout"
    logs.mkdir()
    src = REPO / "evals" / "triage" / "logs"
    for new, old in (("h-01", "log-01"), ("h-02", "log-05")):
        shutil.copy(src / f"{old}.log", logs / f"{new}.log")
        meta = json.loads((src / f"{old}.json").read_text())
        (logs / f"{new}.json").write_text(json.dumps({**meta, "id": new}))
    data = tmp_path / "holdout.yml"
    data.write_text(
        "schema_version: 1\nsamples:\n"
        "  - {id: h-01, label: rtl, description: lint error}\n"
        "  - {id: h-02, label: rtl, description: simulation mismatch}\n"
    )
    suite = evals.Suite("triage-holdout", "triage", data, logs)
    out = tmp_path / "out"
    result = evals.run_suite(suite, out)
    assert result.status == "pass"
    assert [(i["id"], i["backend"]) for i in _summary(out)["items"]] == [
        ("h-01", "rule"),
        ("h-02", "small"),
    ]
