"""The release workflow: a release candidate goes to TestPyPI, is installed and run from
there, and only then goes to PyPI (the marketplace plugin's `uvx chipgraph@<rc>` resolves
from PyPI). Every PyPI upload goes through the reviewed `pypi` environment.
"""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return jobs


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_rc_goes_to_testpypi_then_smoke_test_then_pypi() -> None:
    jobs = _jobs()
    assert "contains(github.ref_name, 'rc')" in jobs["publish-testpypi"]["if"]
    assert _needs(jobs["smoke-testpypi"]) == ["build", "publish-testpypi"]
    smoke = "\n".join(step.get("run", "") for step in jobs["smoke-testpypi"]["steps"])
    assert "test.pypi.org/simple" in smoke and "unsafe-best-match" in smoke
    assert '"chipgraph@$VERSION" --version' in smoke

    pypi = jobs["publish-pypi"]
    assert set(_needs(pypi)) == {"build", "smoke-testpypi"}
    cond = " ".join(pypi["if"].split())
    # an rc tag reaches PyPI only after the smoke test passed
    assert "contains(github.ref_name, 'rc') && needs.smoke-testpypi.result == 'success'" in cond
    # a final tag and a manual `pypi` run still publish (smoke test skipped there)
    assert "!contains(github.ref_name, 'rc')" in cond
    assert "inputs.target == 'pypi'" in cond
    assert cond.startswith("!cancelled() && needs.build.result == 'success'")


def test_pypi_uploads_need_the_reviewed_environment_and_publish_the_built_files() -> None:
    jobs = _jobs()
    assert jobs["publish-pypi"]["environment"] == "pypi"
    assert jobs["publish-testpypi"]["environment"] == "testpypi"
    for name in ("publish-pypi", "publish-testpypi"):
        steps = jobs[name]["steps"]
        assert any(s.get("uses", "").startswith("actions/download-artifact@") for s in steps)
        assert jobs[name]["permissions"] == {"id-token": "write"}
    # the smoke test only installs from the public index: no token at all, not even
    # the workflow-level `contents: read`
    assert jobs["smoke-testpypi"]["permissions"] == {}
    assert jobs["build"]["outputs"]["version"] == "${{ steps.version.outputs.version }}"
