"""M2-04: the `digital-rtl/rtl_module` rule through the runtime claude-code tools, in
process, on the acceptance fixture (`docs/rtl-claude-code/fixture.py`: tinysoc with a new
block `pwm` and its approved plan). No model: the test plays the subagent's part.

The task goes to `chipgraph:rtl-author` and denies every testbench path; its context holds
the plan entry, the requirements, the interface, the naming rules as text and the capped
exemplars, and no testbench line; an exemplar that is a testbench is refused. On submit
the engine formats the file first (the hash recorded is the formatted file's), then runs
lint, naming, style and hardcode; a module that breaks the naming rule is rejected with
the redo text and accepted once fixed; what a failure in a testbench shows the author
keeps the behaviour and drops the testbench.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rtl_author_helpers import (
    HAS_VERILATOR,
    OUTPUT,
    TASK,
    commit_all,
    fx,
    get_context,
    leaks,
    make_project,
    next_task,
    profile_of,
    record,
    render_pwm,
    submit,
    tb_lines,
    write,
)

from chipgraph.app.errors import AppError
from chipgraph.core.engine.agent_rule import TB_WITHHELD_NOTE
from chipgraph.core.state.artifacts import hash_file
from chipgraph.packs.digital_rtl.rtl.context import MAX_EXEMPLAR_CHARS


def _project(tmp_path: Path, **profile: Any) -> Path:
    return make_project(tmp_path / "tinysoc", exemplars="a", **profile)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _project(tmp_path)


def _edit_profile(root: Path, **changes: Any) -> None:
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text())
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    commit_all(root, "profile")


GOOD = render_pwm(profile_of("a"))


# --- dispatch ------------------------------------------------------------------------------


def test_the_module_goes_to_the_rtl_author(project: Path) -> None:
    answer = next_task(project)
    [task] = answer["tasks"]
    assert (task["task_id"], task["agent"], task["role"]) == (
        TASK,
        "chipgraph:rtl-author",
        "rtl-author",
    )
    assert task["skills"] == ["lang-sv/rtl", "org/naming"] and task["outputs"] == [OUTPUT]
    rec = record(project)
    assert {"dv/**", "*/test_*.py", "tb_*.sv", "*_tb.sv"} <= set(rec.denied_reads)
    assert OUTPUT not in rec.denied_reads


# --- the context -----------------------------------------------------------------------------


def test_the_context_has_the_plan_requirements_interface_naming_and_exemplars(
    project: Path,
) -> None:
    next_task(project)
    context = get_context(project)
    rtl = context["rtl"]
    module = rtl["module"]
    assert (module["name"], module["plan_top"], module["writes"]) == (
        "tiny_pwm",
        "tiny_pwm",
        [{"path": OUTPUT, "replaces": False}],
    )
    assert [r["id"] for r in rtl["requirements"]] == list(fx.REQS)
    assert all(r["text"] for r in rtl["requirements"])
    ports = rtl["interface"]["ports"]
    assert [p["name"] for p in ports] == [
        "clk", "rst_n", "addr", "wr_en", "wdata", "rdata", "pwm_out",
    ]  # fmt: skip
    assert {p["source"] for p in ports} == {"model"}
    assert (rtl["interface"]["clock"], rtl["interface"]["reset"]) == ("clk", "rst_n")
    assert rtl["dependencies"] == []
    naming = rtl["naming"]
    assert naming["rules"] == ".chipgraph/naming.yml"
    assert "^tiny_[a-z0-9_]+$" in naming["text"] and "Allowed names, per kind" in naming["text"]
    style = rtl["style"]
    assert [e["path"] for e in style["exemplars"]] == list(fx.exemplar_files("a"))
    assert all("imitate this style" in e["label"] and e["content"] for e in style["exemplars"])
    assert style["profile"]["reset"].startswith("async")
    assert set(context["skill_texts"]) == {"lang-sv/rtl", "org/naming"}
    assert "`rtl`" in context["instructions"] and "no file-reading tools" in context["instructions"]
    # model inputs: the block's spec side cut to the module's requirements, no RTL slice
    block = json.loads(
        next(i for i in context["inputs"] if i.get("model_key") == "block/pwm")["content"]
    )
    assert [r["id"] for r in block["requirements"]] == list(fx.REQS)
    assert {r["name"] for r in block["registers"]} == {"PERIOD", "DUTY", "CTRL"}
    paths = [i["path"] for i in context["inputs"] if "path" in i]
    assert paths == ["plan/pwm.approved.json", fx.SPEC]


def test_the_context_has_no_testbench_text(project: Path) -> None:
    lines = tb_lines(project)
    assert len(lines) > 10 and fx.FINGERPRINT in lines
    answer = next_task(project)
    context = get_context(project)
    assert not leaks(answer, lines) and not leaks(context, lines)
    assert "dv/" not in json.dumps({k: v for k, v in context.items() if k != "skill_texts"})


def test_exemplars_are_capped(tmp_path: Path) -> None:
    root = _project(tmp_path)
    big = "// " + "x" * 90 + "\n"
    write(root, "style/a/ex_a_counter.sv", big * (MAX_EXEMPLAR_CHARS // len(big) + 50))
    commit_all(root, "a big exemplar")
    next_task(root)
    [first, second] = get_context(root)["rtl"]["style"]["exemplars"]
    assert first["truncated"] is True and len(first["content"]) == MAX_EXEMPLAR_CHARS
    assert "truncated" not in second


def test_an_exemplar_that_is_a_testbench_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _edit_profile(root, style={"exemplars": ["dv/tb_tiny_gpio.py", "style/a/ex_a_flag.sv"]})
    next_task(root)
    [tb, kept] = get_context(root)["rtl"]["style"]["exemplars"]
    assert tb["path"] == "dv/tb_tiny_gpio.py" and "must not read" in tb["refused"]
    assert "content" not in tb and kept["content"]
    assert not leaks(get_context(root), tb_lines(root))


def test_a_testbench_input_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    rule = Path(fx.HERE).parents[1] / "src" / "chipgraph" / "packs" / "digital_rtl" / "rules"
    assert (rule / "rtl_module.yml").is_file()
    pack = root / ".chipgraph" / "packs" / "badrtl"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text("name: badrtl\nversion: 0.1.0\nprovides:\n  rules: [rules]\n")
    (pack / "rules" / "r.yml").write_text(
        yaml.safe_dump(
            {
                "rule": "r",
                "kind": "agent",
                "role": "rtl-author",
                "inputs": [{"path": "dv/test_tiny_gpio.py"}],
                "outputs": ["rtl/tiny_extra.sv"],
            }
        )
    )
    _edit_profile(root, packs=["digital-rtl", "badrtl"])
    with pytest.raises(AppError, match=r"'rtl-author' must not see 'tb'.*dv/test_tiny_gpio\.py"):
        next_task(root, "badrtl/r")


# --- submit ----------------------------------------------------------------------------------


def test_a_good_module_is_formatted_then_accepted(project: Path) -> None:
    next_task(project)
    get_context(project)
    messy = "\n".join(line + "   " for line in GOOD.splitlines()) + "\n\n\n"
    write(project, OUTPUT, messy)
    answer = submit(project)
    assert answer["accepted"] is True, (answer["reasons"], answer["checks"])
    checks = {c["check_id"]: c["status"] for c in answer["checks"]}
    assert list(checks) == ["format", "lint", "naming", "style", "hardcode"]
    assert set(checks.values()) == {"pass"}
    text = (project / OUTPUT).read_text()
    assert text == GOOD.rstrip("\n") + "\n"  # formatted in place by the engine
    rec = record(project)
    [(key, digest)] = rec.accepted_hashes.items()
    assert key.endswith(OUTPUT) and digest == hash_file(project / OUTPUT)


def test_a_module_breaking_the_naming_rule_is_rejected_then_fixed(project: Path) -> None:
    next_task(project)
    get_context(project)
    write(project, OUTPUT, render_pwm(profile_of("a"), bad_signal="BadSignal"))
    rejected = submit(project)
    assert rejected["status"] == "rejected" and rejected["label"] == "constraint"
    assert "naming" in [c["check_id"] for c in rejected["failed_checks"]]
    redo = rejected["redo"]
    assert "BadSignal" in redo and "rtl/tiny_pwm.sv" in redo and "What to fix" in redo
    again = next_task(project)
    assert [t["task_id"] for t in again["tasks"]] == [TASK]
    assert get_context(project)["previous_rejection"] == [redo]
    write(project, OUTPUT, GOOD)
    assert submit(project)["accepted"] is True


@pytest.mark.skipif(not HAS_VERILATOR, reason="needs verilator and make")
def test_a_module_that_fails_lint_is_rejected(project: Path) -> None:
    next_task(project)
    get_context(project)
    write(project, OUTPUT, render_pwm(profile_of("a"), drop_output=True))
    rejected = submit(project)
    assert rejected["status"] == "rejected" and rejected["label"] == "verification"
    assert "lint" in [c["check_id"] for c in rejected["failed_checks"]]
    assert "pwm_out" in rejected["redo"]


_FAKE_LINT = """\
import sys

