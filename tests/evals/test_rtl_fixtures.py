"""M2-13: the QSoC fixtures of the rtl/tb evals are QSoC's files, unchanged, with their
provenance; the problems' interfaces, plans and oracles agree.

Without a clone the check is offline: every fixture has its header (repository, path,
commit, git blob id, licence) and its text is that blob. With a QSoC clone that has the
commits ($CHIPGRAPH_QSOC_REPO, else /tmp/qsoc-clone; pull-request heads need `git fetch
origin '+refs/pull/*/head:refs/remotes/pr/*'`) each commit must exist and the file at it
must be identical; a clone without a commit skips, naming it.
"""

from __future__ import annotations

import pytest
import yaml
from rtl_eval_helpers import qsoc_clone, rtl

fixtures = rtl("fixtures")
problems_mod = rtl("problems")
PROBLEMS = problems_mod.load_problems()


def test_every_fixture_has_a_header_and_is_its_blob() -> None:
    names = fixtures.all_fixtures()
    assert names, "no fixtures"
    for rel in names:
        header = fixtures.check_offline(rel)
        assert header.repo == "quynhonsemiconductor/vlsi_deep_training"
        assert header.licence == "Apache-2.0"
        assert len(header.commit) == 40


def test_fixtures_are_exactly_the_files_problems_yml_names() -> None:
    named = {s.fixture for s in PROBLEMS.sources()}
    assert named == set(fixtures.all_fixtures())
    for source in PROBLEMS.sources():
        header, _ = fixtures.read(source.fixture)
        assert (header.path, header.commit, header.pull_request) == (
            source.path,
            source.commit,
            source.pr,
        )


def test_an_edited_fixture_is_caught(tmp_path, monkeypatch) -> None:
    rel = PROBLEMS.problems[0].fixture(PROBLEMS.problems[0].files.oracle)
    copy = tmp_path / rel
    copy.parent.mkdir(parents=True)
    copy.write_text((fixtures.FIXTURES / rel).read_text().replace("S_IDLE", "S_IDL"))
    monkeypatch.setattr(fixtures, "FIXTURES", tmp_path)
    with pytest.raises(fixtures.FixtureError, match="edited"):
        fixtures.check_offline(rel)
    copy.write_text("module x; endmodule\n")
    with pytest.raises(fixtures.FixtureError, match="header"):
        fixtures.check_offline(rel)


@pytest.mark.parametrize("rel", fixtures.all_fixtures())
def test_fixture_matches_the_qsoc_clone(rel: str) -> None:
    repo = qsoc_clone()
    if repo is None:
        pytest.skip("no QSoC clone ($CHIPGRAPH_QSOC_REPO or /tmp/qsoc-clone)")
    header, _ = fixtures.read(rel)
    if not fixtures.has_commit(repo, header.commit):
        pytest.skip(f"{repo} does not have commit {header.commit} (fetch the PR heads)")
    fixtures.check_fixture(rel, repo)


@pytest.mark.parametrize("problem", PROBLEMS.problems, ids=lambda p: p.id)
def test_interface_is_the_oracles_declaration_and_the_plans_ports(problem) -> None:
    from chipgraph.packs.dv.declaration import declaration_in_text

    oracle = fixtures.strip_header(problem.fixture(problem.files.oracle))
    decl = declaration_in_text(oracle, problem.module)
    assert decl is not None
    declared = [(p.name, p.direction, p.width) for p in decl.ports]
    assert declared == [(p.name, p.direction, p.width) for p in problem.interface]

    plan = yaml.safe_load(problem.plan_path.read_text())
    assert plan["block"] == problem.block and plan["top"] == problem.module
    [module] = plan["modules"]
    assert module["name"] == problem.module
    assert [w["path"] for w in module["writes"]] == [problem.rtl_output]
    ports = [(p["name"], p["direction"], p["width"]) for p in module["interface"]["ports"]]
    assert ports == declared
    assert not any(p.get("internal") for p in module["interface"]["ports"])


@pytest.mark.parametrize("problem", PROBLEMS.problems, ids=lambda p: p.id)
def test_the_plan_accounts_for_every_requirement_of_the_mas(problem) -> None:
    import re

    spec = fixtures.strip_header(problem.fixture(problem.files.spec))
    ids = set(re.findall(rf"`({problem.block.upper()}_\d{{3}})`", spec))
    plan = yaml.safe_load(problem.plan_path.read_text())
    assigned = {r for m in plan["modules"] for r in m["reqs"]}
    unassigned = {u["req"] for u in plan["unassigned_reqs"]}
    assert assigned and not assigned & unassigned
    assert assigned | unassigned == ids


def test_no_oracle_is_an_exemplar_and_no_vendor_file_is_a_fixture() -> None:
    oracles = {p.files.oracle for p in PROBLEMS.problems}
    assert not oracles & {e.path for e in PROBLEMS.exemplars}
    assert not any("vendor/" in rel for rel in fixtures.all_fixtures())
