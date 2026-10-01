"""Triage every committed sample log with the deterministic rules only (no model).

    uv run python evals/triage/rules_only.py [--answers OUT.jsonl] [--json]

Copies examples/tinysoc to a temporary directory (the project the logs came from), runs
`run_triage(..., backend=None)` on each `logs/<id>.log` with its check id, and grades the
answers with `grade.py`. A sample no rule classifies is `undecided` here: it is one the
model tiers have to answer (the simulation mismatches). Exit code: grade.py's.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOGS = HERE / "logs"


def _grade_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_grade", HERE / "grade.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_grade"] = module
    spec.loader.exec_module(module)
    return module


def sample_answers(project: Path, logs: Path = LOGS) -> list[dict[str, Any]]:
    """One rules-only answer per committed sample log, triaged in the tinysoc copy `project`."""
    from chipgraph.app.context import AppContext
    from chipgraph.packs.assist.triage import run_triage

    grade = _grade_module()
    ctx = AppContext.load(project)
    answers = []
    for sample in grade.load_samples():
        meta = json.loads((logs / f"{sample.id}.json").read_text(encoding="utf-8"))
        text = (logs / f"{sample.id}.log").read_text(encoding="utf-8")
        report = run_triage(ctx, text, check_id=meta.get("check"), backend=None)
        answers.append(
            {
                "id": sample.id,
                "label": report.label,
                "backend": report.backend,
                "status": report.status,
                "rule": report.rule,
                "confidence": report.confidence,
                "low_confidence": report.low_confidence,
            }
        )
    return answers


def copy_tinysoc(dest: Path) -> Path:
    """examples/tinysoc at `dest`, as its own git repo."""
    shutil.copytree(REPO / "examples" / "tinysoc", dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triage the sample logs with rules only.")
    parser.add_argument("--answers", type=Path, help="also write the answers here (JSONL)")
    parser.add_argument("--json", action="store_true", help="print the grade as JSON")
    args = parser.parse_args(argv)
    grade = _grade_module()
    with tempfile.TemporaryDirectory(prefix="cg-triage-rules-") as tmp:
        answers = sample_answers(copy_tinysoc(Path(tmp) / "tinysoc"))
    if args.answers:
        args.answers.write_text("".join(json.dumps(a) + "\n" for a in answers))
    report = grade.grade(grade.load_samples(), {a["id"]: a for a in answers})
    print(json.dumps(report.to_json(), indent=2) if args.json else grade.format_report(report))
    undecided = [a["id"] for a in answers if a["label"] is None]
    if not args.json:
        print(f"need a model (no rule decides): {', '.join(undecided) or 'none'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
