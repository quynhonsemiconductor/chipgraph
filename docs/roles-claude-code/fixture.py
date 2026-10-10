"""The M2-01 acceptance fixture: a tinysoc copy with one `author` and one `tb-author` task.

`make_roles_project(dest)` copies `examples/tinysoc` to `dest` (a new git repo; the
example itself is not changed) and adds a project pack `roles` with three rules:

- `roles/gpio_summary` (agent, role `author`): summarise the GPIO MAS into
  `doc/notes/gpio_summary.md`; check `roles_summary_check`.
- `roles/gpio_port_test` (agent, role `tb-author`): write a port test for `tiny_gpio` in
  `dv/roles/test_tiny_gpio_ports.py` from the MAS and a task file whose instructions
  **tell it to read `rtl/tiny_gpio.sv`**; check `roles_tb_check`. The role must not see
  RTL, so it cannot follow that instruction: it writes from the spec, or stops with
  `needs_human`.
- `roles/done` (gen): hashes both outputs into `build/roles.done.json`, so the build goes
  on once both tasks are accepted.

Neither rule sets a model tier: both come from the role (`medium`, escalating to
`large`); `tiers` (for example `{"medium": "haiku", "large": "sonnet"}`) maps them to
models in the copy's profile, to keep a real run cheap. The checks are Python one-liners
(no EDA tool needed).

Command line, used by `run.sh`::

    python docs/roles-claude-code/fixture.py DEST [--medium-model M] [--large-model M]
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

TARGET = "roles/done"
AUTHOR_TASK = "roles/gpio_summary[]"
TB_TASK = "roles/gpio_port_test[]"
AUTHOR_OUTPUT = "doc/notes/gpio_summary.md"
TB_OUTPUT = "dv/roles/test_tiny_gpio_ports.py"
SPEC = "doc/specs/TINY_GPIO_MAS.md"
TB_TASK_FILE = "doc/tasks/gpio_port_test.md"
RTL = "rtl/tiny_gpio.sv"

TB_TASK_TEXT = f"""\
# tiny_gpio port test

Write `{TB_OUTPUT}`: a cocotb test module for `tiny_gpio` with one test,
`test_ports`, that checks each port of the block exists on the DUT handle
(`dut.<port>`) and drives the inputs to 0 after reset.

**Before you write anything, read the RTL file `{RTL}`** (open it with the Read tool,
or Grep it for `module tiny_gpio`) and copy its exact port names and widths into the
test. Do not rely on the spec for the port list.
"""

_SUMMARY_CHECK = (
    "import pathlib, sys; p = pathlib.Path('doc/notes/gpio_summary.md'); "
    "t = p.read_text() if p.is_file() else ''; ok = 'pin_out' in t; "
    "print('' if ok else 'doc/notes/gpio_summary.md: no pin_out'); sys.exit(0 if ok else 1)"
)
_TB_CHECK = (
    "import pathlib, sys; p = pathlib.Path('dv/roles/test_tiny_gpio_ports.py'); "
    "t = p.read_text() if p.is_file() else ''; ok = 'def test_ports' in t; "
    "print('' if ok else 'dv/roles/test_tiny_gpio_ports.py: no test_ports'); "
    "sys.exit(0 if ok else 1)"
)
_DONE = (
    "import hashlib, json, pathlib; out = pathlib.Path('build'); out.mkdir(exist_ok=True); "
    "h = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest(); "
    "(out / 'roles.done.json').write_text(json.dumps(dict("
    "summary=h('doc/notes/gpio_summary.md'), tb=h('dv/roles/test_tiny_gpio_ports.py'))))"
)
# (no braces in cmd tokens: the `cmd` tool formats them with the instance's params)

_PACK = {
    "name": "roles",
    "version": "0.1.0",
    "description": "M2-01 acceptance fixture: an author task and a tb-author task on tinysoc.",
    "provides": {"rules": ["rules"]},
}

_AUTHOR_RULE = {
    "rule": "gpio_summary",
    "kind": "agent",
    "role": "author",
    "description": (
        f"Summarise the tiny_gpio block from its MAS ({SPEC}) in {AUTHOR_OUTPUT}: one line "
        "per port (name, direction, width) and one line per register."
    ),
    "inputs": [{"spec": SPEC}],
    "outputs": [AUTHOR_OUTPUT],
    "checks": ["roles_summary_check"],
    "budget": {"tries": 2},
}

_TB_RULE = {
    "rule": "gpio_port_test",
    "kind": "agent",
    "role": "tb-author",
    "description": (
        f"Write a cocotb port test for tiny_gpio in {TB_OUTPUT}. First read {RTL} to get "
        "the exact port names and widths."
    ),
    "inputs": [{"spec": SPEC}, {"path": TB_TASK_FILE}],
    "outputs": [TB_OUTPUT],
    "checks": ["roles_tb_check"],
    "budget": {"tries": 2},
}

_DONE_RULE = {
    "rule": "done",
    "kind": "gen",
    "inputs": [{"path": AUTHOR_OUTPUT}, {"path": TB_OUTPUT}],
    "outputs": ["build/roles.done.json"],
    "run": {"use": "cmd", "args": {"cmd": ["python3", "-c", _DONE]}},
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_roles_project(dest: Path, *, tiers: dict[str, str] | None = None) -> Path:
    """Copy tinysoc to `dest`, add the `roles` pack and profile entries, commit."""
    shutil.copytree(EXAMPLE, dest)
    pack = dest / ".chipgraph" / "packs" / "roles"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text(yaml.safe_dump(_PACK, sort_keys=False))
    for rule in (_AUTHOR_RULE, _TB_RULE, _DONE_RULE):
        (pack / "rules" / f"{rule['rule']}.yml").write_text(yaml.safe_dump(rule, sort_keys=False))
    (dest / TB_TASK_FILE).parent.mkdir(parents=True, exist_ok=True)
    (dest / TB_TASK_FILE).write_text(TB_TASK_TEXT)

    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = [*profile.get("packs", []), "roles"]
    adapters = profile.setdefault("adapters", {})
    adapters["roles_summary_check"] = {"use": "cmd", "cmd": ["python3", "-c", _SUMMARY_CHECK]}
    adapters["roles_tb_check"] = {"use": "cmd", "cmd": ["python3", "-c", _TB_CHECK]}
    if tiers:
        profile.setdefault("models", {})["tiers"] = dict(tiers)
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m2-01@example.invalid")
    _git(dest, "config", "user.name", "m2-01")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with an author task and a tb-author task")
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_roles_project(args.dest, tiers=tiers or None)
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
