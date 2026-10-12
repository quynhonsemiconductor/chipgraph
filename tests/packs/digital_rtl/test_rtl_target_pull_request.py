"""M2-11a: the pull request of a block, `open_pr`, and the gate `pr:<block>`.

On a tinysoc copy whose `origin` is a local bare repository and whose `review:` is the
`github` adapter served by the M2-10 fake API on 127.0.0.1 (the fake's branches are the bare
repository's, so a PR can only be opened from a branch that was really pushed):

- the branch `chipgraph/<run id>/<block>` holds the block's outputs and nothing else, and is
  built without touching the user's working tree, index or HEAD, or running a hook;
- a re-run with changed outputs pushes to the same branch as a fast-forward and reuses the
  PR; with nothing changed nothing moves;
- the token is read from `CHIPGRAPH_GITHUB_TOKEN`: a missing one is a clear error (for an
  HTTPS remote), and it is in no error, body or record;
- the gate is the PR's review at its head: waiting, approved, stale after a push, rejected;
  with the `file` adapter it is `chipgraph approve`;
- nothing here merges.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from rtl_target_helpers import (
    EXPAND,
    MODULE_IDS,
    OPEN_PR,
    PR_GATE,
    PR_RECORD,
    RTL_FILES,
    TB_FILE,
    TOKEN,
    ChainRuntime,
    RemoteGitHub,
    approve,
    bare_remote,
    drive,
    git,
    github_for,
    github_profile,
    planned_timer,
    target_project,
    tree_digest,
)

from chipgraph.adapters.review.github import GitHubGateChecker, GitHubReview
from chipgraph.adapters.vcs import git as git_module
from chipgraph.adapters.vcs.git import GitVcs, VcsError
from chipgraph.app.build import gate_checker, project_graph
from chipgraph.app.context import AppContext
from chipgraph.core.runtime.roles import ReviewReport
from chipgraph.packs.digital_rtl.pr.body import MAX_CHARS, MAX_COMMENTS, render_body
from chipgraph.packs.digital_rtl.pr.open import (
    OpenPrError,
    OpenPrTool,
    PrRecord,
    needs_token,
    open_block_pr,
    read_record,
)

OUTPUTS = [*RTL_FILES, TB_FILE]


@dataclass
class Built:
    """A project built up to the PR gate, its remote and fake GitHub."""

    root: Path
    remote: Path
    fake: RemoteGitHub
    runtime: ChainRuntime

    def app(self) -> AppContext:
        return AppContext.load(self.root)

    def review(self) -> GitHubReview:
        review = self.app().pr_review
        assert review is not None
        return review

    def record(self) -> PrRecord:
        record = read_record(self.root / PR_RECORD)
        assert record is not None
        return record

    def push_elsewhere(self, tmp_path: Path) -> str:
        """Someone else pushes a commit to the PR branch (from a clone); its sha."""
        branch = self.record().branch
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        git(other, "checkout", "-q", branch)
        (other / "NOTE.txt").write_text("a reviewer's fix\n")
        git(other, "add", "NOTE.txt")
        ident = ["-c", "user.name=r", "-c", "user.email=r@example.invalid"]
        git(other, *ident, "commit", "-qm", "fix")
        git(other, "push", "-q", "origin", branch)
        return git(other, "rev-parse", "HEAD")

    def open(self, **kwargs: object) -> PrRecord:
        app = self.app()
        return open_block_pr(
            app,
            "timer",
            review=app.pr_review,
            token=kwargs.pop("token", TOKEN),
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Built]:
    monkeypatch.setenv("CHIPGRAPH_GITHUB_TOKEN", TOKEN)
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    remote = bare_remote(root, tmp_path / "remote.git")
    with github_for(remote) as fake:
        path = root / ".chipgraph.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["review"] = github_profile(fake)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        runtime = ChainRuntime(root, [planned_timer()])
        drive(root, runtime, "rtl:timer")
        approve(root)
        summary = drive(root, runtime, "rtl:timer")
        assert summary.waiting_gate == (PR_GATE,), summary
        yield Built(root, remote, fake, runtime)


# --- the branch -------------------------------------------------------------------------


def test_the_branch_holds_the_outputs_and_nothing_else(built: Built) -> None:
    record = built.record()
    head = git(built.root, "rev-parse", "HEAD")
    assert re.fullmatch(r"chipgraph/[0-9TZ]+-[0-9a-f]+/timer", record.branch)
    assert record.files == tuple(OUTPUTS) and record.gate == "pr:timer"
    # One commit on top of the user's HEAD at that time, adding or changing the outputs only.
    changed = git(built.root, "diff", "--name-status", head, f"refs/heads/{record.branch}")
    assert sorted(changed.splitlines()) == sorted(
        [
            "A\tdv/timer/test_timer.py",
            "A\trtl/tiny_timer_counter.sv",
            "A\trtl/tiny_timer_irq.sv",
            "M\trtl/tiny_timer.sv",
        ]
    )
    assert git(built.root, "rev-list", "--count", f"{head}..{record.branch}") == "1"
    for rel in OUTPUTS:  # the committed bytes are the user's files
        shown = subprocess.run(
            ["git", "show", f"{record.branch}:{rel}"],
            cwd=built.root,
            check=True,
            capture_output=True,
        ).stdout
        assert shown == (built.root / rel).read_bytes()
    # What the build wrote besides (the plan, the review, state, the profile) is not in it.
    tree = set(git(built.root, "ls-tree", "-r", "--name-only", record.branch).splitlines())
    base = set(git(built.root, "ls-tree", "-r", "--name-only", head).splitlines())
    assert tree == base | set(OUTPUTS)
    message = git(built.root, "log", "-1", "--format=%B", record.branch)
    assert "block timer" in message and "Co-Authored" not in message
    # The same branch is on the remote, at the same commit.
    assert git(built.remote, "rev-parse", f"refs/heads/{record.branch}") == record.head_sha


def test_the_users_tree_index_and_head_are_untouched_and_no_hook_runs(built: Built) -> None:
    marker = built.root / "hook-ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push", "reference-transaction"):
        hook = built.root / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o755)
    (built.root / "scratch.txt").write_text("not part of the PR\n")
    (built.root / "doc" / "specs" / "TINY_GPIO_MAS.md").write_text("an unrelated edit\n")
    git(built.root, "add", "scratch.txt")  # staged by the user: must stay staged
    before = tree_digest(built.root)
    status = git(built.root, "status", "--porcelain")
    head_ref = git(built.root, "symbolic-ref", "HEAD")

    (built.root / OUTPUTS[1]).write_text("// edited after the first PR\n")
    before = tree_digest(built.root)  # includes that edit; open_pr must not change anything
    status = git(built.root, "status", "--porcelain")
    record = built.open()

    assert tree_digest(built.root) == before
    assert git(built.root, "status", "--porcelain") == status
    assert git(built.root, "symbolic-ref", "HEAD") == head_ref
    assert not marker.exists()
    assert "scratch.txt" not in git(built.root, "ls-tree", "-r", "--name-only", record.branch)
    assert (
        git(built.root, "show", f"{record.branch}:{OUTPUTS[1]}") == "// edited after the first PR"
    )
    assert git(built.root, "worktree", "list").count("\n") == 0  # no workspace is left behind


def test_a_rerun_with_changed_outputs_is_a_fast_forward_to_the_same_branch(built: Built) -> None:
    first = built.record()
    number = first.number
    assert number == 1
    (built.root / OUTPUTS[1]).write_text("// a fix after review\n")
    second = built.open()

    assert second.branch == first.branch and second.number == number and second.url == first.url
    assert second.head_sha != first.head_sha
    assert git(built.remote, "rev-parse", f"refs/heads/{first.branch}") == second.head_sha
    # Never a force: the old head is an ancestor of the new one, on the remote too.
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", first.head_sha, second.head_sha],
            cwd=built.remote,
        ).returncode
        == 0
    )
    assert git(built.root, "rev-list", "--count", f"{first.head_sha}..{second.head_sha}") == "1"
    # One PR only, and its head follows the push.
    assert len(built.fake.prs) == 1
    assert built.fake.prs[number]["head"]["sha"] == second.head_sha
    posts = [r for r in built.fake.requests if r.method == "POST"]
    assert len(posts) == 1  # the PR was created once


def test_a_rerun_with_nothing_changed_moves_nothing(built: Built) -> None:
    first = built.record()
    again = built.open()
    assert again == first
    assert len([r for r in built.fake.requests if r.method == "POST"]) == 1


def test_a_push_by_someone_else_to_the_branch_is_not_overwritten(
    built: Built, tmp_path: Path
) -> None:
    first = built.record()
    pushed = built.push_elsewhere(tmp_path)
    (built.root / OUTPUTS[1]).write_text("// a local change\n")
    with pytest.raises(VcsError, match="failed"):
        built.open()  # not a fast-forward of the remote branch: refused, not forced
    assert git(built.remote, "rev-parse", f"refs/heads/{first.branch}") == pushed


# --- the token ---------------------------------------------------------------------------


def test_needs_token_only_for_http_remotes() -> None:
    assert needs_token("https://github.com/acme/chip.git")
    assert needs_token("http://127.0.0.1:9/x.git")
    for url in ("/tmp/remote.git", "file:///tmp/remote.git", "git@github.com:acme/chip.git"):
        assert not needs_token(url)
    assert not needs_token("ssh://git@github.com/acme/chip.git")


def test_a_missing_token_is_a_clear_error_for_an_https_remote(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(built.root, "remote", "set-url", "origin", "https://127.0.0.1:9/acme/chip-sandbox.git")
    monkeypatch.delenv("CHIPGRAPH_GITHUB_TOKEN")
    (built.root / OUTPUTS[1]).write_text("// changed\n")
    with pytest.raises(OpenPrError, match=r"no GitHub token: set CHIPGRAPH_GITHUB_TOKEN to push"):
        built.open(token=None)
    # And through the tool: a failed check with that message, nothing pushed.
    from chipgraph.core.contracts import CheckSpec
    from chipgraph.core.plugin_api.types import ToolContext

    spec = CheckSpec(id="open_pr", capability="gen", adapter="open_pr", args={})
    ctx = ToolContext(
        repo_root=built.root,
        runner=built.app().registry.get("runner", "local"),
        env={},
        params={"block": "timer"},
    )
    import asyncio

    result = asyncio.run(OpenPrTool().run(spec, ctx))
    assert result.status == "error" and "CHIPGRAPH_GITHUB_TOKEN" in result.issues[0].msg
    assert TOKEN not in result.issues[0].msg


def test_an_unreachable_https_remote_fails_without_the_token_in_the_message(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(built.root, "remote", "set-url", "origin", "https://127.0.0.1:9/acme/chip-sandbox.git")
    (built.root / OUTPUTS[1]).write_text("// changed\n")
    from chipgraph.core.contracts import CheckSpec
    from chipgraph.core.plugin_api.types import ToolContext

    spec = CheckSpec(id="open_pr", capability="gen", adapter="open_pr", args={})
    ctx = ToolContext(
        repo_root=built.root,
        runner=built.app().registry.get("runner", "local"),
        env={},
        params={"block": "timer"},
    )
    import asyncio

    result = asyncio.run(OpenPrTool().run(spec, ctx))
    assert result.status == "error" and "git push" in result.issues[0].msg
    assert TOKEN not in result.issues[0].msg and TOKEN not in repr(result)
    assert TOKEN not in (built.root / ".git" / "config").read_text()
    assert TOKEN not in (built.root / PR_RECORD).read_text()


def test_the_tool_refuses_a_block_without_an_approved_plan_or_built_files(built: Built) -> None:
    app = built.app()
    with pytest.raises(OpenPrError, match="unknown block 'nope'"):
        open_block_pr(app, "nope")
    with pytest.raises(OpenPrError, match="has no approved, valid plan: approve gate 'plan:gpio'"):
        open_block_pr(app, "gpio")
    (built.root / OUTPUTS[0]).unlink()
    with pytest.raises(OpenPrError, match=r"not built yet: rtl/tiny_timer_counter\.sv"):
        open_block_pr(app, "timer")


# --- the body ----------------------------------------------------------------------------


def test_the_body_has_the_plan_coverage_checks_and_the_verdict(built: Built) -> None:
    body = built.fake.prs[1]["body"]
    assert body.startswith("Block `timer`")
    assert "chipgraph never merges" in body
    assert "`plan/timer.plan.yml`" in body and "Top: `tiny_timer`" in body
    for name in ("tiny_timer_counter", "tiny_timer_irq", "tiny_timer"):
        assert f"`{name}`:" in body
    assert "5 of 5 requirements assigned to a module." in body
    assert "- REQ-TIM-003: tiny_timer_irq" in body
    for iid in (
        "digital-rtl/rtl_module[block=timer,module=tiny_timer_irq]",
        "dv/tb_module[block=timer]",
    ):
        assert f"`{iid}`:" in body
    assert "format pass" in body and "tb_static pass" in body
    assert "Verdict: **changes_requested**." in body
    assert "- major `rtl/tiny_timer_irq.sv:16` (REQ-TIM-003): The pending flag" in body
    assert "1 minor or nit comment(s) are in `reports/review/timer.json`." in body
    assert "`rtl/tiny_timer.sv`" in body.split("### Files")[1]
    assert len(body) < 6000 and TOKEN not in body


def test_the_body_is_bounded() -> None:
    comments = [
        {
            "id": f"R{n}",
            "severity": "blocker",
            "category": "requirement",
            "file": "rtl/a.sv",
            "line": n,
            "claim": "x" * 5000,
            "evidence": "rtl/a.sv:1 y",
        }
        for n in range(1, 30)
    ]
    from rtl_target_helpers import review_json

    report = ReviewReport.model_validate_json(review_json(comments=comments))
    body = render_body(
        block="timer",
        summary={"path": "p", "sha256": "0" * 64, "gate": {}, "modules": [], "coverage": []},
        checks={},
        review=report,
        review_path="reports/review/timer.json",
        files=["rtl/a.sv"],
    )
    assert len(body) <= MAX_CHARS
    assert f"and {29 - MAX_COMMENTS} more blocker/major comments" in body or "(cut:" in body
    assert "No check results recorded" in body and "no requirements" in body


def test_the_body_says_so_when_there_is_no_review(tmp_path: Path) -> None:
    body = render_body(
        block="timer",
        summary={"path": "p", "sha256": "0" * 64, "gate": {}, "modules": [], "coverage": []},
        checks={},
        review=None,
        review_path="reports/review/timer.json",
        files=[],
    )
    assert "No valid review report at `reports/review/timer.json`." in body


# --- the gate ------------------------------------------------------------------------------


def test_the_gate_waits_then_is_approved_at_the_head_then_goes_stale(
    built: Built, tmp_path: Path
) -> None:
    graph = project_graph(built.app())
    instance = graph.instances[PR_GATE]
    checker = gate_checker(built.app())
    assert isinstance(checker, GitHubGateChecker)
    record = built.record()
    number = record.number
    assert number is not None
    assert checker.review.pr_for("pr:timer") == number  # open_pr bound the gate

    assert checker.status("pr:timer", instance) == "waiting"  # no review yet
    assert built.review().reviews(number) == ()
    built.fake.approve(number)
    assert checker.status("pr:timer", instance) == "approved"
    # Another gate id goes to the decision files, not to GitHub: approved there.
    assert checker.status("plan:timer", graph.instances[EXPAND]) == "approved"

    # A push to the PR branch after the review: the old approval is stale.
    new_head = built.push_elsewhere(tmp_path)
    built.fake.sync()
    assert built.fake.prs[number]["head"]["sha"] == new_head != record.head_sha
    assert checker.status("pr:timer", instance) == "waiting"
    # A review that requests changes at the new head rejects the gate.
    built.fake.review(number, "other", "CHANGES_REQUESTED")
    assert checker.status("pr:timer", instance) == "rejected"


def test_a_rerun_after_review_changes_does_not_reopen_and_the_old_approval_is_stale(
    built: Built,
) -> None:
    first = built.record()
    number = first.number
    assert number is not None
    built.fake.approve(number)
    checker = gate_checker(built.app())
    instance = project_graph(built.app()).instances[PR_GATE]
    assert checker.status("pr:timer", instance) == "approved"

    (built.root / OUTPUTS[1]).write_text("// a fix after review\n")
    second = built.open()
    assert second.number == number and second.head_sha != first.head_sha
    assert len(built.fake.prs) == 1
    assert checker.status("pr:timer", instance) == "waiting"  # approved the old head only
    built.fake.approve(number)
    assert checker.status("pr:timer", instance) == "approved"


def test_the_file_adapter_keeps_the_decision_files(tmp_path: Path) -> None:
    root = target_project(tmp_path / "tinysoc", real_lint=False)
    app = AppContext.load(root)
    assert app.pr_review is None
    assert gate_checker(app) is app.gates


def test_the_scheduler_gets_the_pr_checker_only_with_the_github_adapter(built: Built) -> None:
    from chipgraph.app.build import make_scheduler

    assert isinstance(make_scheduler(built.app(), "rtl:timer").gates, GitHubGateChecker)
    assert OPEN_PR in project_graph(built.app()).instances
    assert set(MODULE_IDS) <= set(project_graph(built.app()).instances)


# --- the record -----------------------------------------------------------------------------


def test_the_record_is_the_branch_head_and_pr(built: Built) -> None:
    data = json.loads((built.root / PR_RECORD).read_text(encoding="utf-8"))
    assert set(data) == {
        "base",
        "block",
        "branch",
        "files",
        "gate",
        "head_sha",
        "number",
        "remote",
        "review",
        "schema_version",
        "url",
    }
    assert (data["number"], data["base"], data["remote"], data["review"]) == (
        1,
        "main",
        "origin",
        "github",
    )
    assert data["url"].endswith("/pull/1")
    assert built.fake.prs[1]["title"] == "chipgraph: RTL of block timer"


# --- nothing merges ---------------------------------------------------------------------------

TOUCHED = (
    "src/chipgraph/packs/digital_rtl/pr/open.py",
    "src/chipgraph/packs/digital_rtl/pr/gate.py",
    "src/chipgraph/packs/digital_rtl/pr/body.py",
    "src/chipgraph/packs/digital_rtl/rules/open_pr.yml",
    "src/chipgraph/packs/digital_rtl/rules/pr_gate.yml",
    "src/chipgraph/packs/digital_rtl/targets/rtl.yml",
    "src/chipgraph/adapters/vcs/git.py",
    "src/chipgraph/adapters/review/github.py",
    "src/chipgraph/app/build.py",
    "src/chipgraph/cli/__init__.py",
    "src/chipgraph/cli/next_steps.py",
    "tests/e2e_github/test_rtl_target_pr.py",
)
_MERGE_CALLS = (
    r"\.merge\(",
    r"def \w*merge\w*\(",
    r"/merge\b",
    r"/merges\b",
    r"auto_?merge",
    r"enablePullRequestAutoMerge",
    r"gh\s+pr\s+merge",
    r"\"merge\"",
    r"'merge'",
    r"git\s+merge\b(?!-base)",
)
_FORCE = (r"--force", r"\+refs", r"\"-f\"", r"force-with-lease", r"--mirror", r"--delete")


@pytest.mark.parametrize("pattern", _MERGE_CALLS)
def test_no_file_of_this_task_merges(pattern: str) -> None:
    root = Path(__file__).resolve().parents[3]
    for rel in TOUCHED:
        text = (root / rel).read_text(encoding="utf-8")
        # Prose says "never merges"; only code that could merge counts.
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        assert not found, f"{rel}: {pattern} -> {found}"
    assert [n for n in dir(GitHubReview) if "merge" in n.lower()] == []
    assert [n for n in dir(GitHubGateChecker) if "merge" in n.lower()] == []
    assert [n for n in dir(GitVcs) if "merge" in n.lower()] == []


@pytest.mark.parametrize("pattern", _FORCE)
def test_the_publishing_code_never_forces_a_push(pattern: str) -> None:
    root = Path(__file__).resolve().parents[3]
    sources = [
        (root / rel).read_text(encoding="utf-8")
        for rel in (
            "src/chipgraph/packs/digital_rtl/pr/open.py",
            "src/chipgraph/packs/digital_rtl/pr/gate.py",
            "src/chipgraph/packs/digital_rtl/pr/body.py",
        )
    ]
    sources += [inspect.getsource(GitVcs.push_branch), inspect.getsource(git_module._push)]
    sources += [inspect.getsource(GitVcs.advance_branch)]
    for text in sources:
        assert not re.search(pattern, text, flags=re.IGNORECASE), pattern


# --- the Opus review of #106: branch, symlinks, stale head, host, redaction ---------------


def _set_record_branch(built: Built, branch: str) -> None:
    record = built.record().model_copy(update={"branch": branch})
    (built.root / PR_RECORD).write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )


def _remote_ref(built: Built, ref: str) -> str:
    """The sha `ref` has in the remote, or "" when there is no such ref."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=built.remote,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "refs/heads/main",
        "chipgraph/run-1/gpio",  # another block's branch
        "chipgraph/a/b/timer",
        "chipgraph//timer",
        "chipgraph/r1/timer/extra",
        "-evil/r1/timer",
        "feature/x",
    ],
)
def test_a_record_naming_any_other_branch_is_refused_and_nothing_moves(
    built: Built, branch: str
) -> None:
    """The record is a file in the working tree: it must not choose which branch is moved
    and pushed (it once could: `main` was updated and pushed without a PR)."""
    local_main = git(built.root, "rev-parse", "main")
    remote_main = _remote_ref(built, "refs/heads/main")
    digest = tree_digest(built.root)
    _set_record_branch(built, branch)
    with pytest.raises(OpenPrError, match="refusing branch"):
        built.open()
    assert git(built.root, "rev-parse", "main") == local_main
    assert _remote_ref(built, "refs/heads/main") == remote_main
    assert tree_digest(built.root) == digest


