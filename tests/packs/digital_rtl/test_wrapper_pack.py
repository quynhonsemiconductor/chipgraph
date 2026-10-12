"""M2-12: the wrapper rules, the skill `rtl/wrapper-auto`, the `foreach: wrappers`
selectors and the author's context. No emacs, no model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wrapper_helpers import (
    AUTHOR,
    GEN,
    IP_FILE,
    OUT,
    SRC,
    WRAPPER,
    edit_profile,
    get_context,
    make_project,
    next_task,
)

from chipgraph.app.build import _resolver_for, builtin_packs_dir, load_rules
from chipgraph.app.context import AppContext
from chipgraph.core.engine.graph import build_graph
from chipgraph.core.engine.rules import load_pack_rules
from chipgraph.core.plugin_api.pack import Pack, discover_packs
from chipgraph.core.runtime.roles import SkillError, check_agent_rules, load_skills


@pytest.fixture(scope="module")
def pack() -> Pack:
    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    return discover_packs([packs_dir])["digital-rtl"]


def _graph(root: Path):  # type: ignore[no-untyped-def]
    ctx = AppContext.load(root)
    rules = load_rules(ctx)
    return build_graph(rules, _resolver_for(ctx, rules))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_project(tmp_path / "t", source=False, generated=False, runtime="claude-code")


# --- the rules ---------------------------------------------------------------------------


def test_the_author_rule(pack: Pack) -> None:
    rules = {r.id: r for r in load_pack_rules(pack)}
    rule = rules["digital-rtl/wrapper"]
    assert (rule.kind, rule.role, rule.foreach) == ("agent", "rtl-author", "wrappers")
    assert [(i.source, i.selector) for i in rule.inputs] == [
        ("model", "block/{block}"),
        ("model", "interface/{wrapper}"),
    ]
    assert rule.outputs == ("rtl/emacs/{wrapper}.src.sv",)
    assert rule.skills == ("rtl/wrapper-auto", "org/naming")
    assert rule.checks == ("wrap_check",)
    assert rule.budget.tries == 3 and rule.budget.model_fields_set == {"tries"}


def test_the_generator_rule(pack: Pack) -> None:
    rules = {r.id: r for r in load_pack_rules(pack)}
    rule = rules["digital-rtl/wrapper_gen"]
    assert (rule.kind, rule.foreach) == ("gen", "wrappers.vendor")
    assert [(i.source, i.selector) for i in rule.inputs] == [
        ("artifact", "rtl/emacs/{wrapper}.src.sv")
    ]
    assert rule.outputs == ("rtl/{wrapper}.sv",)
    assert rule.run is not None and rule.run.use == "emacs_auto"
    assert rule.run.args == {"src": "rtl/emacs/{wrapper}.src.sv", "out": "rtl/{wrapper}.sv"}
    assert rule.checks == ("wrap_check", "format", "lint", "naming")  # in this order


def test_the_rules_pass_check_agent_rules_and_expand_per_wrapper(root: Path) -> None:
    graph = _graph(root)
    check_agent_rules(graph.rules, graph.instances.values())  # does not raise
    author = graph.instances[AUTHOR]
    assert [r.path or r.model_key for r in author.inputs] == [
        "block/top",
        "interface/tiny_fifo_wrap",
    ]
    assert [r.path for r in author.outputs] == [SRC]
    gen = graph.instances[GEN]
    assert [r.path for r in gen.inputs] == [SRC, IP_FILE]  # the IP's files: a change regenerates
    assert [r.path for r in gen.outputs] == [OUT]
    assert gen.params == {"block": "top", "wrapper": WRAPPER}


def test_a_new_ip_version_makes_the_generated_wrapper_stale_not_the_source(root: Path) -> None:
    graph = _graph(root)
    assert IP_FILE not in [r.path for r in graph.instances[AUTHOR].inputs]


def test_an_exemplar_is_an_input_of_the_author_only(root: Path) -> None:
    (root / "rtl/emacs/other_wrap.src.sv").write_text("module other_wrap; endmodule\n")
    edit_profile(
        root,
        lambda p: p["blocks"]["top"]["wrappers"][0].update(exemplar="rtl/emacs/other_wrap.src.sv"),
    )
    graph = _graph(root)
    assert "rtl/emacs/other_wrap.src.sv" in [r.path for r in graph.instances[AUTHOR].inputs]
    assert "rtl/emacs/other_wrap.src.sv" not in [r.path for r in graph.instances[GEN].inputs]


def test_a_second_wrapper_is_a_second_pair_of_instances(root: Path) -> None:
    edit_profile(
        root,
        lambda p: p["blocks"]["timer"].update(
            wrappers=[
                {
                    "name": "tiny_fifo_wrap2",
                    "vendor_module": "vendor_fifo",
                    "vendor_files": ["vendor/vendor_fifo/vendor_fifo.sv"],
                }
            ]
        ),
    )
    ids = sorted(i for i in _graph(root).instances if "wrapper" in i)
    assert ids == [
        "digital-rtl/wrapper[block=timer,wrapper=tiny_fifo_wrap2]",
        "digital-rtl/wrapper[block=top,wrapper=tiny_fifo_wrap]",
        "digital-rtl/wrapper_gen[block=timer,wrapper=tiny_fifo_wrap2]",
        "digital-rtl/wrapper_gen[block=top,wrapper=tiny_fifo_wrap]",
    ]


def test_a_project_without_wrappers_has_no_wrapper_instances(root: Path) -> None:
    edit_profile(root, lambda p: p["blocks"]["top"].pop("wrappers"))
    assert [i for i in _graph(root).instances if "wrapper" in i] == []


# --- the skill -----------------------------------------------------------------------------


def test_the_skill_is_for_the_rtl_author_only(pack: Pack) -> None:
    skills = load_skills([pack])
    [skill] = skills.resolve(["rtl/wrapper-auto"], "rtl-author")
    assert skill.roles == ("rtl-author",)
    for role in ("author", "critic", "tb-author", "planner"):
        with pytest.raises(SkillError, match=f"not for role '{role}'"):
            skills.resolve(["rtl/wrapper-auto"], role)


def test_the_skill_teaches_the_auto_idioms_and_the_nevers(pack: Pack) -> None:
    text = load_skills([pack]).get("rtl/wrapper-auto").text
    for idiom in (
        "AUTO_TEMPLATE",
        "AUTOINST",
        "AUTOWIRE",
        "AUTOARG",
        "AUTOINPUT",
        "AUTOOUTPUT",
        "AUTORESET",
        "AUTO_LISP",
        "\\(.*\\)",  # the regexp capture
        "\\1",
        "unused_",  # sink an unused output
        "1'b0",  # tie off an input
    ):
        assert idiom in text, idiom
    for rule in (
        "edit, copy or re-declare the IP",
        "Never leave an input open",
        "write the expanded port list",
        "needs_human",
        "put logic in the wrapper",
    ):
        assert rule.lower() in text.lower(), rule
    for project_name in ("tinysoc", "tiny_", "QSoC", "APB", "GF180", "Verilator "):
        assert project_name not in text, project_name


# --- the author's context ----------------------------------------------------------------------


def test_the_context_has_the_vendor_declaration_and_never_its_body(root: Path) -> None:
    next_task(root)
    context = get_context(root)
    wrapper = context["rtl"]["wrapper"]
    vendor = wrapper["vendor"]
    assert vendor["module"] == "vendor_fifo"
    assert [p["name"] for p in vendor["parameters"]] == ["Width", "Depth"]
    ports = {p["name"]: p for p in vendor["ports"]}
    assert set(ports) == {
        "clk_i",
        "rst_ni",
        "clr_i",
        "testmode_i",
        "push_i",
        "data_i",
        "full_o",
        "pop_i",
        "data_o",
        "empty_o",
        "err_o",
    }
    assert ports["data_o"]["direction"] == "output" and ports["data_o"]["packed"] == "[Width-1:0]"
    text = json.dumps(context)
    ip_text = (root / IP_FILE).read_text()
    for body_line in ("mem_q[wptr_q] <= data_i", "assign err_o", "wptr_q", "$clog2", "AddrW"):
        assert body_line in ip_text and body_line not in text, body_line
    assert "stand-in for a third-party IP" not in text  # not even a comment of the IP
    assert wrapper["source"] == SRC and wrapper["generated"] == OUT
    assert wrapper["vendor_files"] == [IP_FILE]


def test_the_context_gives_the_naming_rules_and_the_generator_settings(root: Path) -> None:
    next_task(root)
    context = get_context(root)
    assert "Allowed names, per kind" in context["rtl"]["naming"]["text"]
    generator = context["rtl"]["wrapper"]["generator"]
    assert generator["pinned"]["verilog-auto-inst-param-value"] == "t"
    assert "setq" not in generator["lisp_functions"] and "downcase" in generator["lisp_functions"]
    assert context["role"] == "rtl-author"
    assert context["checks"] == ["wrap_check"]
    assert "rtl.wrapper" in context["instructions"] and "no shell" in context["instructions"]
    assert any(i["model_key"] == "interface/tiny_fifo_wrap" for i in context["inputs"])


def test_the_interface_comes_from_the_approved_plan_when_there_is_one(root: Path) -> None:
    import yaml

    plan = yaml.safe_load((Path(__file__).parent / "plan_fixtures/timer.plan.yml").read_text())
    plan["block"], plan["top"] = "top", "tiny_top"
    plan["modules"] = [
        {
            "name": WRAPPER,
            "summary": "Wraps the vendored FIFO with the project's names.",
            "reqs": [],
            "interface": {
                "ports": [
                    {"name": "i_clk", "direction": "input", "width": 1},
                    {"name": "o_data", "direction": "output", "width": 8},
                ]
            },
            "writes": [{"path": SRC}],
            "budget": {"tries": 3, "tier": "medium"},
        }
    ]
    plan["unassigned_reqs"] = []
    (root / "plan").mkdir(exist_ok=True)
    (root / "plan/top.plan.yml").write_text(yaml.safe_dump(plan))
    (root / "plan/top.approved.json").write_text(json.dumps({"plan": "plan/top.plan.yml"}))
    next_task(root)
    interface = get_context(root)["rtl"]["wrapper"]["interface"]
    assert interface["source"] == "plan"
    assert [(p["name"], p["direction"], p["width"]) for p in interface["ports"]] == [
        ("i_clk", "input", 1),
        ("o_data", "output", 8),
    ]


def test_the_model_ports_of_an_existing_wrapper_are_its_interface(root: Path) -> None:
    from chipgraph.app.ingest import run_ingest

    (root / OUT).write_text(
        "module tiny_fifo_wrap (input logic i_clk, output logic o_full);\nendmodule\n"
    )
    (root / "filelists/top.f").write_text(
        (root / "filelists/top.f").read_text() + "rtl/tiny_fifo_wrap.sv\n"
    )
    run_ingest(AppContext.load(root))
    next_task(root)
    interface = get_context(root)["rtl"]["wrapper"]["interface"]
    assert interface["source"] == "model_rtl"
    assert [(p["name"], p["direction"]) for p in interface["ports"]] == [
        ("i_clk", "input"),
        ("o_full", "output"),
    ]
    assert "keep them" in interface["note"]


def test_an_exemplar_that_is_a_testbench_file_is_refused_when_the_graph_is_built(
    root: Path,
) -> None:
    from chipgraph.app.errors import AppError

    (root / "dv").mkdir(exist_ok=True)
    (root / "dv/tb_wrap.sv").write_text("module tb_wrap; endmodule\n")
    edit_profile(root, lambda p: p["blocks"]["top"]["wrappers"][0].update(exemplar="dv/tb_wrap.sv"))
    with pytest.raises(AppError, match="must not see 'tb' artifacts"):
        next_task(root)


def test_an_exemplar_wrapper_is_labelled_and_its_text_is_an_input(root: Path) -> None:
    (root / "rtl/emacs/other_wrap.src.sv").write_text("module other_wrap; endmodule\n")
    edit_profile(
        root,
        lambda p: p["blocks"]["top"]["wrappers"][0].update(exemplar="rtl/emacs/other_wrap.src.sv"),
    )
    next_task(root)
    context = get_context(root)
    exemplar = context["rtl"]["wrapper"]["exemplar"]
    assert exemplar["path"] == "rtl/emacs/other_wrap.src.sv" and "imitate" in exemplar["label"]
    assert any("other_wrap" in json.dumps(i) for i in context["inputs"])


def test_a_wrapper_the_profile_does_not_list_is_reported_not_guessed() -> None:
    from chipgraph.core.config.models import Profile
    from chipgraph.packs.digital_rtl.wrapper.common import WrapperError, find_wrapper

    profile = Profile.model_validate({"project": "p"})
    with pytest.raises(WrapperError, match="no wrapper 'x'"):
        find_wrapper(profile, {"wrapper": "x", "block": "b"})
    with pytest.raises(WrapperError, match="no `wrapper` param"):
        find_wrapper(profile, {"block": "b"})
