"""Tests for isolated workspaces in the claude-code runtime (M2-11c Step 1).

These tests verify:
- Task record gets workspace and seed_hashes fields on dispatch when isolated
- next_task creates workspaces when fanout.isolate is true
- get_context returns absolute workspace paths
- guard allows writes to workspace paths, denies writes to project root
- Workspaces are cleaned up on task endings
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from chipgraph.core.contracts import ArtifactRef, Budget, RuleInstance
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout

# Path to the guard script - go up to repo root
PLUGIN = Path(__file__).resolve().parents[4] / "plugin"
GUARD = PLUGIN / "hooks" / "guard.py"
GET_CONTEXT = "mcp__plugin_chipgraph_chipgraph__get_context"
AUTHOR = "chipgraph:author"
GUARD_PYTHON = os.environ.get("CHIPGRAPH_TEST_GUARD_PYTHON", sys.executable)


def _task(rule_id: str, output: str, inputs: tuple[str, ...] = ()) -> AgentTask:
    """Create a test AgentTask."""
    instance = RuleInstance(
        rule_id=rule_id,
        outputs=(ArtifactRef(kind="rtl", path=output),),
        inputs=tuple(ArtifactRef(kind="spec", path=p) for p in inputs),
        instance_id=RuleInstance.make_id(rule_id, {}),
    )
    return AgentTask(
        instance=instance, role="author", allowed_writes=(output,), budget=Budget(tries=2)
    )


def run_guard(
    root: Path, event: dict[str, Any] | str, *, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    """Run the guard as a subprocess and return (exit_code, output_dict, stderr)."""
    stdin = event if isinstance(event, str) else json.dumps(event)
    full_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), **(env or {})}
    proc = subprocess.run(
        [GUARD_PYTHON, str(GUARD)],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
        timeout=30,
    )
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else None), proc.stderr


def event(
    tool: str,
    *,
    agent: str | None = None,
    agent_type: str = AUTHOR,
    **tool_input: Any,
) -> dict[str, Any]:
    """Create a hook event for the guard."""
    ev: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": "/",
    }
    if agent is not None:
        ev["agent_id"] = agent
        ev["agent_type"] = agent_type
    return ev


def denied(result: tuple[int, dict[str, Any] | None, str]) -> str:
    """Extract the denial reason from a guard result."""
    code, out, _ = result
    assert code == 0, result
    assert out is not None, "expected a deny"
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    return str(spec["permissionDecisionReason"])


def no_decision(result: tuple[int, dict[str, Any] | None, str]) -> None:
    """Assert the guard made no decision (allowed)."""
    code, out, _ = result
    assert (code, out) == (0, None), result


def bind(root: Path, agent: str, task_id: str) -> None:
    """Bind an agent to a task via get_context."""
    no_decision(run_guard(root, event(GET_CONTEXT, agent=agent, task_id=task_id)))


# --- Task record workspace fields --------------------------------------------------------


class TestTaskRecordWorkspaceFields:
    """Test that task records have workspace and seed_hashes fields."""

    def test_record_has_workspace_field(self, tmp_path: Path) -> None:
        """AgentTaskRecord should have an optional workspace field."""
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        queue = TaskQueue(StateLayout(project))
        task = _task("test/rule", "rtl/out.sv")
        queue.enqueue(task, inputs_hash="0", output_hashes={})

        # Dispatch with workspace
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        record = queue.dispatch(
            task.instance.instance_id,
            run_id="r1",
            baseline={},
            workspace=str(workspace),
            seed_hashes={"input.txt": "abc123"},
        )

        assert record.workspace == str(workspace)
        assert record.seed_hashes == {"input.txt": "abc123"}

    def test_record_workspace_defaults_to_none(self, tmp_path: Path) -> None:
        """Without explicit workspace, it should be None."""
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        queue = TaskQueue(StateLayout(project))
        task = _task("test/rule", "rtl/out.sv")
        queue.enqueue(task, inputs_hash="0", output_hashes={})

        record = queue.dispatch(
            task.instance.instance_id,
            run_id="r1",
            baseline={},
        )

        assert record.workspace is None
        assert record.seed_hashes == {}


# --- Guard workspace mode ----------------------------------------------------------------


@pytest.fixture
def isolated_project(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a project with one isolated task and return (project_root, workspace, task_id)."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)

    # Create workspace directory
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Setup task with workspace
    queue = TaskQueue(StateLayout(project))
    task = _task("pulse/a", "rtl/a.sv")
    queue.enqueue(task, inputs_hash="0", output_hashes={})
    record = queue.dispatch(
        task.instance.instance_id,
        run_id="r1",
        baseline={},
        workspace=str(workspace),
        seed_hashes={},
    )

    return project, workspace, record.task_id


class TestGuardWorkspaceMode:
    """Test the guard's behavior when a task has an isolated workspace."""

    def test_bound_agent_may_write_to_workspace_output(
        self, isolated_project: tuple[Path, Path, str]
    ) -> None:
        """A bound agent should be able to write to its output inside the workspace."""
        project, workspace, task_id = isolated_project
        bind(project, "a1", task_id)

        # Write to workspace path should be allowed
        ev = event("Write", agent="a1", file_path=str(workspace / "rtl" / "a.sv"), content="x")
        no_decision(run_guard(project, ev))

    def test_write_to_project_root_is_denied_for_isolated_task(
        self, isolated_project: tuple[Path, Path, str]
    ) -> None:
        """A bound agent should NOT be able to write to the project root when isolated."""
        project, _workspace, task_id = isolated_project
        bind(project, "a1", task_id)

        # Write to project path should be denied
        ev = event("Write", agent="a1", file_path=str(project / "rtl" / "a.sv"), content="x")
        reason = denied(run_guard(project, ev))
        assert "workspace" in reason.lower() or "project" in reason.lower()

    def test_write_outside_allowed_writes_in_workspace_is_denied(
        self, isolated_project: tuple[Path, Path, str]
    ) -> None:
        """A bound agent should NOT be able to write outside allowed_writes even in workspace."""
        project, workspace, task_id = isolated_project
        bind(project, "a1", task_id)

        # Write to non-output path in workspace should be denied
        ev = event("Write", agent="a1", file_path=str(workspace / "other" / "file.sv"), content="x")
        reason = denied(run_guard(project, ev))
        assert "not an output" in reason

    def test_write_completely_outside_workspace_is_denied(
        self, isolated_project: tuple[Path, Path, str]
    ) -> None:
        """A bound agent should NOT be able to write outside the workspace entirely."""
        project, _workspace, task_id = isolated_project
        bind(project, "a1", task_id)

        # Write to path outside workspace should be denied
        outside = project.parent / "outside" / "file.sv"
        ev = event("Write", agent="a1", file_path=str(outside), content="x")
        reason = denied(run_guard(project, ev))
        assert "outside" in reason.lower()


