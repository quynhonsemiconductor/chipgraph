"""`problems.yml`: the problems of the `rtl` and `tb` evals, as pydantic models.

One `Problem` per leaf module of a finished QSoC block whose RTL people wrote (the
oracle): the source commit, the files (MAS, oracle, filelist), the clock and reset
convention, the module's interface, the approved-plan fixture (`plans/<id>.plan.yml`,
written by hand from the MAS), the hand-written reference test (`reference/<id>.py`),
the stimulus settings, the mutant settings and the fake authors' scripts. `rejected`
lists the blocks that were looked at and not used, with the reason.

The JSON Schema is `python -m rtl.problems` (from `evals/`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HERE = Path(__file__).resolve().parent
PROBLEMS = HERE / "problems.yml"
PLANS = HERE / "plans"
REFERENCE = HERE / "reference"

RtlReply = Literal["oracle", "broken", "syntax_error"]
"""What a fake RTL author writes on one try: the oracle, the problem's broken variant
(lint-clean, wrong behaviour), or a module with a syntax error (the rule rejects it)."""
TbReply = Literal["reference", "broken", "unknown_port"]
"""What a fake testbench author writes on one try: the reference test, its broken
variant (fails on the oracle), or the reference with an access to a port the interface
does not have (`tb_static` rejects it)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Port(_Model):
    """One port of the module (the problem's interface)."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    direction: Literal["input", "output"]
    width: int = Field(ge=1)


class Reset(_Model):
    name: str
    active_low: bool = True
    kind: Literal["async", "sync"] = "async"


class Files(_Model):
    """Paths in the source repository; the fixtures are `fixtures/<block>/<path>`."""

    spec: str
    oracle: str
    filelist: str


class Stimulus(_Model):
    """The differential simulation's random stimulus (`stimulus.py`)."""

    cycles: int = Field(default=1000, ge=10)
    seeds: tuple[int, ...] = Field(default=(1, 2, 3, 4), min_length=1)
    reset_cycles: int = Field(default=2, ge=1, description="Reset held at the start.")
    reset_rate: float = Field(
        default=0.01, ge=0, le=0.5, description="Chance per cycle of a reset pulse later."
    )


class Mutants(_Model):
    cap: int = Field(default=8, ge=1)
    exclude: dict[str, str] = Field(
        default_factory=dict,
        description="Mutant ids left out, each with the reason (reviewed by hand).",
    )


class Edit(_Model):
    """A find/replace on a text: `find` must occur exactly once."""

    find: str = Field(min_length=1)
    replace: str
    why: str = ""

    def apply(self, text: str) -> str:
        count = text.count(self.find)
        if count != 1:
            raise ValueError(f"edit: {self.find!r} occurs {count} times, not once")
        return text.replace(self.find, self.replace)


class Fake(_Model):
    """The fake runtime's scripted authors: one reply per try."""

    rtl: tuple[RtlReply, ...] = Field(default=("oracle",), min_length=1)
    tb: tuple[TbReply, ...] = Field(default=("reference",), min_length=1)
    broken_rtl: Edit
    broken_tb: Edit


