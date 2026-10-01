"""Run `evals/triage/rules_only.py` on the holdout set, as a black box.

    uv run python docs/triage-claude-code/rules_only_holdout.py [--json]

`rules_only.py` triages the 22 samples of `faults.yml` with the rules only (no model). This
script runs it, unchanged and as a subprocess, on the holdout set instead: it copies the
repository's files to a temporary directory, puts the holdout there in place of the 22
(`holdout.yml` as `faults.yml`, `logs-holdout/` as `logs/`, ids `hold-NN` renamed `log-NN`
because the 22's loader insists on them), runs `rules_only.py --answers` in the copy,
renames the ids back and grades the answers with `evals/triage/grade.py --set holdout`.
Nothing in the checkout is written. Exit code: grade.py's (0 pass, 1 fail, 2 bad input).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "evals" / "triage"
_HOLD_ID = re.compile(r"^(\s*- id: )hold-(\d{2})$", re.MULTILINE)


def _grade_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_grade", EVALS / "grade.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_grade"] = module
    spec.loader.exec_module(module)
    return module


def _copy_repo(dest: Path) -> None:
    """The checkout's tracked and unignored files (no .venv, no caches) under `dest`."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    for rel in filter(None, listed.split("\0")):
        source = REPO / rel
        if source.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / rel)


def _swap_in_holdout(copy: Path) -> None:
    triage = copy / "evals" / "triage"
    text = (EVALS / "holdout.yml").read_text(encoding="utf-8")
    (triage / "faults.yml").write_text(_HOLD_ID.sub(r"\1log-\2", text), encoding="utf-8")
    shutil.rmtree(triage / "logs")
    (triage / "logs").mkdir()
    for path in sorted((EVALS / "logs-holdout").iterdir()):
        name = path.name.replace("hold-", "log-", 1)
        if path.suffix == ".json":
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["id"] = meta["id"].replace("hold-", "log-", 1)
            (triage / "logs" / name).write_text(json.dumps(meta, indent=2) + "\n")
        else:
            shutil.copy2(path, triage / "logs" / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rules_only.py on the holdout set.")
    parser.add_argument("--json", action="store_true", help="print the grade as JSON")
    args = parser.parse_args(argv)
    grade = _grade_module()
    samples = grade.load_samples(grade.SETS["holdout"])
    with tempfile.TemporaryDirectory(prefix="cg-rules-holdout-") as tmp:
        copy = Path(tmp) / "repo"
        _copy_repo(copy)
        _swap_in_holdout(copy)
        answers_path = Path(tmp) / "answers.jsonl"
        done = subprocess.run(
            [sys.executable, "evals/triage/rules_only.py", "--answers", str(answers_path)],
            cwd=copy,
            capture_output=True,
            text=True,
            check=False,
        )
        if not answers_path.is_file():
            print(f"rules_only.py wrote no answers:\n{done.stdout}{done.stderr}", file=sys.stderr)
            return 2
        answers = {}
        for line in answers_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                item["id"] = str(item["id"]).replace("log-", "hold-", 1)
                answers[item["id"]] = item
    report = grade.grade(samples, answers)
    print(json.dumps(report.to_json(), indent=2) if args.json else grade.format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