def test_a_branch_that_is_checked_out_in_the_users_tree_is_refused(built: Built) -> None:
    name = "chipgraph/zz/timer"
    git(built.root, "checkout", "-q", "-b", name)
    head = git(built.root, "rev-parse", "HEAD")
    _set_record_branch(built, name)
    digest = tree_digest(built.root)
    with pytest.raises(OpenPrError, match="checked out in a worktree"):
        built.open()
    assert git(built.root, "rev-parse", name) == head
    assert tree_digest(built.root) == digest
    assert _remote_ref(built, f"refs/heads/{name}") == ""


def test_a_branch_that_is_checked_out_in_a_linked_worktree_is_refused(
    built: Built, tmp_path: Path
) -> None:
    name = "chipgraph/zz2/timer"
    other = tmp_path / "other-worktree"
    git(built.root, "worktree", "add", "-q", "-b", name, str(other))
    tip = git(built.root, "rev-parse", name)
    _set_record_branch(built, name)
    with pytest.raises(OpenPrError, match="checked out in a worktree"):
        built.open()
    assert git(built.root, "rev-parse", name) == tip
    assert git(other, "rev-parse", "HEAD") == tip
    assert name in GitVcs().checked_out_branches(built.root)


def test_the_vcs_itself_refuses_to_move_a_checked_out_branch(built: Built) -> None:
    """Defence in depth: `advance_branch` and `create_branch` check, whoever calls them."""
    vcs = GitVcs()
    current = git(built.root, "rev-parse", "--abbrev-ref", "HEAD")
    head = git(built.root, "rev-parse", "HEAD")
    with pytest.raises(VcsError, match="checked out"):
        vcs.advance_branch(built.root, current, head, head[:-1] + ("0" if head[-1] != "0" else "1"))
    with pytest.raises(VcsError, match="checked out"):
        vcs.create_branch(built.root, current, head)


