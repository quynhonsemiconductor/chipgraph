"""M2-01: loading skills from packs (`provides.skills`) and resolving a task's skills for
its role. The packs are test-only fixtures under `skill_fixtures/`."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.core.plugin_api.pack import Pack, discover_packs, load_pack
from chipgraph.core.runtime.roles import SkillError, SkillSpec, load_skills, parse_skill_file

FIXTURES = Path(__file__).resolve().parent / "skill_fixtures"


def _packs(*names: str) -> list[Pack]:
    return [load_pack(FIXTURES / name) for name in names]


def test_skills_load_from_a_directory_and_a_file() -> None:
    skills = load_skills(_packs("alpha", "beta"))
    assert sorted(skills.skills) == ["test-dv/cocotb", "test-org/naming", "test-sv/rtl"]
    rtl = skills.get("test-sv/rtl")
    assert rtl.roles == ("author",)
    assert rtl.version == "0.1.0"
    assert rtl.text == "Use one `always_ff` per register group.\n"
    assert "pack 'alpha'" in skills.source("test-sv/rtl")


def test_resolving_a_task_s_skills_for_its_role() -> None:
    skills = load_skills(_packs("alpha", "beta"))
    found = skills.resolve(["test-sv/rtl", "test-org/naming"], "author")
    assert [s.id for s in found] == ["test-sv/rtl", "test-org/naming"]
    assert [s.id for s in skills.resolve(["test-dv/cocotb"], "tb-author")] == ["test-dv/cocotb"]
    assert skills.resolve([], "tb-author") == ()
    assert [s.id for s in skills.resolve(["test-dv/cocotb"], "pack/tb-author")] == [
        "test-dv/cocotb"
    ]


def test_an_unknown_skill_is_rejected() -> None:
    skills = load_skills(_packs("alpha"))
    with pytest.raises(SkillError, match=r"unknown skill 'lang-sv/rtl'.*test-dv/cocotb"):
        skills.resolve(["lang-sv/rtl"], "author")
    with pytest.raises(SkillError, match="skills available: none"):
        load_skills([]).get("test-sv/rtl")


def test_a_skill_not_for_the_role_is_rejected() -> None:
    skills = load_skills(_packs("alpha"))
    with pytest.raises(SkillError, match=r"'test-sv/rtl' is not for role 'tb-author'.*author"):
        skills.resolve(["test-sv/rtl"], "tb-author")


def test_a_duplicate_skill_id_is_rejected() -> None:
    with pytest.raises(SkillError, match=r"duplicate skill id 'test-sv/rtl'.*'alpha'.*'dup'"):
        load_skills(_packs("alpha", "dup"))


def test_a_skill_for_an_unknown_role_is_rejected() -> None:
    with pytest.raises(SkillError, match="unknown roles: writer"):
        load_skills(_packs("badrole"))


def test_packs_are_discovered_like_rule_packs() -> None:
    found = discover_packs([FIXTURES])
    assert {"alpha", "beta", "dup", "badrole"} <= set(found)
    assert sorted(load_skills([found["beta"]]).skills) == ["test-org/naming"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no frontmatter\n", "frontmatter"),
        ("---\nid: bad\ndescription: d\nroles: [author]\nversion: 0.1.0\n---\nx\n", "id"),
        ("---\nid: a/b\ndescription: d\nroles: []\nversion: 0.1.0\n---\nx\n", "roles"),
        ("---\nid: a/b\ndescription: d\nroles: [author]\nversion: 1\n---\nx\n", "version"),
        ("---\nid: a/b\ndescription: d\nroles: [author]\nversion: 0.1.0\n---\n\n", "text"),
        (
            "---\nid: a/b\ndescription: d\nroles: [author]\nversion: 0.1.0\ntext: t\n---\nx\n",
            "body",
        ),
        ("---\nid: a/b\ndescription: d\nroles: [author]\nversion: 0.1.0\nx: 1\n---\nx\n", "x"),
    ],
)
def test_a_broken_skill_file_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "s.md"
    path.write_text(text)
    with pytest.raises(SkillError, match=message):
        parse_skill_file(path)


def test_skill_spec_is_frozen() -> None:
    skill = SkillSpec(id="a/b", description="d", roles=("author",), version="0.1.0", text="t")
    with pytest.raises(ValueError, match="frozen"):
        skill.text = "u"  # type: ignore[misc]
