"""Selecting the `github` review adapter: entry point, profile options, `config check`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.adapters.review.github import GitHubReview, ReviewError
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.cli import app
from chipgraph.core.config.models import AdapterCfg
from chipgraph.core.plugin_api.protocols import ReviewAdapter
from chipgraph.core.plugin_api.registry import Registry


def _project(root: Path, review: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / ".chipgraph.yml").write_text(f"project: demo\n{review}", encoding="utf-8")


def test_entry_point_registers_github_review() -> None:
    registry = Registry()
    registry.discover()
    adapter = registry.get("review", "github")
    assert isinstance(adapter, GitHubReview)
    assert isinstance(adapter, ReviewAdapter)
    assert adapter.repo is None


def test_from_config_reads_options(tmp_path: Path) -> None:
    cfg = AdapterCfg.model_validate(
        {
            "use": "github",
            "repo": "acme/chip",
            "base": "develop",
            "api_url": "https://ghe.example.test/api/v3/",
            "allow_gh_fallback": False,
            "timeout_s": 5,
            "max_retries": 1,
        }
    )
    review = GitHubReview.from_config(cfg, state_dir=tmp_path, env={})
    assert (review.repo, review.base, review.api_url) == (
        "acme/chip",
        "develop",
        "https://ghe.example.test/api/v3",
    )
    assert (review.allow_gh_fallback, review.timeout_s, review.max_retries) == (False, 5.0, 1)


@pytest.mark.parametrize(
    ("options", "needle"),
    [
        ({}, r"review\.repo is required"),
        ({"repo": "not-a-repo"}, "owner/name"),
        ({"repo": "a/b", "token": "x"}, "unknown review option"),
        ({"repo": "a/b", "allow_gh_fallback": "yes"}, "true or false"),
        ({"repo": "a/b", "timeout_s": "slow"}, "timeout_s"),
        ({"repo": "a/b", "api_url": "http://ghe.example.test"}, "https"),
    ],
)
def test_from_config_rejects_bad_options(options: dict[str, object], needle: str) -> None:
    with pytest.raises(ReviewError, match=needle):
        GitHubReview.from_config({"use": "github", **options}, env={})


def test_app_context_builds_the_pr_review_from_the_profile(tmp_path: Path) -> None:
    _project(tmp_path, "review:\n  use: github\n  repo: acme/chip\n")
    ctx = AppContext.load(tmp_path)
    assert isinstance(ctx.pr_review, GitHubReview)
    assert ctx.pr_review.repo == "acme/chip"
    # The file adapter keeps waivers, baselines and light gates.
    assert ctx.review.name == "file"
    # Bindings live in machine-local state, never in the repo tree.
    ctx.pr_review.bind("pr:timer", 7)
    assert (ctx.layout.state_dir / "review" / "github.json").is_file()


def test_app_context_without_github_review_has_no_pr_review(tmp_path: Path) -> None:
    _project(tmp_path, "")
    assert AppContext.load(tmp_path).pr_review is None


def test_missing_repo_is_an_app_error(tmp_path: Path) -> None:
    _project(tmp_path, "review:\n  use: github\n")
    with pytest.raises(AppError, match=r"review\.repo is required"):
        AppContext.load(tmp_path)


def test_config_check_shows_a_missing_repo(tmp_path: Path) -> None:
    _project(tmp_path, "review:\n  use: github\n")
    result = CliRunner().invoke(app, ["-C", str(tmp_path), "config", "check"])
    assert result.exit_code != 0
    assert "review.repo is required" in result.output
