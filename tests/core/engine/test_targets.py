"""M2-11a: the generic target mechanism (`chipgraph.core.engine.targets`).

A target is a named set of graph selectors, provided by a pack in `targets/*.yml`;
`BuildGraph.select("name:param")` resolves it. The registry knows no pack, rule or project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.graph import BuildGraph, GraphError, StaticForeach, build_graph
from chipgraph.core.engine.targets import (
    TargetError,
    TargetRegistry,
    TargetSpec,
    load_pack_targets,
    load_target_file,
)
from chipgraph.core.plugin_api.pack import load_pack

FILE = """\
schema_version: 1
target: demo
description: Two rules of one unit.
params:
  - { name: unit, description: A unit. }
select:
  - select: "p/build[unit={unit}]"
  - select: "p/ship[unit={unit}]"
    optional: true
"""


def _rule(name: str, *, needs: str | None = None) -> RuleSpec:
    return RuleSpec.model_validate(
        {
            "id": f"p/{name}",
            "kind": "gen",
            "foreach": "units",
            "inputs": [{"path": needs}] if needs else [],
            "outputs": [f"out/{{unit}}.{name}"],
            "run": {"use": "cmd", "args": {"cmd": ["true"]}},
        }
    )


def _spec(text: str = FILE, tmp: Path | None = None) -> TargetSpec:
    assert tmp is not None
    path = tmp / "demo.yml"
    path.write_text(text, encoding="utf-8")
    return load_target_file(path, pack="p")


def test_a_target_file_loads(tmp_path: Path) -> None:
    spec = _spec(tmp=tmp_path)
    assert (spec.name, spec.pack) == ("demo", "p")
    assert [p.name for p in spec.params] == ["unit"]
    assert [(s.select, s.optional) for s in spec.select] == [
        ("p/build[unit={unit}]", False),
        ("p/ship[unit={unit}]", True),
    ]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- not a mapping\n", "must contain a YAML mapping"),
        ("target: Bad Name\nselect: [{select: p/x}]\n", "invalid target file"),
        ("target: demo\nselect: []\n", "invalid target file"),
        ("target: demo\nselect: [{select: nothing}]\n", "names a rule"),
        ("target: demo\nselect: [{select: p/x}]\nextra: 1\n", "invalid target file"),
        ("target: [", "cannot read target file"),
    ],
)
def test_an_invalid_target_file_is_refused_by_name(tmp_path: Path, text: str, needle: str) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TargetError, match=needle) as caught:
        load_target_file(path)
    assert str(path) in str(caught.value)


def test_the_registry_parses_params_by_position_and_by_name(tmp_path: Path) -> None:
    registry = TargetRegistry([_spec(tmp=tmp_path)])
    assert registry.names() == ("demo",)
    assert registry.is_target("demo:a") and registry.is_target("demo")
    assert not registry.is_target("p/build[unit=a]") and not registry.is_target("other:a")
    assert [s.select for s in registry.resolve("demo:a")] == [
        "p/build[unit=a]",
        "p/ship[unit=a]",
    ]
    assert registry.resolve("demo:unit=b")[0].select == "p/build[unit=b]"
    assert registry.describe_params("demo:a") == "unit='a'"


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("nope:a", "unknown target 'nope'; known targets: demo"),
        ("demo", "needs unit: write 'demo:<unit>'"),
        ("demo:a,b", "takes 1 param"),
        ("demo:colour=red", "has no param 'colour'"),
        ("demo:unit=a,unit=b", "given twice"),
        ("demo:a b", "invalid value"),
        ("demo:a[x]", "invalid value"),
    ],
)
def test_wrong_params_are_a_clear_error(tmp_path: Path, text: str, needle: str) -> None:
    registry = TargetRegistry([_spec(tmp=tmp_path)])
    with pytest.raises(TargetError, match=needle.replace("[", r"\[")):
        registry.resolve(text)


def test_a_selector_may_only_use_the_targets_params(tmp_path: Path) -> None:
    bad = FILE.replace('{unit}]"\n    optional', '{other}]"\n    optional')
    with pytest.raises(TargetError, match=r"uses \{other\}, which is not one of its params"):
        TargetRegistry([_spec(bad, tmp_path)])


def test_two_targets_of_one_name_are_refused(tmp_path: Path) -> None:
    spec = _spec(tmp=tmp_path)
    with pytest.raises(TargetError, match="'demo' is defined twice"):
        TargetRegistry([spec, spec])


def test_a_pack_gives_its_targets(tmp_path: Path) -> None:
    pack_dir = tmp_path / "p"
    (pack_dir / "targets").mkdir(parents=True)
    (pack_dir / "pack.yml").write_text("schema_version: 1\nname: p\nversion: 0.1.0\n")
    (pack_dir / "targets" / "demo.yml").write_text(FILE)
    (pack_dir / "targets" / "other.yaml").write_text(FILE.replace("demo", "other"))
    pack = load_pack(pack_dir)
    assert [(t.name, t.pack) for t in load_pack_targets(pack)] == [("demo", "p"), ("other", "p")]
    empty = tmp_path / "q"
    empty.mkdir()
    (empty / "pack.yml").write_text("schema_version: 1\nname: q\nversion: 0.1.0\n")
    assert load_pack_targets(load_pack(empty)) == []


# --- BuildGraph.select ----------------------------------------------------------------------


def _graph(tmp_path: Path) -> BuildGraph:
    rules = [_rule("build"), _rule("ship", needs="out/{unit}.build")]
    resolver = StaticForeach({"units": [{"unit": "a"}, {"unit": "b"}]})
    registry = TargetRegistry([_spec(tmp=tmp_path)])
    return build_graph(rules, resolver, targets=registry)


def test_select_resolves_a_target_with_each_selectors_upstream(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    assert graph.select("demo:a") == {"p/build[unit=a]", "p/ship[unit=a]"}
    assert graph.select("p/ship[unit=b]") == {"p/build[unit=b]", "p/ship[unit=b]"}
    assert graph.select("*") == set(graph.instances)


def test_an_optional_selector_that_matches_nothing_is_skipped(tmp_path: Path) -> None:
    rules = [_rule("build")]  # no `ship` rule at all
    graph = build_graph(
        rules,
        StaticForeach({"units": [{"unit": "a"}]}),
        targets=TargetRegistry([_spec(tmp=tmp_path)]),
    )
    assert graph.select("demo:a") == {"p/build[unit=a]"}


def test_a_required_selector_that_matches_nothing_is_an_error_naming_the_params(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    with pytest.raises(GraphError, match=r"target 'demo:zzz'.*check its params \(unit='zzz'\)"):
        graph.select("demo:zzz")
    with pytest.raises(GraphError, match="needs unit"):
        graph.select("demo")


def test_without_a_registry_a_target_name_is_just_an_unknown_rule() -> None:
    graph = build_graph([_rule("build")], StaticForeach({"units": [{"unit": "a"}]}))
    with pytest.raises(GraphError, match="unknown"):
        graph.select("demo:a")
