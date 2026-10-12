"""Static measures of an author's work, next to the oracle's.

- RTL: Verilator `--lint-only -Wall` warnings (the module alone), the style distance to
  the exemplars (`chipgraph.checks.style.style_distance`, 0 = the exemplars' style), and
  the same two numbers for the oracle, so a reader sees how far the people's own RTL is;
- both phases: the status and issue count of each check of the rule's last run on the
  work (`format`, `lint`, `naming`, `style`, `hardcode` or `tb_static`), as `submit`
  reported them.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .fixtures import strip_header
from .problems import Problem, ProblemSet

_WARNING_RE = re.compile(r"^%Warning-(?P<code>[A-Z0-9_]+):", re.MULTILINE)


def lint_warnings(text: str, module: str, verilator: str = "verilator") -> dict[str, Any]:
    """Verilator -Wall warnings of `text` alone, by code (`error` set when it does not lint)."""
    with tempfile.TemporaryDirectory(prefix="cg-eval-lint-") as tmp:
        path = Path(tmp) / f"{module}.sv"
        path.write_text(text, encoding="utf-8")
        try:
            proc = subprocess.run(
                [verilator, "--lint-only", "-Wall", "-Wno-fatal", path.name],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"warnings": None, "by_code": {}, "error": str(exc)}
    log = proc.stdout + proc.stderr
    codes: dict[str, int] = {}
    for match in _WARNING_RE.finditer(log):
        codes[match["code"]] = codes.get(match["code"], 0) + 1
    out: dict[str, Any] = {"warnings": sum(codes.values()), "by_code": codes}
    if proc.returncode != 0:
        out["error"] = next((ln for ln in log.splitlines() if "%Error" in ln), "lint failed")
    return out


def style_distance(text: str, problems: ProblemSet) -> float:
    from chipgraph.checks.style import style_distance as distance
    from chipgraph.checks.style import style_profile

    exemplars = {
        rel: strip_header(e.fixture)
        for e, rel in zip(problems.exemplars, problems.exemplar_paths, strict=True)
    }
    return round(distance(style_profile(exemplars), text), 4)


def rtl_static(problem: Problem, text: str | None, problems: ProblemSet) -> dict[str, Any]:
    """Lint warnings and style distance of the written module and of the oracle."""
    oracle = strip_header(problem.fixture(problem.files.oracle))
    out: dict[str, Any] = {
        "oracle": {
            "lint": lint_warnings(oracle, problem.module),
            "style_distance": style_distance(oracle, problems),
        }
    }
    if text is not None:
        out["module"] = {
            "lint": lint_warnings(text, problem.module),
            "style_distance": style_distance(text, problems),
            "lines": len(text.splitlines()),
        }
    return out


def check_summary(checks: Sequence[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """`submit`'s `checks` list as {check: {status, issues}}."""
    return {
        str(c.get("check_id")): {"status": c.get("status"), "issues": len(c.get("issues") or [])}
        for c in checks or ()
    }


__all__ = ["check_summary", "lint_warnings", "rtl_static", "style_distance"]