@pytest.fixture
def non_isolated_project(tmp_path: Path) -> tuple[Path, str]:
    """Create a project with one non-isolated task and return (project_root, task_id)."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)

    # Setup task without workspace
    queue = TaskQueue(StateLayout(project))
    task = _task("pulse/b", "rtl/b.sv")
    queue.enqueue(task, inputs_hash="0", output_hashes={})
    record = queue.dispatch(
        task.instance.instance_id,
        run_id="r1",
        baseline={},
        # No workspace - non-isolated mode
    )

    return project, record.task_id


class TestGuardNonIsolatedMode:
    """Test the guard's behavior for non-isolated tasks (existing behavior)."""

    def test_bound_agent_may_write_to_project_output(
        self, non_isolated_project: tuple[Path, str]
    ) -> None:
        """A bound agent should be able to write to its output in the project root."""
        project, task_id = non_isolated_project
        bind(project, "b1", task_id)

        # Write to project path should be allowed
        ev = event("Write", agent="b1", file_path=str(project / "rtl" / "b.sv"), content="x")
        no_decision(run_guard(project, ev))

    def test_write_outside_allowed_writes_is_denied(
        self, non_isolated_project: tuple[Path, str]
    ) -> None:
        """A bound agent should NOT be able to write outside allowed_writes."""
        project, task_id = non_isolated_project
        bind(project, "b1", task_id)

        # Write to non-output path should be denied
        ev = event("Write", agent="b1", file_path=str(project / "other" / "file.sv"), content="x")
        reason = denied(run_guard(project, ev))
        assert "not an output" in reason


# --- FanoutCfg.isolate field -------------------------------------------------------------