def test_a_symlinked_directory_in_the_users_tree_is_refused(built: Built, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    rtl = built.root / "rtl"
    for item in rtl.iterdir():
        item.rename(outside / item.name)
    rtl.rmdir()
    rtl.symlink_to(outside, target_is_directory=True)
    before = sorted(p.name for p in outside.iterdir())
    with pytest.raises(OpenPrError, match="symlink"):
        built.open()
    assert sorted(p.name for p in outside.iterdir()) == before


def test_a_symlinked_output_file_is_refused(built: Built, tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("not part of the block\n")
    module = built.root / RTL_FILES[0]
    module.unlink()
    module.symlink_to(target)
    with pytest.raises(OpenPrError, match="symlink"):
        built.open()


def test_a_symlinked_directory_in_the_committed_branch_cannot_write_outside(
    tmp_path: Path,
) -> None:
    """The workspace is a checkout of the branch tip: if the tip commits `rtl` as a symlink,
    copying `rtl/x.sv` would write through it to wherever it points."""
    from chipgraph.packs.digital_rtl.pr.open import build_branch

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "rtl").symlink_to(outside, target_is_directory=True)
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "link")
    (root / "rtl").unlink()  # the working tree has a real directory now
    (root / "rtl").mkdir()
    (root / "rtl/x.sv").write_text("module x; endmodule\n")
    with pytest.raises(OpenPrError, match="symlink"):
        build_branch(GitVcs(), root, "chipgraph/r1/timer", ["rtl/x.sv"], "msg")
    assert list(outside.iterdir()) == []


