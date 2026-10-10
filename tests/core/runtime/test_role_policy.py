"""M2-01: a role's tool table applied to rules and tasks: the rule validator
(`check_agent_rules`) and what a task's agent must not read (`denied_reads`)."""

from __future__ import annotations

import pytest

from chipgraph.core.contracts import ArtifactRef, RuleSpec
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.runtime.roles import (
    RoleError,
    check_agent_rules,
    covers_denied,
    denied_reads,
    get_role,
    path_denied,
)


def _agent(rule_id: str, role: str, inputs: list[dict[str, str]], outputs: list[str]) -> RuleSpec:
    return RuleSpec.model_validate(
        {"id": rule_id, "kind": "agent", "role": role, "inputs": inputs, "outputs": outputs}
    )


def _check(*rules: RuleSpec) -> None:
    graph = build_graph(rules, StaticForeach({}))
    check_agent_rules(graph.rules, graph.instances.values())


def test_known_roles_pass() -> None:
    _check(
        _agent("p/rtl", "author", [{"spec": "doc/a.md"}], ["rtl/a.sv"]),
        _agent("p/tb", "tb-author", [{"spec": "doc/a.md"}, {"model": "block/a"}], ["dv/t.py"]),
        _agent("p/plan", "planner", [{"spec": "doc/a.md"}], ["plan/a.md"]),
        _agent("p/research", "researcher", [], ["doc/proposal.md"]),
    )


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(RoleError, match=r"rule 'p/x': unknown role 'writer'"):
        _check(_agent("p/x", "writer", [], ["a.txt"]))


def test_tb_author_with_an_rtl_input_is_rejected() -> None:
    rule = _agent("p/tb", "tb-author", [{"spec": "doc/a.md"}, {"path": "rtl/a.sv"}], ["dv/t.py"])
    with pytest.raises(RoleError, match=r"'tb-author' must not see 'rtl'.*'rtl/a\.sv'"):
        _check(rule)


def test_tb_author_rtl_input_through_foreach_is_rejected_once() -> None:
    rule = RuleSpec.model_validate(
        {
            "id": "p/tb",
            "kind": "agent",
            "role": "tb-author",
            "foreach": "blocks",
            "inputs": [{"artifact": "rtl/{block}.sv"}],
            "outputs": ["dv/{block}.py"],
        }
    )
    graph = build_graph([rule], StaticForeach({"blocks": [{"block": "a"}, {"block": "b"}]}))
    with pytest.raises(RoleError) as info:
        check_agent_rules(graph.rules, graph.instances.values())
    assert "rtl/a.sv" in str(info.value) and "rtl/b.sv" in str(info.value)


def test_an_author_may_read_rtl() -> None:
    _check(_agent("p/fix", "author", [{"path": "rtl/a.sv"}], ["rtl/b.sv"]))


def test_triage_and_critic_cannot_run_agent_rules() -> None:
    with pytest.raises(RoleError, match="'triage' is not handed out"):
        _check(_agent("p/t", "triage", [], ["a.txt"]))
    with pytest.raises(RoleError, match="'critic' writes no files"):
        _check(_agent("p/c", "critic", [], ["a.txt"]))


# --- M2-09: a critic rule with the one output the engine writes from its reply ----------


def test_a_critic_rule_with_one_engine_written_report_passes() -> None:
    _check(_agent("p/review", "critic", [{"model": "block/a"}], ["reports/review/a.json"]))


def test_the_review_rule_of_the_digital_rtl_pack_passes() -> None:
    from chipgraph.app.build import builtin_packs_dir
    from chipgraph.core.engine.rules import load_pack_rules
    from chipgraph.core.plugin_api.pack import discover_packs

    packs_dir = builtin_packs_dir()
    assert packs_dir is not None
    rules = load_pack_rules(discover_packs([packs_dir])["digital-rtl"])
    blocks = StaticForeach({"blocks": [{"block": "timer"}, {"block": "gpio"}]})
    graph = build_graph(rules, blocks)
    check_agent_rules(graph.rules, graph.instances.values())
    assert sorted(i for i in graph.instances if i.startswith("digital-rtl/review")) == [
        "digital-rtl/review[block=gpio]",
        "digital-rtl/review[block=timer]",
    ]


def test_a_critic_rule_that_wants_an_agent_written_output_is_rejected() -> None:
    with pytest.raises(RoleError, match=r"'critic' writes no files.*'rtl/a\.sv' is a 'rtl'"):
        _check(_agent("p/c", "critic", [], ["rtl/a.sv"]))
    with pytest.raises(RoleError, match=r"'critic' writes no files.*'doc/review\.md' is a 'doc'"):
        _check(_agent("p/c", "critic", [], ["doc/review.md"]))