class TestFanoutCfgIsolate:
    """Test that the FanoutCfg has the isolate field."""

    def test_fanout_cfg_isolate_defaults_to_false(self) -> None:
        """FanoutCfg.isolate should default to False."""
        from chipgraph.core.config.models import FanoutCfg

        cfg = FanoutCfg()
        assert cfg.isolate is False

    def test_fanout_cfg_isolate_can_be_true(self) -> None:
        """FanoutCfg.isolate can be set to True."""
        from chipgraph.core.config.models import FanoutCfg

        cfg = FanoutCfg(isolate=True)
        assert cfg.isolate is True


# --- Isolation module functions ----------------------------------------------------------


class TestIsolationModule:
    """Test the isolation module functions."""

    def test_should_isolate_false_by_default(self) -> None:
        """should_isolate should return False with default profile."""
        from chipgraph.adapters.runtime.claude_code.isolation import should_isolate
        from chipgraph.core.config.models import Profile

        profile = Profile(project="test")
        assert profile.fanout.isolate is False
        assert should_isolate(profile) is False

    def test_should_isolate_true_when_fanout_isolate_true(self) -> None:
        """should_isolate should return True when fanout.isolate is True."""
        from chipgraph.adapters.runtime.claude_code.isolation import should_isolate
        from chipgraph.core.config.models import FanoutCfg, Profile

        profile = Profile(project="test", fanout=FanoutCfg(isolate=True))
        assert should_isolate(profile) is True

    def test_input_paths_for_extracts_file_inputs(self) -> None:
        """input_paths_for should extract only file paths from inputs."""
        from chipgraph.adapters.runtime.claude_code.isolation import input_paths_for
        from chipgraph.core.contracts import ArtifactRef, RuleInstance

        instance = RuleInstance(
            rule_id="test/rule",
            inputs=(
                ArtifactRef(kind="spec", path="spec/block.md"),
                ArtifactRef(kind="model", model_key="block/foo"),  # No path
                ArtifactRef(kind="rtl", path="rtl/dep.sv"),
            ),
            outputs=(ArtifactRef(kind="rtl", path="rtl/out.sv"),),
            instance_id="test/rule[]",
        )

        paths = input_paths_for(instance)
        assert paths == ("spec/block.md", "rtl/dep.sv")


# --- Step 2: Isolation module functions --------------------------------------------------