print("%Error: dv/test_tiny_gpio.py:51: ZQXTB a line of the testbench")
print("   51 |     assert got == 0xA5, 'ZQXTB excerpt'")
print("      |     ^~~~~~")
print("%Error: rtl/tiny_pwm.sv:40:3: rdata expected 0x2, got 0x0 (see tb_pwm.sv)")
print("%Error: Exiting due to 2 error(s)")
sys.exit(1)
"""


def test_a_failure_in_a_testbench_keeps_the_behaviour_and_hides_the_testbench(
    project: Path,
) -> None:
    write(project, "scripts/fake_lint.py", _FAKE_LINT)
    lint = {"use": "cmd", "cmd": ["python3", "scripts/fake_lint.py"], "parser": "verilator"}
    _edit_profile(project, adapters={"lint": lint})
    next_task(project)
    get_context(project)
    write(project, OUTPUT, GOOD)
    rejected = submit(project)
    assert rejected["status"] == "rejected"
    redo = rejected["redo"]
    assert "rdata expected 0x2, got 0x0" in redo  # behavioural text stays
    assert TB_WITHHELD_NOTE in redo and "A failure in the testbench was hidden" in redo
    for hidden in ("ZQXTB", "dv/test_tiny_gpio.py", "tb_pwm.sv", "   51 |"):
        assert hidden not in redo and hidden not in json.dumps(rejected), hidden
    assert "<tb>" in redo
    journal = (project / ".chipgraph" / "state" / "runs").rglob("journal.jsonl")
    assert any("ZQXTB" in p.read_text() for p in journal)  # the engine keeps it all