def test_a_critic_rule_with_two_outputs_is_rejected() -> None:
    with pytest.raises(RoleError, match=r"exactly one output \(it has 2\)"):
        _check(_agent("p/c", "critic", [], ["reports/a.json", "reports/b.json"]))


def test_a_role_that_writes_nothing_without_engine_writes_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chipgraph.core.runtime.roles import policy

    plain = get_role("critic").model_copy(update={"engine_writes": ()})
    monkeypatch.setattr(policy, "find_role", lambda _: plain)
    with pytest.raises(RoleError, match="writes no files, so it cannot produce"):
        _check(_agent("p/c", "critic", [], ["reports/a.json"]))


def test_plan_and_proposal_roles_write_one_file() -> None:
    with pytest.raises(RoleError, match=r"only its plan file.*has 2"):
        _check(_agent("p/plan", "planner", [], ["plan/a.md", "plan/b.md"]))
    with pytest.raises(RoleError, match="only its proposal file"):
        _check(_agent("p/r", "researcher", [], ["a.md", "b.md"]))


def test_every_problem_is_listed() -> None:
    with pytest.raises(RoleError) as info:
        _check(_agent("p/a", "nobody", [], ["a"]), _agent("p/b", "ghost", [], ["b"]))
    assert "'nobody'" in str(info.value) and "'ghost'" in str(info.value)


def test_non_agent_rules_are_ignored() -> None:
    human = RuleSpec.model_validate({"id": "p/h", "kind": "human", "outputs": ["rtl/a.sv"]})
    _check(human)


# --- denied_reads -----------------------------------------------------------------------


def _ref(path: str) -> ArtifactRef:
    from chipgraph.core.engine.graph import kind_for

    return ArtifactRef(kind=kind_for(path), path=path)


def test_author_has_no_denied_reads() -> None:
    refs = [_ref("rtl/a.sv")]
    assert denied_reads(get_role("author"), artifacts=refs, layout={"rtl": "rtl/*.sv"}) == ()


def test_tb_author_denies_rtl_artifacts_and_their_directories() -> None:
    refs = [
        _ref("rtl/tiny_gpio.sv"),
        _ref("ip/timer/rtl/timer.v"),
        _ref("top.sv"),  # at the root: the root itself is never denied
        _ref("doc/specs/A.md"),
        ArtifactRef(kind="model", model_key="block/gpio"),
    ]
    denied = denied_reads(get_role("tb-author"), artifacts=refs, layout={})
    assert denied == (
        "ip/timer/rtl/**",
        "ip/timer/rtl/timer.v",
        "rtl/**",
        "rtl/tiny_gpio.sv",
        "top.sv",
    )


def test_tb_author_denies_the_profile_rtl_layout_per_block() -> None:
    denied = denied_reads(
        get_role("tb-author"),
        artifacts=[],
        layout={"rtl": ("hw/{block}/rtl/*.sv", "hw/{BLOCK}_{rev}.v"), "spec": "doc/{block}.md"},
        block_layouts={"gpio": {}, "top": {"rtl": "top/top.sv"}, "dummy": {"rtl": ()}},
    )
    assert denied == ("hw/GPIO_*.v", "hw/gpio/rtl/*.sv", "top/top.sv")


def test_layout_without_blocks_uses_wildcards() -> None:
    denied = denied_reads(get_role("tb-author"), artifacts=[], layout={"rtl": "src/{block}/*.sv"})
    assert denied == ("src/*/*.sv",)


def test_the_task_outputs_and_escaping_paths_are_never_listed() -> None:
    denied = denied_reads(
        get_role("tb-author"),
        artifacts=[_ref("dv/tb_top.sv")],  # a SystemVerilog testbench: kind rtl
        layout={"rtl": ("/abs/*.sv", "../up/*.sv")},
        keep=["dv/tb_top.sv"],
    )
    assert denied == ("dv/**",)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("rtl/tiny_gpio.sv", True),
        ("rtl/sub/x.sv", True),
        ("rtl", False),
        ("doc/specs/A.md", False),
        ("hw/gpio/rtl/a.sv", True),
        ("hw/gpio/rtl/deep/a.sv", True),  # `*` crosses `/`: the safe side
        ("hw/gpio/a.sv", False),
    ],
)
def test_path_denied(path: str, expected: bool) -> None:
    assert path_denied(path, ["rtl/**", "rtl/tiny_gpio.sv", "hw/gpio/rtl/*.sv"]) is expected


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("", True),  # the project root holds everything
        ("rtl", True),
        ("rtl/sub", False),  # does not hold rtl/tiny_gpio.sv
        ("hw", True),
        ("hw/gpio", True),
        ("hw/gpio/rtl", True),
        ("doc", False),
        ("hw/timer", False),
    ],
)
def test_covers_denied(scope: str, expected: bool) -> None:
    assert covers_denied(scope, ["rtl/tiny_gpio.sv", "hw/gpio/rtl/*.sv"]) is expected