def _git_worktree(tmp_path: Path) -> Path:
    """A real git worktree at HEAD of a small repo: tracked files the task never touches."""
    import subprocess

    from chipgraph.adapters.vcs.git import GitVcs

    repo = tmp_path / "repo"
    (repo / "rtl").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "readme.md").write_text("tracked\n")
    (repo / "rtl" / "old.sv").write_text("module old; endmodule\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    vcs = GitVcs()
    return vcs.create_branch_workspace(repo, vcs.head(repo), "ws", tmp_path / "wsroot")


class TestDiffWorkspace:
    """`diff_workspace` on a real worktree: the files HEAD has are not changes."""

    def test_an_untouched_worktree_has_no_changes(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import diff_workspace

        workspace = _git_worktree(tmp_path)
        assert diff_workspace(workspace, ("rtl/a.sv",), {}) == ((), ())

    def test_a_new_output_is_detected(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import diff_workspace

        workspace = _git_worktree(tmp_path)
        (workspace / "rtl" / "a.sv").write_text("module a; endmodule")
        assert diff_workspace(workspace, ("rtl/a.sv",), {}) == (("rtl/a.sv",), ())

    def test_a_modified_seeded_output_is_detected(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import _hash_or_empty, diff_workspace

        workspace = _git_worktree(tmp_path)
        out = workspace / "rtl" / "old.sv"
        seed = {"rtl/old.sv": _hash_or_empty(out)}
        assert diff_workspace(workspace, ("rtl/old.sv",), seed) == ((), ())
        out.write_text("module old; wire x; endmodule\n")
        assert diff_workspace(workspace, ("rtl/old.sv",), seed) == (("rtl/old.sv",), ())

    def test_a_new_file_outside_the_write_set_is_detected(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import diff_workspace

        workspace = _git_worktree(tmp_path)
        (workspace / "other").mkdir()
        (workspace / "other" / "file.txt").write_text("outside")
        assert diff_workspace(workspace, ("rtl/a.sv",), {}) == ((), ("other/file.txt",))

    def test_an_edit_or_a_delete_of_a_tracked_file_is_detected(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import diff_workspace

        workspace = _git_worktree(tmp_path)
        (workspace / "docs" / "readme.md").write_text("edited\n")
        (workspace / "rtl" / "old.sv").unlink()
        _changed, outside = diff_workspace(workspace, ("rtl/a.sv",), {})
        assert outside == ("docs/readme.md", "rtl/old.sv")

    def test_a_seeded_input_that_differs_from_head_is_not_a_change(self, tmp_path: Path) -> None:
        """The user's uncommitted version of an input was copied in: unchanged since seed."""
        from chipgraph.adapters.runtime.claude_code.isolation import _hash_or_empty, diff_workspace

        workspace = _git_worktree(tmp_path)
        (workspace / "docs" / "readme.md").write_text("the user's uncommitted edit\n")
        seed = {"docs/readme.md": _hash_or_empty(workspace / "docs" / "readme.md")}
        assert diff_workspace(workspace, ("rtl/a.sv",), seed) == ((), ())

    def test_the_engine_scratch_under_dot_chipgraph_is_not_a_change(self, tmp_path: Path) -> None:
        from chipgraph.adapters.runtime.claude_code.isolation import diff_workspace

        workspace = _git_worktree(tmp_path)
        (workspace / ".chipgraph" / "tmp").mkdir(parents=True)
        (workspace / ".chipgraph" / "tmp" / "x.log").write_text("scratch")
        assert diff_workspace(workspace, ("rtl/a.sv",), {}) == ((), ())


class TestCopyOutputsBack:
    """Test copy_outputs_back function."""

    def test_copy_new_output(self, tmp_path: Path) -> None:
        """A new output is copied to user's tree."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()
        (workspace / "rtl").mkdir()
        (workspace / "rtl" / "a.sv").write_text("new file")

        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {})
        assert result.ok
        assert result.copied == ("rtl/a.sv",)
        assert (user_root / "rtl" / "a.sv").read_text() == "new file"

    def test_copy_refuses_when_user_changed_file(self, tmp_path: Path) -> None:
        """Copy is refused if user edited the file since seed."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back
        from chipgraph.core.state.artifacts import hash_file

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()
        (workspace / "rtl").mkdir()
        (user_root / "rtl").mkdir()

        # Seed state: user has "original"
        user_file = user_root / "rtl" / "a.sv"
        user_file.write_text("original")
        seed_hash = hash_file(user_file)

        # User edits the file during the task
        user_file.write_text("user edit")

        # Workspace has agent's output
        (workspace / "rtl" / "a.sv").write_text("agent output")

        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {"rtl/a.sv": seed_hash})
        assert not result.ok
        assert "rtl/a.sv" in result.refused
        assert "changed since seed" in result.reason

    def test_copy_refuses_symlink(self, tmp_path: Path) -> None:
        """Copy is refused if output is a symlink."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()
        (workspace / "rtl").mkdir()

        # Create a symlink as output
        target = workspace / "target.txt"
        target.write_text("target")
        (workspace / "rtl" / "a.sv").symlink_to(target)

        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {})
        assert not result.ok
        assert "rtl/a.sv" in result.symlinks
        assert "symlink" in result.reason.lower()

    def test_copy_refuses_when_user_deleted_seeded_file(self, tmp_path: Path) -> None:
        """Copy is refused if user deleted a file that was seeded."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()
        (workspace / "rtl").mkdir()

        # Workspace has output
        (workspace / "rtl" / "a.sv").write_text("agent output")

        # User tree does NOT have the file (user deleted it)
        # Seed hash says it existed at seed time
        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {"rtl/a.sv": "some_hash"})
        assert not result.ok
        assert "rtl/a.sv" in result.refused

    def test_copy_refuses_when_user_created_new_file(self, tmp_path: Path) -> None:
        """Copy is refused if user created a file that didn't exist at seed."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()
        (workspace / "rtl").mkdir()
        (user_root / "rtl").mkdir()

        # Workspace has output
        (workspace / "rtl" / "a.sv").write_text("agent output")
        # User created a file there (wasn't in seed)
        (user_root / "rtl" / "a.sv").write_text("user created")

        # No seed hash means it didn't exist
        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {})
        assert not result.ok
        assert "rtl/a.sv" in result.refused


class TestCheckOverlappingWrites:
    """Test check_overlapping_writes function."""

    def test_no_overlap(self) -> None:
        """Disjoint write sets have no overlaps."""
        from chipgraph.adapters.runtime.claude_code.isolation import check_overlapping_writes

        write_sets = {
            "task1": ("rtl/a.sv", "rtl/b.sv"),
            "task2": ("rtl/c.sv", "rtl/d.sv"),
        }
        overlaps = check_overlapping_writes(write_sets)
        assert overlaps == {}

    def test_overlap_detected(self) -> None:
        """Overlapping write sets are detected."""
        from chipgraph.adapters.runtime.claude_code.isolation import check_overlapping_writes

        write_sets = {
            "task1": ("rtl/a.sv", "rtl/b.sv"),
            "task2": ("rtl/b.sv", "rtl/c.sv"),
        }
        overlaps = check_overlapping_writes(write_sets)
        assert "rtl/b.sv" in overlaps
        assert set(overlaps["rtl/b.sv"]) == {"task1", "task2"}

    def test_case_insensitive_overlap(self) -> None:
        """Overlaps are detected case-insensitively."""
        from chipgraph.adapters.runtime.claude_code.isolation import check_overlapping_writes

        write_sets = {
            "task1": ("rtl/A.sv",),
            "task2": ("rtl/a.sv",),
        }
        overlaps = check_overlapping_writes(write_sets)
        assert len(overlaps) == 1


class TestMaxParallelLimit:
    """Test max_parallel_limit function."""

    def test_default_limit(self) -> None:
        """Default max_parallel comes from FanoutCfg."""
        from chipgraph.adapters.runtime.claude_code.isolation import max_parallel_limit
        from chipgraph.core.config.models import Profile

        profile = Profile(project="test")
        limit = max_parallel_limit(profile)
        assert limit >= 1  # Should be min(4, cpu_count)

    def test_custom_limit(self) -> None:
        """Custom max_parallel is respected."""
        from chipgraph.adapters.runtime.claude_code.isolation import max_parallel_limit
        from chipgraph.core.config.models import FanoutCfg, Profile

        profile = Profile(project="test", fanout=FanoutCfg(max_parallel=2))
        limit = max_parallel_limit(profile)
        assert limit == 2


class TestCopyBackResultModel:
    """Test the CopyBackResult model."""

    def test_ok_result(self) -> None:
        """A successful copy-back result."""
        from chipgraph.adapters.runtime.claude_code.isolation import CopyBackResult

        result = CopyBackResult(ok=True, copied=("rtl/a.sv",))
        assert result.ok
        assert result.copied == ("rtl/a.sv",)
        assert result.refused == ()
        assert result.symlinks == ()

    def test_refused_result(self) -> None:
        """A refused copy-back result."""
        from chipgraph.adapters.runtime.claude_code.isolation import CopyBackResult

        result = CopyBackResult(
            ok=False,
            refused=("rtl/a.sv",),
            reason="user changed since seed",
        )
        assert not result.ok
        assert result.refused == ("rtl/a.sv",)
        assert "user changed" in result.reason


class TestSymlinkInPath:
    """Test symlink detection in output paths."""

    def test_symlink_in_parent_directory(self, tmp_path: Path) -> None:
        """A symlink in the parent directory is detected."""
        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace = tmp_path / "ws"
        user_root = tmp_path / "user"
        workspace.mkdir()
        user_root.mkdir()

        # Create real rtl dir with a file
        real_dir = workspace / "real_rtl"
        real_dir.mkdir()
        (real_dir / "a.sv").write_text("content")

        # Make rtl a symlink to real_rtl
        (workspace / "rtl").symlink_to(real_dir)

        result = copy_outputs_back(workspace, user_root, ("rtl/a.sv",), {})
        assert not result.ok
        assert "rtl/a.sv" in result.symlinks


# --- Step 3: Best-of-N support -----------------------------------------------------------


class TestCandidateId:
    """Test candidate_id function for best-of-N."""

    def test_candidate_id_format(self) -> None:
        """candidate_id should return task_id#r<round>-<candidate>."""
        from chipgraph.adapters.runtime.claude_code.isolation import candidate_id

        # Default round is 1
        assert candidate_id("task/a[]", 1) == "task/a[]#r1-1"
        assert candidate_id("task/a[]", 2) == "task/a[]#r1-2"
        assert candidate_id("task/b[block=x]", 3) == "task/b[block=x]#r1-3"
        # Explicit round number
        assert candidate_id("task/a[]", 1, 2) == "task/a[]#r2-1"
        assert candidate_id("task/a[]", 2, 3) == "task/a[]#r3-2"


class TestParseCandidateId:
    """Test parse_candidate_id function."""

    def test_parse_valid_candidate_id(self) -> None:
        """parse_candidate_id should extract base_task_id, candidate number, and round."""
        from chipgraph.adapters.runtime.claude_code.isolation import parse_candidate_id

        # New format: r<round>-<candidate>
        base, num, round_num = parse_candidate_id("task/a[]#r1-1")
        assert base == "task/a[]"
        assert num == 1
        assert round_num == 1

        base, num, round_num = parse_candidate_id("task/b[block=x]#r2-3")
        assert base == "task/b[block=x]"
        assert num == 3
        assert round_num == 2

        # Old format backward compatibility: just <candidate>, round assumed 1
        base, num, round_num = parse_candidate_id("task/a[]#1")
        assert base == "task/a[]"
        assert num == 1
        assert round_num == 1

    def test_parse_invalid_candidate_id(self) -> None:
        """parse_candidate_id should raise ValueError for non-candidate ids."""
        import pytest

        from chipgraph.adapters.runtime.claude_code.isolation import parse_candidate_id

        with pytest.raises(ValueError, match="not a candidate id"):
            parse_candidate_id("task/a[]")  # No #

        with pytest.raises(ValueError, match="not a candidate id"):
            parse_candidate_id("task/a[]#abc")  # Non-numeric


class TestIsCandidateId:
    """Test is_candidate_id function."""

    def test_is_candidate_id_true(self) -> None:
        """is_candidate_id should return True for candidate ids."""
        from chipgraph.adapters.runtime.claude_code.isolation import is_candidate_id

        assert is_candidate_id("task/a[]#1") is True
        assert is_candidate_id("task/b#2") is True

    def test_is_candidate_id_false(self) -> None:
        """is_candidate_id should return False for non-candidate ids."""
        from chipgraph.adapters.runtime.claude_code.isolation import is_candidate_id

        assert is_candidate_id("task/a[]") is False
        assert is_candidate_id("task/b") is False


class TestBestOfN:
    """Test best_of_n function for determining N from profile and params."""

    def test_best_of_n_from_instance_param(self) -> None:
        """best_of_n should return N from instance param."""
        from chipgraph.adapters.runtime.claude_code.isolation import best_of_n
        from chipgraph.core.config.models import Profile

        profile = Profile(project="test")
        assert best_of_n(profile, "rule/a", {"best_of": 2}) == 2
        assert best_of_n(profile, "rule/a", {"best_of": 3}) == 3
        assert best_of_n(profile, "rule/a", {"best_of": "2"}) == 2

    def test_best_of_n_from_profile_select(self) -> None:
        """best_of_n should return N from profile's select.rules."""
        from chipgraph.adapters.runtime.claude_code.isolation import best_of_n
        from chipgraph.core.config.models import Profile, SelectCfg

        profile = Profile(
            project="test",
            select=SelectCfg(rules={"rule/a": 2, "rule/b": 3}),
        )
        assert best_of_n(profile, "rule/a", {}) == 2
        assert best_of_n(profile, "rule/b", {}) == 3
        assert best_of_n(profile, "rule/c", {}) is None  # Not in rules

    def test_best_of_n_instance_param_takes_precedence(self) -> None:
        """Instance param should take precedence over profile."""
        from chipgraph.adapters.runtime.claude_code.isolation import best_of_n
        from chipgraph.core.config.models import Profile, SelectCfg

        profile = Profile(
            project="test",
            select=SelectCfg(rules={"rule/a": 2}),
        )
        # Instance param overrides profile
        assert best_of_n(profile, "rule/a", {"best_of": 3}) == 3

    def test_best_of_n_invalid_returns_none(self) -> None:
        """Invalid best_of values should return None."""
        from chipgraph.adapters.runtime.claude_code.isolation import best_of_n
        from chipgraph.core.config.models import Profile

        profile = Profile(project="test")
        assert best_of_n(profile, "rule/a", {"best_of": "abc"}) is None
        assert best_of_n(profile, "rule/a", {"best_of": 0}) is None
        assert best_of_n(profile, "rule/a", {"best_of": -1}) is None


class TestBestOfNRoundState:
    """Test BestOfNRound and CandidateState models."""

    def test_candidate_state_model(self) -> None:
        """CandidateState should hold candidate info."""
        from chipgraph.adapters.runtime.claude_code.service import CandidateState

        state = CandidateState(
            candidate=1,
            variant="approach a: prefer simple",
            task_id="task/a[]#1",
            workspace="/tmp/ws/1",
            status="dispatched",
        )
        assert state.candidate == 1
        assert state.status == "dispatched"
        assert state.passed is False
        assert state.errors == 0

    def test_best_of_n_round_model(self) -> None:
        """BestOfNRound should track the round state."""
        from chipgraph.adapters.runtime.claude_code.service import (
            BestOfNRound,
            CandidateState,
        )

        candidates = (
            CandidateState(
                candidate=1,
                variant="a",
                task_id="task[]#1",
                workspace="/tmp/1",
            ),
            CandidateState(
                candidate=2,
                variant="b",
                task_id="task[]#2",
                workspace="/tmp/2",
            ),
        )
        round_state = BestOfNRound(
            base_task_id="task[]",
            run_id="run1",
            round_num=1,
            n=2,
            candidates=candidates,
            ledger_tries=4,
        )
        assert round_state.n == 2
        assert len(round_state.candidates) == 2
        assert round_state.status == "running"
        assert round_state.winner is None


class TestBestOfNSelection:
    """Test best-of-N selection: winner has minimal key."""

    def test_winner_has_minimal_key_by_passed(self) -> None:
        """The winner should be the candidate that passed when others failed."""
        import asyncio

        from chipgraph.core.engine.select import Candidate, select_best

        async def run_test() -> None:
            candidates = [
                Candidate(id="1", passed=True, checks=(), finished=True),
                Candidate(id="2", passed=False, checks=(), finished=True),
            ]
            selection = await select_best(candidates, metrics=None, tiebreak=None, ledger=None)
            assert selection.winner == "1"
            assert selection.decided_by == "passed"

        asyncio.run(run_test())

    def test_winner_has_minimal_key_by_errors(self) -> None:
        """The winner should have fewer errors when all failed."""
        import asyncio

        from chipgraph.core.contracts import CheckResult, Issue
        from chipgraph.core.engine.select import Candidate, select_best

        async def run_test() -> None:
            # Both failed, but candidate 1 has fewer errors
            checks_1 = (
                CheckResult(
                    check_id="lint",
                    status="fail",
                    issues=(Issue(msg="error", severity="error"),),
                    duration_s=0.1,
                    idempotency_key="k1",
                ),
            )
            checks_2 = (
                CheckResult(
                    check_id="lint",
                    status="fail",
                    issues=(
                        Issue(msg="error1", severity="error"),
                        Issue(msg="error2", severity="error"),
                    ),
                    duration_s=0.1,
                    idempotency_key="k2",
                ),
            )
            candidates = [
                Candidate(id="1", passed=False, checks=checks_1, finished=True),
                Candidate(id="2", passed=False, checks=checks_2, finished=True),
            ]
            selection = await select_best(candidates, metrics=None, tiebreak=None, ledger=None)
            assert selection.winner == "1"
            assert selection.decided_by == "errors"
            assert selection.none_passed is True

        asyncio.run(run_test())

    def test_winner_by_lowest_id_when_tied(self) -> None:
        """When tied on all metrics, lowest id wins."""
        import asyncio

        from chipgraph.core.engine.select import Candidate, select_best

        async def run_test() -> None:
            # Both passed with no errors
            candidates = [
                Candidate(id="2", passed=True, checks=(), finished=True),
                Candidate(id="1", passed=True, checks=(), finished=True),
            ]
            selection = await select_best(candidates, metrics=None, tiebreak=None, ledger=None)
            assert selection.winner == "1"
            assert selection.decided_by == "lowest_id"

        asyncio.run(run_test())


class TestBestOfNTotalTriesProperty:
    """Test that total tries never exceed the ledger."""

    def test_ledger_tracks_tries(self) -> None:
        """CostLedger should track tries correctly."""
        from chipgraph.core.engine.select import CostLedger, LedgerLimits

        limits = LedgerLimits(tries=4)
        ledger = CostLedger(limits)

        assert ledger.remaining_tries == 4
        assert ledger.can_afford(1) is True

        # Reserve a try
        ledger.reserve("cand1", tries=1)
        assert ledger.remaining_tries == 3
        assert ledger.total.tries == 1

        # Reserve more
        ledger.reserve("cand2", tries=2)
        assert ledger.remaining_tries == 1
        assert ledger.total.tries == 3

        # Try to reserve more than available
        assert ledger.can_afford(2) is False
        assert ledger.can_afford(1) is True

    def test_ledger_exhausted(self) -> None:
        """CostLedger should report when exhausted."""
        from chipgraph.core.engine.select import CostLedger, LedgerLimits

        limits = LedgerLimits(tries=2)
        ledger = CostLedger(limits)

        ledger.reserve("cand1", tries=1)
        assert ledger.exhausted is None

        ledger.reserve("cand2", tries=1)
        assert ledger.exhausted == "tries"
        assert ledger.remaining_tries == 0


class TestWorkspaceDirNames:
    """Real task ids (`digital-rtl/plan[block=iso]`, `a#2`) must give names `GitVcs` accepts."""

    def test_names_pass_the_vcs_rule_and_do_not_collide(self, tmp_path: Path) -> None:
        import re
        from types import SimpleNamespace

        from chipgraph.adapters.runtime.claude_code import isolation

        ids = [
            "digital-rtl/plan[block=iso]",
            "digital-rtl/rtl_module[block=iso,module=tiny_pwm]",
            "digital-rtl/rtl_module[block=iso,module=tiny_pwm]#2",
            "Digital-RTL/Plan[block=ISO]",
            "digital-rtl_plan_block=iso_",
            "///",
        ]
        ctx = SimpleNamespace(root=tmp_path, layout=None)
        names = set()
        for task_id in ids:
            path = isolation.workspace_dir_for(ctx, "20261012T000000Z-abc", task_id)  # type: ignore[arg-type]
            assert re.match(r"^[a-z0-9][a-z0-9._-]*$", path.name), path.name
            names.add(path.name)
        assert len(names) == len(ids)


class TestGuardWorkspaceAliases:
    """macOS temp dirs are `/var` and `/private/var` at once; a link is not a way out."""

    def _bound(self, tmp_path: Path, recorded: Path) -> tuple[Path, str]:
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        queue = TaskQueue(StateLayout(project))
        task = _task("pulse/a", "rtl/a.sv")
        queue.enqueue(task, inputs_hash="0", output_hashes={})
        record = queue.dispatch(
            task.instance.instance_id,
            run_id="r1",
            baseline={},
            workspace=str(recorded),
            seed_hashes={},
        )
        bind(project, "a1", record.task_id)
        return project, record.task_id

    def test_the_recorded_path_may_be_an_alias_of_the_real_one(self, tmp_path: Path) -> None:
        real = tmp_path / "real_ws"
        real.mkdir()
        alias = tmp_path / "alias_ws"
        alias.symlink_to(real, target_is_directory=True)
        project, _ = self._bound(tmp_path, alias)
        for base in (real, alias):
            ev = event("Write", agent="a1", file_path=str(base / "rtl" / "a.sv"), content="x")
            no_decision(run_guard(project, ev))

    def test_a_link_inside_the_workspace_is_not_a_way_out(self, tmp_path: Path) -> None:
        real = tmp_path / "real_ws"
        real.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (real / "rtl").symlink_to(elsewhere, target_is_directory=True)
        project, _ = self._bound(tmp_path, real)
        ev = event("Write", agent="a1", file_path=str(real / "rtl" / "a.sv"), content="x")
        assert "outside" in denied(run_guard(project, ev)).lower()


class TestCopyBackIsAllOrNothing:
    def test_a_failure_while_staging_leaves_the_users_tree_untouched(self, tmp_path: Path) -> None:
        import os
        import stat

        from chipgraph.adapters.runtime.claude_code.isolation import copy_outputs_back

        workspace, user = tmp_path / "ws", tmp_path / "user"
        for root in (workspace, user):
            (root / "sub").mkdir(parents=True)
        (workspace / "a.sv").write_text("new a\n")
        (workspace / "sub" / "b.sv").write_text("new b\n")
        (user / "a.sv").write_text("old a\n")
        from chipgraph.core.state.artifacts import hash_file

        seed = {"a.sv": hash_file(user / "a.sv"), "sub/b.sv": ""}
        os.chmod(user / "sub", stat.S_IRUSR | stat.S_IXUSR)  # cannot create files there
        try:
            result = copy_outputs_back(workspace, user, ("a.sv", "sub/b.sv"), seed)
        finally:
            os.chmod(user / "sub", stat.S_IRWXU)
        assert not result.ok
        assert (user / "a.sv").read_text() == "old a\n"
        assert not (user / "sub" / "b.sv").exists()
        assert sorted(p.name for p in user.iterdir()) == ["a.sv", "sub"]  # no temp left