def test_a_path_that_climbs_out_or_is_absolute_is_refused(tmp_path: Path) -> None:
    from chipgraph.packs.digital_rtl.pr.open import build_branch

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a")
    for rel in ("../a.txt", "/etc/hosts", "rtl/../../a.txt"):
        with pytest.raises(OpenPrError, match="plain relative path"):
            build_branch(GitVcs(), root, "chipgraph/r1/timer", [rel], "msg")


def test_the_gate_waits_while_the_record_is_behind_the_pull_requests_head(
    built: Built, tmp_path: Path
) -> None:
    import asyncio

    from chipgraph.core.contracts import CheckSpec
    from chipgraph.core.plugin_api.types import ToolContext
    from chipgraph.packs.digital_rtl.pr.gate import PrGateTool

    def run_gate() -> str:
        spec = CheckSpec(id="pr_gate", capability="gen", adapter="pr_gate", args={})
        ctx = ToolContext(
            repo_root=built.root,
            runner=built.app().registry.get("runner", "local"),
            env={},
            params={"block": "timer"},
        )
        return asyncio.run(PrGateTool().run(spec, ctx))

    built.open()
    ok = run_gate()
    assert ok.status == "pass"
    moved = built.push_elsewhere(tmp_path)
    stale = run_gate()
    assert stale.status == "fail"
    message = stale.issues[0].msg
    assert "branch moved" in message and moved[:12] in message
    assert not (built.root / "pr/timer.approved.json").exists() or (
        json.loads((built.root / "pr/timer.approved.json").read_text())["head_sha"] != moved
    )