class Problem(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    block: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    pull_request: int | None = None
    files: Files
    clock: str
    reset: Reset
    interface: tuple[Port, ...] = Field(min_length=2)
    plan: str = Field(description="The approved-plan fixture, under plans/.")
    reference_tb: str = Field(description="The hand-written reference test, under reference/.")
    stimulus: Stimulus = Field(default_factory=Stimulus)
    mutants: Mutants = Field(default_factory=Mutants)
    fake: Fake
    why: str = Field(min_length=1, description="Why this block was chosen.")
    provenance: str = Field(min_length=1, description="Who wrote the oracle, per the record.")
    team_testbench: str = Field(min_length=1, description="The team's testbench, if any.")
    notes: str = ""

    @model_validator(mode="after")
    def _ports(self) -> Problem:
        names = [p.name for p in self.interface]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.id}: duplicate port names")
        for name in (self.clock, self.reset.name):
            port = next((p for p in self.interface if p.name == name), None)
            if port is None or port.direction != "input" or port.width != 1:
                raise ValueError(f"{self.id}: {name} must be a 1-bit input of the interface")
        if not any(p.direction == "output" for p in self.interface):
            raise ValueError(f"{self.id}: the interface has no output")
        return self

    def fixture(self, path: str) -> str:
        """The fixture of a source file of this problem, under `fixtures/`."""
        return f"{self.block}/{path}"

    @property
    def inputs(self) -> tuple[Port, ...]:
        """The inputs the stimulus drives: every input but the clock."""
        return tuple(p for p in self.interface if p.direction == "input" and p.name != self.clock)

    @property
    def outputs(self) -> tuple[Port, ...]:
        return tuple(p for p in self.interface if p.direction == "output")

    @property
    def rtl_output(self) -> str:
        """Where `digital-rtl/rtl_module` writes the module in the eval project."""
        return f"{Path(self.files.oracle).parent.as_posix()}/{self.module}.sv"

    @property
    def tb_output(self) -> str:
        """Where `dv/tb_module` writes the test in the eval project."""
        return f"dv/{self.block}/test_{self.block}.py"

    @property
    def rtl_target(self) -> str:
        return f"digital-rtl/rtl_module[block={self.block},module={self.module}]"

    @property
    def tb_target(self) -> str:
        return f"dv/tb_module[block={self.block}]"

    @property
    def plan_path(self) -> Path:
        return PLANS / self.plan

    @property
    def reference_path(self) -> Path:
        return REFERENCE / self.reference_tb


class Exemplar(_Model):
    """A style exemplar: human RTL of the same repository, not one of the oracles."""

    fixture: str
    path: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    pull_request: int | None = None


class Rejected(_Model):
    """A block looked at and not used, and why."""

    block: str
    source: str
    reason: str = Field(min_length=1)


class SourceFile(_Model):
    """One fixture and where it comes from (for `fixtures.py --write`)."""

    fixture: str
    path: str
    commit: str
    pr: int | None


class ProblemSet(_Model):
    schema_version: Literal[1] = 1
    repo: str
    licence: str
    exemplars: tuple[Exemplar, ...] = Field(min_length=1)
    problems: tuple[Problem, ...] = Field(min_length=1)
    rejected: tuple[Rejected, ...] = ()

    @field_validator("problems")
    @classmethod
    def _unique(cls, value: tuple[Problem, ...]) -> tuple[Problem, ...]:
        ids = [p.id for p in value]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate problem ids")
        blocks = [p.block for p in value]
        if len(set(blocks)) != len(blocks):
            raise ValueError("one problem per block")
        return value

    def problem(self, pid: str) -> Problem:
        for problem in self.problems:
            if problem.id == pid:
                return problem
        raise KeyError(pid)

    def sources(self) -> list[SourceFile]:
        """Every fixture file the problem set names, with its source path and commit."""
        out = [
            SourceFile(fixture=e.fixture, path=e.path, commit=e.commit, pr=e.pull_request)
            for e in self.exemplars
        ]
        for p in self.problems:
            for path in (p.files.spec, p.files.oracle, p.files.filelist):
                out.append(
                    SourceFile(
                        fixture=p.fixture(path), path=path, commit=p.commit, pr=p.pull_request
                    )
                )
        return out

    @property
    def exemplar_paths(self) -> tuple[str, ...]:
        """Where the exemplars go in an eval project: `style/<file name>`."""
        return tuple(f"style/{Path(e.path).name}" for e in self.exemplars)


def load_problems(path: Path = PROBLEMS) -> ProblemSet:
    return ProblemSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def schema_json() -> str:
    return json.dumps(ProblemSet.model_json_schema(), indent=2, sort_keys=True) + "\n"


def module_name(text: str) -> str | None:
    """The first `module <name>` declared in SystemVerilog text (comments ignored)."""
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
    match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", code)
    return match[1] if match else None


__all__ = [
    "PLANS",
    "PROBLEMS",
    "REFERENCE",
    "Edit",
    "Exemplar",
    "Fake",
    "Port",
    "Problem",
    "ProblemSet",
    "Rejected",
    "RtlReply",
    "TbReply",
    "load_problems",
    "module_name",
    "schema_json",
]


if __name__ == "__main__":
    print(schema_json(), end="")
