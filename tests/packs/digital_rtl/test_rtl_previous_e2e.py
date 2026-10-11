"""M2-04 fix: an RTL author without a read tool fixes a rejected module by writing it anew.

The real run (`docs/rtl-claude-code/run.sh`) showed it: attempt 1 failed Verilator
`-Wall` lint (`UNUSEDSIGNAL` on `wdata[31:8]`), and the second subagent's `Write` over its
own file was refused by Claude Code ("File has not been read yet"), since `rtl-author`
has no Read. Now the engine moves the file aside at the re-dispatch and shows it in
`previous_outputs`. The scripted author here can only create files, as Claude Code
allows an agent without Read; it fixes the lint with the skill's `unused` idiom.
No model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rtl_author_helpers import (
    HAS_VERILATOR,
    OUTPUT,
    get_context,
    make_project,
    next_task,
    profile_of,
    record,
    render_pwm,
    submit,
)

from chipgraph.core.plugin_api.pack import discover_packs
from chipgraph.core.runtime.roles import load_skills

UNUSED = "  logic unused_ok;\n  assign unused_ok = &{1'b0, wdata[31:8]};\n"


def _create(root: Path, rel: str, text: str) -> None:
    """Claude Code's rule for an agent that cannot Read: a Write may only create."""
    path = root / rel
    if path.exists():
        raise PermissionError("File has not been read yet. Read it first before writing to it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _without_pragmas(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if "verilator lint_o" not in line) + "\n"


@pytest.mark.skipif(not HAS_VERILATOR, reason="needs verilator and make")
def test_attempt_two_fixes_the_lint_of_attempt_one_by_writing_a_new_file(tmp_path: Path) -> None:
    root = make_project(tmp_path / "tinysoc", exemplars="a")
    next_task(root)
    assert get_context(root)["previous_outputs"] == []
    _create(root, OUTPUT, _without_pragmas(render_pwm(profile_of("a"))))
    first = submit(root)
    assert first["status"] == "rejected", first["reasons"]
    assert "UNUSEDSIGNAL" in first["redo"] and f"{OUTPUT}:" in first["redo"]

    next_task(root)
    assert not (root / OUTPUT).exists()
    context = get_context(root)
    [entry] = context["previous_outputs"]
    assert entry["path"] == OUTPUT and "wdata" in entry["content"]
    assert "Your previous attempt is in `previous_outputs`" in context["instructions"]
    with pytest.raises(PermissionError):  # what the real run hit, had the file stayed
        _create(root, "rtl/tiny_timer.sv", "x")
    fixed = entry["content"].replace(
        "\n  logic [7:0] period_d;", "\n" + UNUSED + "  logic [7:0] period_d;", 1
    )
    assert fixed != entry["content"]
    _create(root, OUTPUT, fixed)
    second = submit(root)
    assert second["accepted"] is True, (second["reasons"], second["checks"])
    assert "unused_ok" in (root / OUTPUT).read_text()
    assert record(root).status == "accepted"


def test_the_skill_teaches_verilator_wall_cleanliness() -> None:
    from chipgraph.packs import digital_rtl

    packs = discover_packs([Path(digital_rtl.__file__).parent.parent])
    skills = load_skills([packs["digital-rtl"]])
    [skill] = skills.resolve(("lang-sv/rtl",), "rtl-author")
    text = skill.text
    assert "-Wall" in text and "any warning fails the check" in text
    assert "unused_ok = &{1'b0, wdata[31:8]}" in text and "contains `unused`" in text
    assert "lint_off" in text
    for project_name in ("tinysoc", "tiny_", "QSoC", "APB"):
        assert project_name not in text, project_name
