"""Helpers for the `learn`/`try` tests: build small synthetic repos in `tmp_path`.

Tests never write into the source tree (they build under `tmp_path`) and every git repo
they create uses `-b main`. The synthetic repos cover the two common shapes: a per-block
file stem (`filelists/<block>.f`, tinysoc style) and a single-IP tree with no filelist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_QSOC_CLONE = Path("/tmp/qsoc-clone")
_OPENTITAN_IP = _QSOC_CLONE / "vendor/lowrisc/opentitan/hw/ip/aon_timer"
_PULP_IP = _QSOC_CLONE / "vendor/pulp-platform/apb_uart"


def git_init(root: Path) -> None:
    """`git init -b main` and a first commit of everything under `root`."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "learn@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "learn"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial import"], cwd=root, check=True)


def git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout


def file_tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` (except `.git`) as `relpath -> bytes`, for a byte-for-byte diff."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


_TIMER_SV = """\
// SPDX-License-Identifier: Apache-2.0
module blk_timer (
    input  logic       i_clk,
    input  logic       i_rst_n,
    output logic       o_irq
);
  logic r_count;
  logic w_next;
  blk_sub u_sub (.i_clk(i_clk));
  assign o_irq = r_count;
endmodule
"""

_GPIO_SV = """\
// SPDX-License-Identifier: Apache-2.0
module blk_gpio (
    input  logic       i_clk,
    input  logic       i_rst_n,
    output logic [7:0] o_pins
);
  logic [7:0] r_out;
  logic [7:0] w_dir;
  blk_pad u_pad (.i_clk(i_clk));
  assign o_pins = r_out;
endmodule
"""


def make_prefixed_repo(root: Path) -> Path:
    """A tinysoc-shaped repo whose ports/signals/instances use i_/o_, r_/w_, u_ prefixes.

    Filelist paths are relative to each filelist (so the `filelist` check fits).
    """
    (root / "rtl").mkdir(parents=True)
    (root / "filelists").mkdir()
    (root / "doc/specs").mkdir(parents=True)
    (root / "rtl/blk_timer.sv").write_text(_TIMER_SV, encoding="utf-8")
    (root / "rtl/blk_gpio.sv").write_text(_GPIO_SV, encoding="utf-8")
    # Filelist paths are relative to the filelist's own directory (../rtl/...).
    (root / "filelists/timer.f").write_text("../rtl/blk_timer.sv\n", encoding="utf-8")
    (root / "filelists/gpio.f").write_text("../rtl/blk_gpio.sv\n", encoding="utf-8")
    (root / "doc/specs/BLK_TIMER_MAS.md").write_text(
        "# Timer\n\n## 1. Overview\n\n## 2. Registers\n", encoding="utf-8"
    )
    (root / "doc/specs/BLK_GPIO_MAS.md").write_text(
        "# GPIO\n\n## 1. Overview\n\n## 2. Registers\n", encoding="utf-8"
    )
    return root


def make_single_ip_repo(root: Path) -> Path:
    """A single-IP repo (no filelist, no repeated block dir): `rtl/<name>.sv` only."""
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/widget.sv").write_text(_TIMER_SV.replace("blk_timer", "widget"), encoding="utf-8")
    (root / "rtl/widget_core.sv").write_text(
        _GPIO_SV.replace("blk_gpio", "widget_core"), encoding="utf-8"
    )
    return root


def make_vendored_repo(root: Path) -> Path:
    """A repo with a `vendor/` tree that keeps upstream conventions (should be exempted)."""
    make_prefixed_repo(root)
    (root / "vendor/acme/rtl").mkdir(parents=True)
    (root / "vendor/acme/rtl/AcmeThing.sv").write_text(
        "module AcmeThing(input Clk); endmodule\n", encoding="utf-8"
    )
    return root


def opentitan_ip_or_skip() -> Path | None:
    """The vendored OpenTitan `aon_timer` IP, or `None` when the QSoC clone is absent."""
    return _OPENTITAN_IP if _OPENTITAN_IP.is_dir() else None


def pulp_ip_or_skip() -> Path | None:
    """The vendored PULP `apb_uart` IP, or `None` when the QSoC clone is absent."""
    return _PULP_IP if _PULP_IP.is_dir() else None


def copy_ip(src: Path, dest: Path) -> Path:
    """Copy an IP tree into `dest` and `git init -b main` it there."""
    import shutil

    shutil.copytree(src, dest)
    git_init(dest)
    return dest


def rtl_file_count(root: Path) -> int:
    exts = (".sv", ".svh", ".v", ".vh")
    return sum(1 for p in root.rglob("*") if p.is_file() and p.suffix in exts)


__all__ = [
    "copy_ip",
    "file_tree",
    "git_init",
    "git_status",
    "make_prefixed_repo",
    "make_single_ip_repo",
    "make_vendored_repo",
    "opentitan_ip_or_skip",
    "pulp_ip_or_skip",
    "rtl_file_count",
]
