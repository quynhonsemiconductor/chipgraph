"""M2-11a end to end, with the fake runtime, on a tinysoc copy: target `rtl:timer`.

plan (a first, overlapping plan that `plan_check` rejects, then a valid one) -> the gate
`plan:timer` waits, and nothing after the plan has run -> a person approves -> the RTL
author writes each module in dependency order (Verilator's `lint` for real when it is on
PATH) -> the testbench author -> the Critic's review (fake JSON) -> `open_pr` -> the gate
`pr:timer` waits -> a person approves it at the head -> done.

Driven by the commands a person types (`chipgraph rtl timer`, `chipgraph approve ...`).
Twice: with the `file` review adapter (offline: the branch is made, nothing is pushed,
`chipgraph approve pr:timer`), and with the `github` adapter against a fake API served on
127.0.0.1 and a bare repository as `origin` (the branch is pushed, the PR is opened, a
review on the PR approves the gate). No test here calls the network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from plan_helpers import FIXTURES, fixture_plan, overlapping
from rtl_target_helpers import (
    EXPAND,
    HAS_VERILATOR,
    MODULE_IDS,
    MODULES,
    OPEN_PR,
    PLAN,
    PR_GATE,
    PR_RECORD,
    REVIEW,
    RTL_FILES,
    TB,
    TB_FILE,
    TOKEN,
    ChainRuntime,
    bare_remote,
    git,
    github_for,
    github_profile,
    module_text,
    register,
    target_project,
)
from typer.testing import CliRunner

import chipgraph.cli as cli
from chipgraph.app.build import project_graph
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import RuleInstance

runner = CliRunner()
GOOD = (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8")
BAD = yaml.safe_dump(overlapping(fixture_plan("timer")), sort_keys=False)
AFTER_PLAN = {"digital-rtl/rtl_module", "dv/tb_module", "digital-rtl/review"}


def _cli(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(cli.app, ["-C", str(root), *args])
    return result.exit_code, result.output


def _use(monkeypatch: pytest.MonkeyPatch, runtime: ChainRuntime) -> None:
    """Make every command of this test load its project with the scripted runtime."""
    real = cli._load_ctx
    monkeypatch.setattr(cli, "_load_ctx", lambda state: register(real(state), runtime))


def _lint_modules(root: Path) -> None:
    """Real Verilator, on each module the author wrote, on its own."""
    if shutil.which("verilator") is None:
        return
    for name, rel in zip(MODULES, RTL_FILES, strict=True):
        done = subprocess.run(
            ["verilator", "--lint-only", "-Wall", "--top-module", name, str(root / rel)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, f"{rel}: {done.stderr}"


def _plan_and_module_phase(root: Path, runtime: ChainRuntime) -> None:
    """Steps 1 to 3: the plan, its gate, nothing after it, the approval, the modules."""
    # 1. The planner's first plan is rejected by plan_check, the second passes; the gate waits.
    code, out = _cli(root, "rtl", "timer")
    assert code == 1, out
    assert runtime.calls == ["digital-rtl/plan"] * 2
    assert "plan.write_overlap" in runtime.tasks[1].context["feedback"]
    assert f"gate plan:timer: chipgraph approve plan:timer --instance '{EXPAND}'" in out
    assert "chipgraph plan show timer" in out
    assert not set(runtime.calls) & AFTER_PLAN  # nothing after the plan ran while it waits
    assert not (root / TB_FILE).exists() and not any((root / r).exists() for r in RTL_FILES[:2])

    # 2. The plan is read and approved (`--instance` is optional: one instance has the gate).
    code, out = _cli(root, "plan", "show", "timer")
    assert code == 0, out
    code, out = _cli(root, "approve", "plan:timer")
    assert code == 0, out
    assert "recorded for gate 'plan:timer'" in out

    # 3. The target goes on: the modules in dependency order, then the testbench, the review.
    code, out = _cli(root, "rtl", "timer")
    assert code == 1, out  # the gate pr:timer waits
    assert len(runtime.of("digital-rtl/plan")) == 2  # the approved plan was not planned again
    modules = [t.instance.params["module"] for t in runtime.of("digital-rtl/rtl_module")]
    assert modules[-1] == "tiny_timer" and set(modules) == set(MODULES)
    assert runtime.calls.index("digital-rtl/review") > runtime.calls.index("dv/tb_module")
    assert runtime.calls.index("digital-rtl/review") > max(
        i for i, c in enumerate(runtime.calls) if c == "digital-rtl/rtl_module"
    )
    for name, rel in zip(MODULES, RTL_FILES, strict=True):
        assert (root / rel).read_text(encoding="utf-8") == module_text(name)
    assert (root / TB_FILE).read_text(encoding="utf-8").startswith("import cocotb")
    _lint_modules(root)


def _lint_statuses(root: Path) -> list[str]:
    """The status of every `lint` check of the three modules, over all runs."""
    from chipgraph.core.state.journal import read

    ctx = AppContext.load(root)
    statuses: list[str] = []
    for run in sorted(p.name for p in ctx.layout.runs_dir.iterdir()):
        for event in read(ctx.layout.journal(run)).events:
            if (
                event.type == "check_result"
                and event.payload.get("check_id") == "lint"
                and "rtl_module" in str(event.rule_instance)
            ):
                statuses.append(str(event.payload["status"]))
    return statuses


def test_the_chain_offline_with_the_file_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = target_project(tmp_path / "tinysoc")
    runtime = ChainRuntime(root, [BAD, GOOD])
    _use(monkeypatch, runtime)
    _plan_and_module_phase(root, runtime)

    # 4. The review ran, then open_pr made the branch (nothing pushed), and pr:timer waits.
    record = json.loads((root / PR_RECORD).read_text(encoding="utf-8"))
    assert record["review"] == "file" and record["gate"] == "pr:timer"
    assert record["number"] is None and record["url"] is None
    assert record["files"] == [*RTL_FILES, TB_FILE]
    assert git(root, "rev-parse", f"refs/heads/{record['branch']}") == record["head_sha"]
    assert record["branch"].startswith("chipgraph/") and record["branch"].endswith("/timer")
    code, out = _cli(root, "rtl", "timer")
    assert code == 1
    assert f"gate pr:timer: chipgraph approve pr:timer --instance '{PR_GATE}'" in out
    assert not (root / "pr" / "timer.approved.json").exists()

    # 5. A person approves the PR gate; the target is done.
    code, out = _cli(root, "approve", "pr:timer")
    assert code == 0, out
    code, out = _cli(root, "rtl", "timer")
    assert code == 0, out
    run_id = out.splitlines()[0].removeprefix("run ").strip()
    assert "done: branch " in out and "merging is yours" in out
    approved = json.loads((root / "pr" / "timer.approved.json").read_text(encoding="utf-8"))
    assert approved["head_sha"] == record["head_sha"] and approved["gate"] == "pr:timer"
    assert {*MODULE_IDS, TB, REVIEW, OPEN_PR, PR_GATE} <= set(_all_done(root))

    # 6. status shows the chain, and where it waited.
    code, out = _cli(root, "status", run_id)
    assert code == 0 and "chain of rtl:timer:" in out
    for iid in (PLAN, EXPAND, OPEN_PR, PR_GATE):
        assert iid in out
    if HAS_VERILATOR:  # the project's own `make lint` (Verilator) ran on each module
        assert _lint_statuses(root) == ["pass"] * 3


def _all_done(root: Path) -> list[str]:
    ctx = AppContext.load(root)
    from chipgraph.core.state.journal import read, replay

    out: list[str] = []
    for run in sorted(p.name for p in ctx.layout.runs_dir.iterdir()):
        state = replay(read(ctx.layout.journal(run)).events)
        out.extend(i for i, s in state.rules.items() if s == "done")
    return out


def test_the_chain_with_the_github_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHIPGRAPH_GITHUB_TOKEN", TOKEN)
    base = target_project(tmp_path / "tinysoc")
    remote = bare_remote(base, tmp_path / "remote.git")
    with github_for(remote) as fake:
        data = yaml.safe_load((base / ".chipgraph.yml").read_text(encoding="utf-8"))
        data["review"] = github_profile(fake)
        (base / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False))
        runtime = ChainRuntime(base, [BAD, GOOD])
        _use(monkeypatch, runtime)
        _plan_and_module_phase(base, runtime)
        head = git(base, "rev-parse", "HEAD")

        record = json.loads((base / PR_RECORD).read_text(encoding="utf-8"))
        number = record["number"]
        assert number == 1 and record["review"] == "github" and record["base"] == "main"
        assert record["url"] == f"https://github.test/{fake.repo}/pull/1"
        assert record["remote"] == "origin"
        assert fake.branches[record["branch"]] == record["head_sha"]
        assert fake.prs[number]["head"] == {"ref": record["branch"], "sha": record["head_sha"]}
        body = fake.prs[number]["body"]
        for part in ("### Plan", "### Requirement coverage", "### Checks", "### Critic review"):
            assert part in body
        assert "major `rtl/tiny_timer_irq.sv:16` (REQ-TIM-003)" in body
        assert "1 minor or nit comment" in body
        assert "tiny_timer_counter" in body and "REQ-TIM-005" in body
        assert TOKEN not in body

        # The gate waits on the PR; the next steps say where.
        code, out = _cli(base, "rtl", "timer")
        assert code == 1
        assert f"review and approve the pull request {record['url']}" in out
        # The user's HEAD, branch and index were never touched by the branch it published.
        assert git(base, "rev-parse", "HEAD") == head
        assert git(base, "symbolic-ref", "--short", "HEAD") == "main"
        assert git(base, "diff", "--cached", "--name-only") == ""

        # A review on the PR at its head approves the gate; the run ends.
        fake.approve(number)
        code, out = _cli(base, "rtl", "timer")
        assert code == 0, out
        assert f"done: the pull request {record['url']} is approved; merging is yours" in out
        approved = json.loads((base / "pr" / "timer.approved.json").read_text(encoding="utf-8"))
        assert approved["head_sha"] == record["head_sha"] and approved["number"] == number

        # A push to the PR branch after the review makes the approval stale.
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        git(other, "checkout", "-q", record["branch"])
        (other / "NOTE.txt").write_text("after review\n")
        git(other, "add", "NOTE.txt")
        git(other, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-qm", "n")
        git(other, "push", "-q", "origin", record["branch"])
        fake.sync()
        assert fake.prs[number]["head"]["sha"] != record["head_sha"]
        from chipgraph.adapters.review.github import GitHubGateChecker

        ctx = AppContext.load(base)
        assert ctx.pr_review is not None
        checker = GitHubGateChecker(ctx.pr_review)
        assert checker.status("pr:timer", _instance(base, PR_GATE)) == "waiting"
        # chipgraph only created a PR and read: no merge, no close, no review submitted.
        assert {(r.method, r.path.rsplit("/", 1)[-1]) for r in fake.requests} <= {
            ("GET", "pulls"),
            ("POST", "pulls"),
            ("GET", "reviews"),
            ("GET", "1"),
        }


def _instance(root: Path, iid: str) -> RuleInstance:
    return project_graph(AppContext.load(root)).instances[iid]
