"""Authors without a read tool (`rtl-author`, `tb-author`) get a file to create and their
previous output in the context (`chipgraph.adapters.runtime.claude_code.previous`).

Claude Code refuses a `Write` over a file the agent has not read, and these roles have no
Read on purpose. So every dispatch moves the task's existing outputs into the state
backend, `get_context` shows them (`previous_outputs`), and a file that existed before
the task is put back, byte for byte with its mode, whenever the task ends without being
accepted. No model: the tests play the subagent, and `create` refuses to overwrite a file
as Claude Code does.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from previous_helpers import (
    GOOD,
    OUTPUTS,
    REPO,
    SPEC,
    TASK,
    create,
    get_context,
    make_project,
    next_task,
    previous_dir,
    record,
    rewind,
    submit,
    tree_hashes,
)

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.adapters.runtime.claude_code.agents import MCP_TOOL_PREFIX
from chipgraph.adapters.runtime.claude_code.previous import (
    MAX_FILE_CHARS,
    MAX_TOTAL_CHARS,
    PreviousOutputs,
    moves_outputs,
)
from chipgraph.app.errors import AppError
from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.runtime.roles import get_role
from chipgraph.core.state.layout import StateLayout

ORIGINAL = b"// the user's own file, before the task\r\nmodule keep; endmodule\n\x00\xff GOOD?\n"
"""Bytes that a text round trip would damage (CRLF, NUL, a non-UTF-8 byte)."""
MODE = 0o751

NO_READ = pytest.mark.parametrize("role", ["rtl-author", "tb-author"])


def _project(tmp_path: Path, role: str = "rtl-author", **kwargs: Any) -> tuple[Path, str]:
    out = OUTPUTS[role]
    existing = kwargs.pop("existing", {out: (ORIGINAL, MODE)})
    root = make_project(tmp_path / "p", role=role, existing=existing, **kwargs)
    return root, out


def _assert_original(root: Path, out: str) -> None:
    path = root / out
    assert path.read_bytes() == ORIGINAL
    assert path.stat().st_mode & 0o7777 == MODE


def _cycle(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((previous_dir(root) / "cycle.json").read_text())
    return data


# --- which roles -------------------------------------------------------------------------------


def test_only_writers_without_a_read_tool_move_their_outputs() -> None:
    assert moves_outputs(get_role("rtl-author")) and moves_outputs(get_role("tb-author"))
    for role in ("author", "planner", "researcher", "critic"):
        assert not moves_outputs(get_role(role)), role
    assert not moves_outputs(None)


# --- dispatch ----------------------------------------------------------------------------------


@NO_READ
def test_dispatch_moves_an_existing_output_aside(tmp_path: Path, role: str) -> None:
    root, out = _project(tmp_path, role)
    answer = next_task(root)
    assert [t["task_id"] for t in answer["tasks"]] == [TASK]
    assert not (root / out).exists()  # the agent's Write creates a new file
    kept = previous_dir(root) / "d1" / out
    assert kept.read_bytes() == ORIGINAL
    moved = json.loads((previous_dir(root) / "d1.json").read_text())
    [entry] = moved["files"]
    assert (entry["path"], entry["origin"], entry["mode"], entry["size"]) == (
        out,
        "before_task",
        MODE,
        len(ORIGINAL),
    )
    assert entry["sha256"] == hashlib.sha256(ORIGINAL).hexdigest()
    assert _cycle(root)["since"] == 1 and _cycle(root)["ended"] is None
    assert out not in TaskQueue(StateLayout(root)).baseline(TASK)


def test_an_author_task_keeps_its_file_in_place(tmp_path: Path) -> None:
    root, out = _project(tmp_path, "author")
    next_task(root)
    _assert_original(root, out)
    assert not previous_dir(root).exists()
    assert "previous_outputs" not in get_context(root)


@NO_READ
def test_the_first_attempt_is_moved_on_redispatch_and_shown(tmp_path: Path, role: str) -> None:
    root, out = _project(tmp_path, role, existing={})
    next_task(root)
    context = get_context(root)
    assert context["previous_outputs"] == []
    assert "write it whole with Write" in context["instructions"]
    create(root, out, "attempt one\n")
    rejected = submit(root)
    assert rejected["status"] == "rejected"
    assert (root / out).read_text() == "attempt one\n"  # left as it is until the next try
    next_task(root)
    assert not (root / out).exists()
    context = get_context(root)
    [entry] = context["previous_outputs"]
    assert (entry["path"], entry["content"], entry["truncated"]) == (out, "attempt one\n", False)
    assert "existing_output" not in entry
    assert (
        "Your previous attempt is in `previous_outputs`; write the whole corrected file with Write"
        in context["instructions"]
    )
    assert f"{out}:1" in context["previous_rejection"][0]  # the redo text names file:line
    moved = json.loads((previous_dir(root) / "d2.json").read_text())
    assert [f["origin"] for f in moved["files"]] == ["attempt"]
    create(root, out, f"attempt two {GOOD}\n")
    assert submit(root)["accepted"] is True


@NO_READ
def test_a_preexisting_file_is_shown_as_existing_output(tmp_path: Path, role: str) -> None:
    root, out = _project(tmp_path, role)
    next_task(root)
    [entry] = get_context(root)["previous_outputs"]
    assert entry["path"] == out and "content" not in entry
    existing = entry["existing_output"]
    assert existing["content"] == ORIGINAL.decode("utf-8", errors="replace")
    assert existing["truncated"] is False
    create(root, out, "attempt one\n")
    submit(root)
    next_task(root)
    [entry] = get_context(root)["previous_outputs"]
    assert entry["content"] == "attempt one\n" and "existing_output" in entry


# --- restore on every ending without acceptance ------------------------------------------------


def test_restored_after_budget_exhausted(tmp_path: Path) -> None:
    root, out = _project(tmp_path, tries=2)
    for text in ("one\n", "two\n"):
        next_task(root)
        create(root, out, text)
        answer = submit(root)
    assert answer["status"] == "budget_exhausted"
    _assert_original(root, out)
    assert _cycle(root)["ended"] == "restored"
    # the last attempt the restore replaced is kept for a person to read
    assert (previous_dir(root) / "e2" / out).read_text() == "two\n"
    events = (root / ".chipgraph" / "state" / "runs").rglob("journal.jsonl")
    assert any(f'"restored":["{out}"]' in p.read_text().replace(" ", "") for p in events)


def test_restored_after_needs_human(tmp_path: Path) -> None:
    root, out = _project(tmp_path)
    next_task(root)
    create(root, out, "half done\n")
    answer = submit(root, status="needs_human", open_questions=["which reset?"])
    assert answer["status"] == "needs_human"
    _assert_original(root, out)


def test_restored_when_get_context_refuses(tmp_path: Path) -> None:
    root, out = _project(tmp_path, inputs=("dv/NOTES.md",))  # a testbench path
    next_task(root)
    assert not (root / out).exists()
    with pytest.raises(AppError, match="must not see"):
        get_context(root)
    assert record(root).status == "needs_human"
    _assert_original(root, out)


def test_restored_after_a_rewind(tmp_path: Path) -> None:
    root, out = _project(tmp_path)
    next_task(root)
    create(root, out, "attempt one\n")
    assert submit(root)["status"] == "rejected"
    rewind(root)
    _assert_original(root, out)
    # the next dispatch starts a new cycle: the restored file is `before_task` again
    next_task(root)
    assert _cycle(root)["since"] == 2
    assert [f["origin"] for f in _cycle(root)["originals"]] == ["before_task"]


def test_a_rewind_leaves_a_dispatched_task_alone(tmp_path: Path) -> None:
    root, out = _project(tmp_path)
    next_task(root)
    rewind(root)
    assert not (root / out).exists() and record(root).status == "dispatched"
    create(root, out, f"done {GOOD}\n")
    assert submit(root)["accepted"] is True


def test_restored_when_the_engine_fails_mid_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, out = _project(tmp_path)

    def boom(self: TaskQueue, task_id: str, **kwargs: Any) -> AgentTaskRecord:
        raise RuntimeError("disk full")

    monkeypatch.setattr(TaskQueue, "dispatch", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        next_task(root)
    _assert_original(root, out)
    assert _cycle(root)["ended"] == "undone"
    assert record(root).status == "ready"
    monkeypatch.undo()
    next_task(root)  # works again, from a clean cycle
    assert not (root / out).exists() and _cycle(root)["ended"] is None


def test_restored_when_the_tries_run_out_with_the_output_missing(tmp_path: Path) -> None:
    root, out = _project(tmp_path, tries=1)
    next_task(root)
    answer = submit(root)  # the agent wrote nothing
    assert answer["status"] == "budget_exhausted" and answer["missing"] == [out]
    _assert_original(root, out)
    assert not (previous_dir(root) / "e1").exists()  # nothing was there to keep


def test_an_accepted_output_replaces_the_preexisting_file(tmp_path: Path) -> None:
    root, out = _project(tmp_path)
    next_task(root)
    create(root, out, f"new {GOOD}\n")
    assert submit(root)["accepted"] is True
    assert (root / out).read_text() == f"new {GOOD}\n"
    assert _cycle(root)["ended"] == "accepted"
    assert (previous_dir(root) / "d1" / out).read_bytes() == ORIGINAL  # still readable
    rewind(root)  # nothing to restore after an acceptance
    assert (root / out).read_text() == f"new {GOOD}\n"


def test_modes_are_preserved(tmp_path: Path) -> None:
    root, out = _project(tmp_path, existing={OUTPUTS["rtl-author"]: (b"x\n", 0o600)})
    next_task(root)
    submit(root, status="needs_human", open_questions=["?"])
    assert (root / out).stat().st_mode & 0o7777 == 0o600


# --- the context -------------------------------------------------------------------------------


def test_large_files_are_truncated(tmp_path: Path) -> None:
    outs = ("rtl/tiny_a.sv", "rtl/tiny_b.sv", "rtl/tiny_c.sv", "rtl/tiny_d.sv")
    big = {rel: (b"x" * (MAX_FILE_CHARS + 500), 0o644) for rel in outs[:3]}
    big[outs[3]] = (b"small\n", 0o644)
    root = make_project(tmp_path / "p", outputs=outs, existing=big)
    next_task(root)
    shown = get_context(root)["previous_outputs"]
    texts = [e["existing_output"] for e in shown]
    assert [len(t["content"]) for t in texts[:3]] == [MAX_FILE_CHARS] * 3
    assert all(t["truncated"] for t in texts[:3])
    assert sum(len(t["content"]) for t in texts) <= MAX_TOTAL_CHARS
    assert texts[3] == {"label": texts[3]["label"], "content": "", "truncated": True}


def test_a_moved_file_of_a_denied_kind_is_not_shown(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p", outputs=("rtl/tiny_prev.sv",))
    queue = TaskQueue(StateLayout(root))
    (root / "dv").mkdir(exist_ok=True)
    (root / "dv" / "tb_prev.sv").write_text("ZQXTB testbench text\n")
    (root / "rtl" / "tiny_prev.sv").write_text("module tiny_prev; endmodule\n")
    mover = PreviousOutputs(queue, TASK)
    mover.move_aside(("rtl/tiny_prev.sv", "dv/tb_prev.sv"), dispatch=1)
    next_task(root)  # the real task, so get_context's role check applies below
    rec = record(root)
    role = get_role("rtl-author")
    shown, withheld = mover.context(
        ("rtl/tiny_prev.sv", "dv/tb_prev.sv"), service._denies(role, rec)
    )
    assert withheld == ["dv/tb_prev.sv"]
    assert "ZQXTB" not in json.dumps(shown)
    [_, hidden] = shown
    assert hidden["path"] == "dv/tb_prev.sv" and "content" not in hidden
    assert "must not see" in hidden["withheld"]
    assert "must not see" in service._previous_instructions(shown, withheld)


def test_get_context_excerpt_for_the_report(tmp_path: Path) -> None:
    """The shape `get_context` gives an author on its second try (quoted in the PR)."""
    root, out = _project(tmp_path, existing={})
    next_task(root)
    create(root, out, "module tiny_prev;\nendmodule\n")
    submit(root)
    next_task(root)
    shown = get_context(root)["previous_outputs"]
    assert shown == [
        {
            "path": out,
            "label": "your previous attempt (moved aside; write the file anew)",
            "content": "module tiny_prev;\nendmodule\n",
            "truncated": False,
        }
    ]


# --- what is never touched ---------------------------------------------------------------------


def test_nothing_outside_the_outputs_is_touched(tmp_path: Path) -> None:
    root, out = _project(tmp_path, tries=2)
    before = tree_hashes(root, skip=(out,))
    next_task(root)
    create(root, out, "one\n")
    submit(root)
    assert tree_hashes(root, skip=(out,)) == before
    next_task(root)
    create(root, out, "two\n")
    submit(root)  # budget_exhausted: restored
    assert tree_hashes(root) == {**before, out: tree_hashes(root)[out]}
    _assert_original(root, out)


def test_an_output_path_outside_the_project_is_never_touched(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p")
    outside = tmp_path / "outside.sv"
    outside.write_text("keep\n")
    mover = PreviousOutputs(TaskQueue(StateLayout(root)), TASK)
    token = mover.move_aside(("../outside.sv", str(outside), "rtl/../../outside.sv"), dispatch=1)
    assert token.moved == () and outside.read_text() == "keep\n"


def test_a_symlinked_output_is_left_alone(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p")
    target = root / "rtl" / "tiny_timer.sv"
    link = root / "rtl" / "tiny_prev.sv"
    link.symlink_to(target)
    mover = PreviousOutputs(TaskQueue(StateLayout(root)), TASK)
    assert mover.move_aside(("rtl/tiny_prev.sv",), dispatch=1).moved == ()
    assert link.is_symlink() and target.is_file()


# --- the guard and old records -----------------------------------------------------------------

GUARD = REPO / "plugin" / "hooks" / "guard.py"


def _guard(root: Path, tool: str, agent: str, **tool_input: Any) -> str | None:
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": tool_input,
                "agent_id": "s1",
                "agent_type": agent,
                "cwd": str(root),
            }
        ),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return str(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]) if out else None


@NO_READ
def test_the_guard_still_denies_read_for_these_roles(tmp_path: Path, role: str) -> None:
    root, out = _project(tmp_path, role)
    root = root.resolve()
    next_task(root)
    agent = f"chipgraph:{role}"
    assert _guard(root, f"{MCP_TOOL_PREFIX}get_context", agent, task_id=TASK) is None
    kept = previous_dir(root) / "d1" / out
    for path in (root / out, kept):
        reason = _guard(root, "Read", agent, file_path=str(path))
        assert reason and "no file-reading tools" in reason, path
    assert _guard(root, "Write", agent, file_path=str(root / out), content="x") is None


def test_an_old_task_record_loads_and_runs(tmp_path: Path) -> None:
    """A record written before this change (no `budget_state`, its file never moved)."""
    root, out = _project(tmp_path)
    queue = TaskQueue(StateLayout(root))
    next_task(root)
    rec = record(root)
    # put the tree and the state back as the old code left them: the file in place,
    # no `previous/`, a record without the newer fields
    (root / out).write_bytes(ORIGINAL)
    os.chmod(root / out, MODE)
    for path in sorted(previous_dir(root).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    previous_dir(root).rmdir()
    old = rec.model_dump(mode="json", exclude={"budget_state", "max_dispatches"})
    queue._task_path(TASK).write_text(json.dumps(old))
    assert AgentTaskRecord.model_validate_json(queue._task_path(TASK).read_text()).status == (
        "dispatched"
    )
    assert get_context(root)["previous_outputs"] == []
    answer = submit(root, status="needs_human", open_questions=["?"])
    assert answer["status"] == "needs_human"
    _assert_original(root, out)  # nothing was moved, nothing restored


# --- end to end --------------------------------------------------------------------------------


@NO_READ
def test_end_to_end_a_rejected_attempt_is_fixed_by_writing_a_new_file(
    tmp_path: Path, role: str
) -> None:
    """Attempt 1 fails its check; attempt 2 reads `previous_outputs`, fixes the file and,
    since an author without Read can only create files, writes it anew: accepted."""
    root, out = _project(tmp_path, role)

    def author() -> None:
        context = get_context(root)
        [entry] = context["previous_outputs"]
        if "content" in entry:  # fix the previous attempt
            create(root, out, entry["content"].replace("BROKEN", GOOD))
        else:  # rewrite the pre-existing file
            create(root, out, entry["existing_output"]["content"].splitlines()[0] + " BROKEN\n")

    next_task(root)
    author()
    first = submit(root)
    assert first["status"] == "rejected" and first["failed_checks"]
    next_task(root)
    author()
    second = submit(root)
    assert second["accepted"] is True, second["reasons"]
    assert GOOD in (root / out).read_text()
    assert _cycle(root)["ended"] == "accepted"


# --- property: the user's file is never lost ---------------------------------------------------


def _dispatch_fails(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def boom(self: TaskQueue, task_id: str, **kwargs: Any) -> AgentTaskRecord:
        raise RuntimeError("engine failed")

    with monkeypatch.context() as patch:
        patch.setattr(TaskQueue, "dispatch", boom)
        with pytest.raises(RuntimeError):
            next_task(root)


@pytest.mark.parametrize("seed", range(10))
def test_property_the_preexisting_file_is_never_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    rng = random.Random(seed)
    root, out = _project(tmp_path, tries=rng.choice((1, 2, 3)))
    others = tree_hashes(root, skip=(out, SPEC))
    users = ORIGINAL  # the user's version: the original, or what was accepted since
    mode = MODE
    for step in range(16):
        rec = TaskQueue(StateLayout(root)).get(TASK)
        status = rec.status if rec is not None else "ready"
        action = rng.choice(("dispatch", "dispatch", "work", "work", "rewind", "fail", "spec"))
        if action == "dispatch":
            next_task(root)
        elif action == "fail" and status in ("ready", "rejected"):
            _dispatch_fails(monkeypatch, root)
        elif action == "rewind":
            rewind(root)
        elif action == "spec" and status not in ("dispatched", "submitted"):
            # a person edits the spec (an input) between attempts, never during one
            (root / SPEC).write_text(f"# prev\n\nstep {step}: write GOOD.\n")
        elif action == "work" and status == "dispatched":
            assert not (root / out).exists()  # always a file to create
            outcome = rng.choice(("good", "bad", "nothing", "human"))
            if outcome in ("good", "bad"):
                create(root, out, f"{GOOD if outcome == 'good' else 'no'} {step}\n")
            if outcome == "human":
                submit(root, status="needs_human", open_questions=["?"])
            else:
                submit(root)
        rec = TaskQueue(StateLayout(root)).get(TASK)
        status = rec.status if rec is not None else "ready"
        path = root / out
        # nothing else is ever touched
        assert tree_hashes(root, skip=(out, SPEC)) == others
        if status == "accepted":
            users, mode = path.read_bytes(), path.stat().st_mode & 0o7777
            assert hashlib.sha256(users).hexdigest() in rec.accepted_hashes.values()
        elif status in ("budget_exhausted", "needs_human") or (
            status in ("ready",) and not (previous_dir(root) / "cycle.json").is_file()
        ):
            assert path.read_bytes() == users and path.stat().st_mode & 0o7777 == mode
        else:  # mid-cycle: the user's version is in the tree or kept in the state backend
            kept = {p.read_bytes() for p in previous_dir(root).rglob("*") if p.is_file()} | (
                {path.read_bytes()} if path.is_file() else set()
            )
            assert users in kept, (seed, step, status)
        cycle = PreviousOutputs(TaskQueue(StateLayout(root)), TASK).cycle()
        if cycle is not None and not cycle.open and status not in ("dispatched", "submitted"):
            assert path.read_bytes() == users, (seed, step, status, cycle.ended)
