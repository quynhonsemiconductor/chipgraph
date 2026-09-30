"""A tinysoc copy with one agent rule, for the runtime claude-code tests (M1-11).

`make_pulse_project(dest)` copies `examples/tinysoc` to `dest` and adds a project pack
`pulse` with two rules:

- `pulse/tiny_pulse` (kind `agent`, role `author`, tier `small`, 2 tries): write
  `rtl/tiny_pulse.sv` from the spec `doc/tasks/tiny_pulse.md`; check `pulse_lint`.
- `pulse/pulse_manifest` (kind `gen`): hashes `rtl/tiny_pulse.sv` into
  `build/pulse.manifest.json`, so a build "goes on" once the agent task is accepted.

`pulse_lint` is a Python one-liner by default (no EDA tools needed), or real Verilator
lint with `lint="verilator"`. `examples/tinysoc` itself is never changed.

Also a command line, used by `docs/runtime-claude-code/run.sh` for the real Claude
Code run::

    python tests/mcp/runtime_tools_helpers.py DEST [--lint verilator]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "tinysoc"

TASK_ID = "pulse/tiny_pulse[]"
OUTPUT = "rtl/tiny_pulse.sv"
SPEC = "doc/tasks/tiny_pulse.md"

SPEC_TEXT = """\
# tiny_pulse

Write the SystemVerilog module `tiny_pulse` in `rtl/tiny_pulse.sv`.

Ports:

| Port    | Dir    | Width | Description                         |
|---------|--------|-------|-------------------------------------|
| clk     | input  | 1     | clock                               |
| rst_n   | input  | 1     | asynchronous reset, active low      |
| in      | input  | 1     | input level                         |
| pulse   | output | 1     | one-cycle pulse on each rising edge |

`pulse` is high for exactly one `clk` cycle after each rising edge of `in`: register
`in` once (`in_q`) and drive `pulse = in & ~in_q`. Reset clears the register.

Style: like `rtl/tiny_timer.sv` (an `always_ff` block with asynchronous reset, `logic`
types, a short header comment).
"""

GOOD_RTL = """\
// tiny_pulse: one-cycle pulse on each rising edge of `in`.
module tiny_pulse (
  input  logic clk,
  input  logic rst_n,
  input  logic in,
  output logic pulse
);
  logic in_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      in_q <= 1'b0;
    end else begin
      in_q <= in;
    end
  end

  assign pulse = in & ~in_q;
endmodule
"""

BAD_RTL = "// not a module yet\n"

_PY_LINT = (
    "import pathlib, sys; t = pathlib.Path('rtl/tiny_pulse.sv').read_text(); "
    "ok = 'module tiny_pulse' in t and 'endmodule' in t; "
    "print('' if ok else 'rtl/tiny_pulse.sv: no module tiny_pulse'); sys.exit(0 if ok else 1)"
)

_MANIFEST = (
    "import hashlib, json, pathlib; out = pathlib.Path('build'); out.mkdir(exist_ok=True); "
    "digest = hashlib.sha256(pathlib.Path('rtl/tiny_pulse.sv').read_bytes()).hexdigest(); "
    "(out / 'pulse.manifest.json').write_text(json.dumps(dict(rtl=digest)))"
)
# (no braces in cmd tokens: the `cmd` tool formats them with the instance's params)

_PACK = {
    "name": "pulse",
    "version": "0.1.0",
    "description": "M1-11 test fixture: one agent rule on tinysoc.",
    "provides": {"rules": ["rules"]},
}

_AGENT_RULE = {
    "rule": "tiny_pulse",
    "kind": "agent",
    "role": "author",
    "description": "Write the tiny_pulse RTL module from its spec.",
    "inputs": [{"path": SPEC}],
    "outputs": [OUTPUT],
    "checks": ["pulse_lint"],
    "budget": {"tries": 2, "tier": "small"},
}

_GEN_RULE = {
    "rule": "pulse_manifest",
    "kind": "gen",
    "inputs": [{"path": OUTPUT}],
    "outputs": ["build/pulse.manifest.json"],
    "run": {"use": "cmd", "args": {"cmd": ["python3", "-c", _MANIFEST]}},
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_pulse_project(
    dest: Path, *, lint: str = "python", nda: bool = False, runtime: str | None = None
) -> Path:
    """Copy tinysoc to `dest`, add the `pulse` pack and profile entries, commit. Returns `dest`."""
    shutil.copytree(EXAMPLE, dest)
    pack = dest / ".chipgraph" / "packs" / "pulse"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text(yaml.safe_dump(_PACK, sort_keys=False))
    (pack / "rules" / "tiny_pulse.yml").write_text(yaml.safe_dump(_AGENT_RULE, sort_keys=False))
    (pack / "rules" / "pulse_manifest.yml").write_text(yaml.safe_dump(_GEN_RULE, sort_keys=False))
    (dest / SPEC).parent.mkdir(parents=True, exist_ok=True)
    (dest / SPEC).write_text(SPEC_TEXT)

    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = [*profile.get("packs", []), "pulse"]
    if lint == "verilator":
        check = {
            "use": "cmd",
            "cmd": f"verilator --lint-only -Wall {OUTPUT}",
            "parser": "verilator",
        }
    else:
        check = {"use": "cmd", "cmd": ["python3", "-c", _PY_LINT]}
    profile.setdefault("adapters", {})["pulse_lint"] = check
    if nda:
        profile["data"] = {"nda_paths": [SPEC]}
    if runtime is not None:
        profile["runtime"] = runtime
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m1-11@example.invalid")
    _git(dest, "config", "user.name", "m1-11")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with the pulse agent rule")
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--lint", choices=["python", "verilator"], default="python")
    args = parser.parse_args(argv)
    make_pulse_project(args.dest, lint=args.lint)
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
