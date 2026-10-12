"""Security review of M2-11a/M2-12: a target file is read strictly and a selector is a rule
selector only, so hostile or malformed target files fail at load with a short error, and
`BuildGraph.select` cannot recurse through a selector that looks like a target."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from chipgraph.core.contracts import RuleSpec
from chipgraph.core.engine.graph import GraphError, StaticForeach, build_graph
from chipgraph.core.engine.targets import (
    MAX_FILE_BYTES,
    TargetError,
    TargetParam,
    TargetRegistry,
    TargetSelector,
    TargetSpec,
    load_pack_targets,
    load_target_file,
)
from chipgraph.core.plugin_api.pack import load_pack

PACKS = Path(__file__).resolve().parents[3] / "src" / "chipgraph" / "packs"


def _load(tmp_path: Path, text: str | bytes) -> TargetSpec:
    path = tmp_path / "t.yml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return load_target_file(path, pack="p")


def _file(select: str, *, extra: str = "") -> str:
    return (
        "schema_version: 1\ntarget: rtl\nparams:\n  - { name: block }\n"
        f"select:\n  - select: {select!r}\n{extra}"
    )


def _rule(name: str) -> RuleSpec:
    return RuleSpec.model_validate(
        {
            "id": f"p/{name}",
            "kind": "gen",
            "foreach": "units",
            "outputs": [f"out/{{block}}.{name}"],
            "run": {"use": "cmd", "args": {"cmd": ["true"]}},
        }
    )


def _unchecked(name: str, select: str) -> TargetSpec:
    """A target with one param, `block`, whose selector skipped validation (as if a loader
    let it through)."""
    selector = TargetSelector.model_construct(select=select, optional=False)
    return TargetSpec.model_construct(
        schema_version=1,
        name=name,
        description="",
        params=(TargetParam(name="block"),),
        select=(selector,),
        pack="p",
    )


# --- finding 10: a selector is never a target ---------------------------------------------


@pytest.mark.parametrize("select", ["rtl:{block}/x", "b:x/y", "rtl:a/x[block={block}]"])
def test_a_selector_with_a_colon_in_its_rule_part_is_refused_at_load(
    tmp_path: Path, select: str
) -> None:
    with pytest.raises(TargetError, match="names a rule"):
        _load(tmp_path, _file(select))


def test_select_never_recurses_through_a_selector_that_looks_like_a_target() -> None:
    resolver = StaticForeach({"units": [{"block": "a"}]})
    registry = TargetRegistry([_unchecked("rtl", "rtl:{block}/x"), _unchecked("a", "b:x/y")])
    registry.add(_unchecked("b", "a:{block}/y"))
    graph = build_graph([_rule("x")], resolver, targets=registry)
    for target in ("rtl:a", "a:a", "b:a"):
        with pytest.raises(GraphError, match="unknown rule id"):
            graph.select(target)


# --- finding 14: unreadable, deep or exploding files ---------------------------------------


def test_a_non_utf8_target_file_is_a_target_error(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match="not UTF-8"):
        _load(tmp_path, _file("p/x").encode() + b"# \xff\n")


def test_deep_nesting_is_a_short_target_error(tmp_path: Path) -> None:
    text = "target: rtl\nselect: " + "[" * 5000 + "x" + "]" * 5000 + "\n"
    with pytest.raises(TargetError, match="nested deeper") as caught:
        _load(tmp_path, text)
    assert len(str(caught.value)) < 500


def test_a_billion_laughs_alias_tree_is_refused_fast(tmp_path: Path) -> None:
    lines = ['a0: &a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for level in range(1, 8):
        refs = ",".join([f"*a{level - 1}"] * 10)
        lines.append(f"a{level}: &a{level} [{refs}]")
    text = "\n".join(lines) + "\ntarget: rtl\nselect: *a7\n"
    start = time.monotonic()
    with pytest.raises(TargetError, match="aliases") as caught:
        _load(tmp_path, text)
    assert time.monotonic() - start < 2.0
    assert len(str(caught.value)) < 500


def test_aliases_under_the_cap_give_a_short_message_without_the_values(tmp_path: Path) -> None:
    big = "x" * 10_000
    text = f"d: &d {big}\ntarget: rtl\ndescription: [*d, *d, *d]\nselect: [{{select: p/x}}]\n"
    with pytest.raises(TargetError, match="invalid target file") as caught:
        _load(tmp_path, text)
    assert big[:300] not in str(caught.value) and "input_value" not in str(caught.value)
    assert len(str(caught.value)) < 1000


def test_a_target_file_over_the_size_cap_is_refused(tmp_path: Path) -> None:
    text = _file("p/x") + "#" + "x" * MAX_FILE_BYTES + "\n"
    with pytest.raises(TargetError, match="larger than"):
        _load(tmp_path, text)


# --- finding 18: strict loading -------------------------------------------------------------


def test_a_duplicate_yaml_key_is_refused(tmp_path: Path) -> None:
    text = "target: rtl\ntarget: other\nselect: [{select: p/x}]\n"
    with pytest.raises(TargetError, match="given twice"):
        _load(tmp_path, text)
    nested = "target: rtl\nselect: [{select: p/x, select: p/y}]\n"
    with pytest.raises(TargetError, match="given twice"):
        _load(tmp_path, nested)


def test_duplicate_param_names_are_refused(tmp_path: Path) -> None:
    text = "target: rtl\nparams: [{name: block}, {name: block}]\nselect: [{select: p/x}]\n"
    with pytest.raises(TargetError, match="declared twice"):
        _load(tmp_path, text)


@pytest.mark.parametrize("value", ["'yes'", "'true'", "1", "'1'"])
def test_optional_must_be_a_real_boolean(tmp_path: Path, value: str) -> None:
    with pytest.raises(TargetError, match="invalid target file"):
        _load(tmp_path, _file("p/x", extra=f"    optional: {value}\n"))


def test_a_real_boolean_optional_loads(tmp_path: Path) -> None:
    spec = _load(tmp_path, _file("p/x", extra="    optional: true\n"))
    assert spec.select[0].optional is True


@pytest.mark.parametrize(
    "select",
    [
        "p/r[",
        "/",
        "p/",
        "/r",
        "p/r[k]",
        "p/r[]",
        "p/r[k=v]x",
        "p/r[k=v][j=w]",
        "p/r[k=v,k=w]",
        "p/r[=v]",
        "p/r[k=]",
        "p/r[k={block]",
        "p/r/s",
        "P/r",
        "p r/x",
    ],
)
def test_a_malformed_selector_is_refused_at_load_even_when_optional(
    tmp_path: Path, select: str
) -> None:
    with pytest.raises(TargetError, match="invalid target file"):
        _load(tmp_path, _file(select, extra="    optional: true\n"))


@pytest.mark.parametrize(
    "select", ["p/r", "p/r[block={block}]", "digital-rtl/plan_expand[block={block},k=v]"]
)
def test_well_formed_selectors_load(tmp_path: Path, select: str) -> None:
    assert _load(tmp_path, _file(select)).select[0].select == select


def test_the_built_in_targets_still_load() -> None:
    names = [t.name for t in load_pack_targets(load_pack(PACKS / "digital_rtl"))]
    assert {"rtl", "plan"} <= set(names)
