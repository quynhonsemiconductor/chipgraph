"""The eval suites: their data files, their graders, and their items as an Inspect dataset."""

from __future__ import annotations

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

SuiteKind = Literal["ask", "triage"]


def load_grader(kind: SuiteKind) -> ModuleType:
    """`evals/<kind>/grade.py`, as the module `<kind>_grade` (reused if already loaded)."""
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

    @property
    def grader(self) -> ModuleType:
        return load_grader(self.kind)

    def has_data(self) -> bool:
        return self.data.is_file()

    def items(self) -> list[Any]:
        """The grader's items: `Question`s (ask) or `Sample`s (triage), in file order."""
        if self.kind == "ask":
            return list(self.grader.load_questions(self.data))
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
        """The command arguments for one item: the question, or `logs/<id>.log [check]`."""
        if self.kind == "ask":
            return str(item.question)
        check = self.check_id(item.id)
        return f"logs/{item.id}.log" + (f" {check}" if check else "")

    def target(self, item: Any) -> list[str]:
        """What a correct answer holds: the expected citations (or `unknown`), the label."""
        if self.kind == "ask":
            return ["unknown"] if item.unknown else list(item.expected)
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
}
"""The suites `chipgraph eval` knows, by name."""


__all__ = ["EVALS", "PLUGIN", "REPO", "SUITES", "TINYSOC", "Suite", "SuiteKind", "load_grader"]
