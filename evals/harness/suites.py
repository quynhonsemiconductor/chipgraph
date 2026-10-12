"""The eval suites: their data files, their graders, and their items as an Inspect dataset."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

HERE = Path(__file__).resolve().parent
EVALS = HERE.parent
REPO = EVALS.parent
TINYSOC = REPO / "examples" / "tinysoc"
PLUGIN = REPO / "plugin"

SuiteKind = Literal["ask", "triage", "review", "rtl", "tb"]
RTL_PACKAGE = "chipgraph_evals_rtl"


def rtl_module(name: str) -> ModuleType:
    """A module of `evals/rtl/` (the `rtl` and `tb` suites), loaded as part of the package
    `chipgraph_evals_rtl`."""
    if RTL_PACKAGE not in sys.modules:
        rtl = EVALS / "rtl"
        spec = importlib.util.spec_from_file_location(
            RTL_PACKAGE, rtl / "__init__.py", submodule_search_locations=[str(rtl)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[RTL_PACKAGE] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{RTL_PACKAGE}.{name}")


def load_grader(kind: SuiteKind) -> ModuleType:
    """`evals/<kind>/grade.py`, as the module `<kind>_grade` (reused if already loaded);
    `evals/rtl/grade.py` for both `rtl` and `tb`."""
    if kind in ("rtl", "tb"):
        return rtl_module("grade")
    name = f"{kind}_grade"
    path = EVALS / kind / "grade.py"
    loaded = sys.modules.get(name)
    if loaded is not None and Path(str(getattr(loaded, "__file__", ""))).resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Suite:
    """One eval suite: `data` is its YAML file, `logs` the sample logs of a triage suite."""

    name: str
    kind: SuiteKind
    data: Path
    logs: Path | None = None
    description: str = ""
    budget_usd: float = 3.0
    """The default `--budget-usd` of a claude-code run of this suite."""

    @property
    def grader(self) -> ModuleType:
        return load_grader(self.kind)

    def has_data(self) -> bool:
        return self.data.is_file()

    def items(self) -> list[Any]:
        """The grader's items: `Question`s (ask) or `Sample`s (triage, review), in file order."""
        if self.kind == "ask":
            return list(self.grader.load_questions(self.data))
        if self.kind in ("rtl", "tb"):
            return list(self.grader.load_samples(self.data, self.kind))
        return list(self.grader.load_samples(self.data))

    def log_path(self, sample_id: str) -> Path:
        assert self.logs is not None
        return self.logs / f"{sample_id}.log"

    def check_id(self, sample_id: str) -> str | None:
        """The chipgraph check id that produced a triage sample's log, if any."""
        assert self.logs is not None
        meta = self.logs / f"{sample_id}.json"
        if not meta.is_file():
            return None
        check = json.loads(meta.read_text(encoding="utf-8")).get("check")
        return str(check) if check else None

    def prompt(self, item: Any) -> str:
        """The command arguments for one item: the question, `logs/<id>.log [check]`, or
        the review target (`digital-rtl/review[block=<block>]`)."""
        if self.kind == "ask":
            return str(item.question)
        if self.kind in ("review", "rtl", "tb"):
            return str(item.target)
        check = self.check_id(item.id)
        return f"logs/{item.id}.log" + (f" {check}" if check else "")

    def target(self, item: Any) -> list[str]:
        """What a correct answer holds: the expected citations (or `unknown`), the label,
        or where the planted defect is (`clean` for a clean sample)."""
        if self.kind == "ask":
            return ["unknown"] if item.unknown else list(item.expected)
        if self.kind == "review":
            return ["clean"] if item.clean else [f"{item.at_file}:{item.at_line}"]
        if self.kind in ("rtl", "tb"):
            return [f"{item.problem.files.oracle} ({item.problem.commit[:7]})"]
        return [str(item.label)]


SUITES: dict[str, Suite] = {
    "ask": Suite(
        "ask",
        "ask",
        EVALS / "ask" / "tinysoc.yml",
        description="/ask on tinysoc: >= 90 % correct citations, 0 invented answers",
    ),
    "triage": Suite(
        "triage",
        "triage",
        EVALS / "triage" / "faults.yml",
        EVALS / "triage" / "logs",
        description="/triage on the labelled tinysoc logs: >= 80 % correct",
    ),
    "triage-holdout": Suite(
        "triage-holdout",
        "triage",
        EVALS / "triage" / "holdout.yml",
        EVALS / "triage" / "logs-holdout",
        description="/triage on the held-out logs: >= 80 % correct",
    ),
    "triage-holdout2": Suite(
        "triage-holdout2",
        "triage",
        EVALS / "triage" / "holdout2.yml",
        EVALS / "triage" / "logs-holdout2",
        description="/triage on the second held-out logs: >= 80 % correct",
    ),
    "review": Suite(
        "review",
        "review",
        EVALS / "review" / "defects.yml",
        description=(
            "Critic review of tinysoc diffs with planted defects: recall >= 70 % "
            "(>= 12 of 16), <= 1 false alarm on 4 clean diffs"
        ),
    ),
    "review-holdout2": Suite(
        "review-holdout2",
        "review",
        EVALS / "review" / "defects2.yml",
        description=(
            "Critic review of the second, blind set of tinysoc diffs: recall >= 70 % "
            "(>= 12 of 17), <= 1 false alarm on 8 clean diffs"
        ),
    ),
    "rtl": Suite(
        "rtl",
        "rtl",
        EVALS / "rtl" / "problems.yml",
        description=(
            "RTL Author (digital-rtl/rtl_module) on finished QSoC blocks, differential "
            "against the human RTL: pass@k >= 50 %"
        ),
        budget_usd=10.0,
    ),
    "tb": Suite(
        "tb",
        "tb",
        EVALS / "rtl" / "problems.yml",
        description=(
            "testbench Author (dv/tb_module) on finished QSoC blocks, run on the human RTL "
            "and its mutants: pass@k >= 50 %, mean mutation score >= 50 %"
        ),
        budget_usd=10.0,
    ),
}
"""The suites `chipgraph eval` knows, by name.

`review-holdout2` (`review/defects2.yml`) is the review suite's second set, written by an
author who never read the Critic's prompt, role, skill, context builder or review tests;
it is graded only, with the thresholds of `review`, unchanged: recall >= 70 % (at least
12 of its 17 defects) and at most 1 false alarm on its 8 clean diffs. Both runtimes run
it exactly as they run `review`.

`rtl` and `tb` (task M2-13) share `rtl/problems.yml`: the RTL Author and the testbench
Author, each on its own, on leaf modules of finished QSoC blocks, graded by
`rtl/grade.py` against the RTL people wrote. Not in the weekly workflow (cost).
"""


__all__ = [
    "EVALS",
    "PLUGIN",
    "REPO",
    "SUITES",
    "TINYSOC",
    "Suite",
    "SuiteKind",
    "load_grader",
    "rtl_module",
]