@pytest.mark.parametrize(
    ("remote", "api", "ok"),
    [
        ("https://github.com/o/r.git", "https://api.github.com", True),
        ("https://x-access-token@github.com/o/r.git", "https://api.github.com", True),
        ("https://evil.example/o/r.git", "https://api.github.com", False),
        ("https://github.com.evil.example/o/r.git", "https://api.github.com", False),
        ("https://ghe.corp/o/r.git", "https://ghe.corp/api/v3", True),
        ("https://other.corp/o/r.git", "https://ghe.corp/api/v3", False),
        ("http://127.0.0.1:5000/o/r.git", "http://127.0.0.1:6000", True),
        ("ssh://git@github.com/o/r.git", "https://api.github.com", False),
    ],
)
def test_the_token_goes_only_to_the_apis_host(remote: str, api: str, ok: bool) -> None:
    from chipgraph.packs.digital_rtl.pr.open import check_token_host

    if ok:
        check_token_host(remote, api)
    else:
        with pytest.raises(OpenPrError, match="refusing to send the GitHub token"):
            check_token_host(remote, api)


def test_a_remote_on_another_host_gets_no_token_and_no_push(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(built.root, "remote", "set-url", "origin", "https://evil.example/o/r.git")
    pushes: list[object] = []
    monkeypatch.setattr(GitVcs, "push_branch", lambda *a, **k: pushes.append((a, k)))
    with pytest.raises(OpenPrError) as info:
        built.open()
    assert pushes == []
    assert TOKEN not in str(info.value)
