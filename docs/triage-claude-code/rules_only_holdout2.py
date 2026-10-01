"""Run `evals/triage/rules_only.py`, unchanged and as a black box, on a triage holdout set.

    uv run python docs/triage-claude-code/rules_only_holdout2.py [--set holdout2|holdout]
                                                                  [--answers FILE]

`rules_only.py` triages the 22 samples of `evals/triage/faults.yml` (logs in
`evals/triage/logs/`) with the triage rules only, no model. This runner never reads or
imports it, nor the rules: it copies the repository to a temporary directory, puts the
holdout set in the place of the 22 there (`faults.yml` and `logs/`, ids renamed in order,
`h2-01` -> `log-01`, ...; the logs byte for byte), runs the copy's `rules_only.py` with
this interpreter, maps its answers back to the holdout ids and grades them with
`evals/triage/grade.py --set <set>`. Nothing in the repository is changed.

The default set is `holdout2` (`evals/triage/holdout2.yml`, logs in
`evals/triage/logs-holdout2/`). A holdout is for grading only: report the result with the
run, never commit it, and never change a rule because of it (evals/README.md).
Exit code: grade.py's (0: at least 80 % correct, 1: below), 2 when the run itself fails.
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

import yaml

REPO = Path(__file__).resolve().parents[2]
TRIAGE = REPO / "evals" / "triage"
# set -> (faults file, logs directory); both holdouts share the 22's schema.
SETS = {
    "holdout": (TRIAGE / "holdout.yml", TRIAGE / "logs-holdout"),
    "holdout2": (TRIAGE / "holdout2.yml", TRIAGE / "logs-holdout2"),
}
# The keys of a faults.yml sample; anything else a holdout adds (stage, scope) is dropped.
_SAMPLE_KEYS = ("id", "label", "description", "tb", "ingest", "fault", "cmd", "check")
_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "obj_dir"
)


def _grade_module() -> ModuleType:
    path = TRIAGE / "grade.py"
    spec = importlib.util.spec_from_file_location("rules_only_holdout2_grade", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _swap_in(root: Path, sample_set: str = "holdout2") -> dict[str, str]:
    """Put `sample_set` in place of the 22 under `root`: {`log-NN` id: holdout id}."""
    faults, logs = SETS[sample_set]
    data = yaml.safe_load(faults.read_text(encoding="utf-8"))
    triage = root / "evals" / "triage"
    renamed: dict[str, str] = {}
    samples: list[dict[str, Any]] = []
    for number, item in enumerate(data["samples"], start=1):
        new = f"log-{number:02d}"
        renamed[new] = item["id"]
        samples.append({k: item[k] for k in _SAMPLE_KEYS if k in item} | {"id": new})
    swapped = {"schema_version": data.get("schema_version", 1), "samples": samples}
    if "project" in data:
        swapped["project"] = data["project"]
    (triage / "faults.yml").write_text(yaml.safe_dump(swapped, sort_keys=False), encoding="utf-8")
    target = triage / "logs"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for new, old in renamed.items():
        shutil.copyfile(logs / f"{old}.log", target / f"{new}.log")
        meta = json.loads((logs / f"{old}.json").read_text(encoding="utf-8"))
        meta["id"] = new
        (target / f"{new}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return renamed


def _answers_back(path: Path, renamed: dict[str, str]) -> list[dict[str, Any]]:
    """The copy's answers with their holdout ids (answers for unknown ids are dropped)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            answer = json.loads(line)
            if answer.get("id") in renamed:
                out.append({**answer, "id": renamed[answer["id"]]})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", choices=sorted(SETS), default="holdout2")
    parser.add_argument("--answers", type=Path, default=None, help="also write the answers here")
    args = parser.parse_args(argv)
    grade = _grade_module()
    with tempfile.TemporaryDirectory(prefix="cg-rules-only-") as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(REPO, root, ignore=_IGNORE, symlinks=True)
        renamed = _swap_in(root, args.set)
        raw = Path(tmp) / "answers.jsonl"
        done = subprocess.run(
            [
                sys.executable,
                str(root / "evals" / "triage" / "rules_only.py"),
                "--answers",
                str(raw),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if not raw.is_file():
            print(done.stdout + done.stderr, file=sys.stderr)
            print(
                f"rules_only_holdout2.py: rules_only.py wrote no answers (exit {done.returncode})"
            )
            return 2
        path = Path(tmp) / f"answers-{args.set}.jsonl"
        lines = "".join(json.dumps(a) + "\n" for a in _answers_back(raw, renamed))
        path.write_text(lines, encoding="utf-8")
        report = grade.grade(grade.load_samples(grade.SETS[args.set]), grade.load_answers(path))
    if args.answers is not None:
        args.answers.write_text(lines, encoding="utf-8")
    print(f"== rules only on {args.set}")
    print(grade.format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
