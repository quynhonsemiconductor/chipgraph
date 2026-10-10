"""M2-07: the pure pieces of the fan-out (F2, F4) and its profile section."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from chipgraph.core.config import FanoutCfg, Profile, load
from chipgraph.core.contracts import AgentResult
from chipgraph.core.engine.fanout import (
    BranchOutcome,
    FanoutError,
    check_write_sets,
    compare_assumptions,
    fanout_tmp_root,
    keyed_assumptions,
    normalize_assumption,
    validate_path,
)
from chipgraph.core.state.layout import StateLayout

# --- F4 ---------------------------------------------------------------------------------


def test_normalize_folds_case_whitespace_and_punctuation() -> None:
    assert normalize_assumption("  8 Bits. ") == normalize_assumption("8 bits")
    assert normalize_assumption("Active-LOW reset!") == "active low reset"
    assert normalize_assumption("1.5 V") != normalize_assumption("15 V")
    assert normalize_assumption("1.5 V") == "1.5 v"


def test_keyed_assumptions_reads_key_value_statements() -> None:
    keys = keyed_assumptions(
        ["interface:uart_if.width = 8", "a free text assumption", "reset=active low", "k=1", "k=2"]
    )
    assert keys == {"interface:uart_if.width": "8", "reset": "active low", "k": "2"}


def test_compare_finds_a_mismatch_with_both_statements() -> None:
    mismatches = compare_assumptions(
        {
            "tb": {"interface:uart_if.width": "16"},
            "rtl": {"interface:uart_if.width": "8", "clock": "50 MHz"},
            "doc": {"Clock": "50 mhz."},
        }
    )
    assert len(mismatches) == 1
    (m,) = mismatches
    assert m.key == "interface:uart_if.width"
    assert [(s.branch, s.value) for s in m.statements] == [("rtl", "8"), ("tb", "16")]
    assert "'8'" in m.question() and "'16'" in m.question()


def test_compare_passes_equal_and_single_keys() -> None:
    assert compare_assumptions({"a": {"x": "On"}, "b": {"X ": "on!"}, "c": {"y": "1"}}) == ()
    assert compare_assumptions({}) == ()


def test_outcome_from_agent_uses_its_keyed_assumptions() -> None:
    result = AgentResult(status="done", assumptions=("bus.width=32", "used the default clock"))
    outcome = BranchOutcome.from_agent(result)
    assert outcome.ok
    assert outcome.assumption_keys == {"bus.width": "32"}
    explicit = BranchOutcome.from_agent(result, assumption_keys={"k": "v"})
    assert explicit.assumption_keys == {"k": "v"}
    failed = BranchOutcome.from_agent(AgentResult(status="failed"))
    assert not failed.ok and failed.message == "agent failed"


# --- F2 and paths -----------------------------------------------------------------------


def test_check_write_sets_names_both_branches_and_the_file() -> None:
    check_write_sets({"a": ["x.v"], "b": ["y.v"]})
    with pytest.raises(FanoutError, match=r"'a' and 'b' both write 'X\.v'"):
        check_write_sets({"a": ["x.v"], "b": ["X.v"]})  # same file on a case-insensitive fs
    with pytest.raises(FanoutError, match="shared file"):
        check_write_sets({"a": ["top.v"]}, shared=["top.v"])


def test_validate_path_accepts_normal_paths() -> None:
    assert validate_path("rtl/uart/uart.sv") == "rtl/uart/uart.sv"
    assert validate_path(".github/x.yml") == ".github/x.yml"
    for bad in ("", "C:/x", "a\\b", "a\0b", "a/", "/a"):
        with pytest.raises(FanoutError):
            validate_path(bad)


# --- where run directories go -----------------------------------------------------------


def test_tmp_root_is_system_temp_when_the_state_dir_is_in_the_repo(tmp_path: Path) -> None:
    layout = StateLayout(tmp_path)
    assert layout.tmp_dir == tmp_path / ".chipgraph" / "state" / "tmp"
    assert fanout_tmp_root(tmp_path, layout) == Path(tempfile.gettempdir())
    assert fanout_tmp_root(tmp_path) == Path(tempfile.gettempdir())
    elsewhere = StateLayout(tmp_path / "state-home")
    repo = tmp_path / "repo"
    assert fanout_tmp_root(repo, elsewhere) == elsewhere.tmp_dir / "fanout"


# --- the profile section ----------------------------------------------------------------


def test_fanout_cfg_defaults_and_validation() -> None:
    cfg = Profile(project="p").fanout
    assert cfg == FanoutCfg()
    assert cfg.max_parallel is None and cfg.branch_timeout_s is None
    assert cfg.effective_max_parallel(cpu_count=16) == 4
    assert cfg.effective_max_parallel(cpu_count=2) == 2
    assert FanoutCfg(max_parallel=7).effective_max_parallel(cpu_count=2) == 7
    for bad in ({"max_parallel": 0}, {"branch_timeout_s": 0}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            FanoutCfg.model_validate(bad)


def test_the_profile_sets_fanout_and_config_check_warns(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / ".chipgraph.yml").write_text(
        "project: demo\nfanout:\n  max_parallel: 4096\n  branch_timeout_s: 900\n"
    )
    resolved = load(tmp_path, user_config=tmp_path / "nouser.yml")
    assert resolved is not None
    assert resolved.profile.fanout == FanoutCfg(max_parallel=4096, branch_timeout_s=900)
    issues = [i for i in resolved.check() if i.key == "fanout.max_parallel"]
    assert len(issues) == 1 and issues[0].severity == "warning"
    shown = {key: value for key, value, _ in resolved.explain()}
    assert shown["fanout.max_parallel"] == 4096
