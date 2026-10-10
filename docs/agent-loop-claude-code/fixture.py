"""The M2-02b acceptance fixture: a tinysoc copy with two author tasks for the agent loop.

`make_loop_project(dest)` copies `examples/tinysoc` to `dest` (a new git repo; the
example itself is not changed) and adds a project pack `loop` with three rules:

- `loop/fixable` (agent, role `author`, 3 tries): write `doc/loop/fixable.md` from the
  task file `doc/tasks/fixable.md`. Its check `loop_fixable_check` fails until the
  file's first line is the marker `MARKER`, and its message names the marker, as
  `doc/loop/fixable.md:1: ...`. The task file does not mention the marker, so the
  first try almost surely fails and the redo text (which quotes the message) lets the
  author fix it on the second. Deterministic, no RTL knowledge needed.
- `loop/hopeless` (agent, role `author`, `budget.tries: 2`): write `doc/loop/hopeless.md`;
  its check `loop_hopeless_check` never passes. It must end `budget_exhausted` after
  exactly 2 dispatches, with HANDOFF.md naming it, its label and the reason.
- `loop/done` (gen, the target): hashes both outputs into `build/loop.done.json`; it is
  blocked while `loop/hopeless` is.

Neither agent rule sets a model tier: both come from the role (`medium`, escalating to
`large`); `tiers` maps them to models in the copy's profile (`run.sh`: haiku, sonnet).
The checks are Python one-liners with a `regex` parser, so each failure is an issue
with a `file:line`.

`extra_rules` and `extra_adapters` add rules and checks to the pack and profile (the
tests use them for an infra failure and other cases).

Command line, used by `run.sh`::

    python docs/agent-loop-claude-code/fixture.py DEST [--medium-model M] [--large-model M]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "tinysoc"

TARGET = "loop/done"
FIXABLE_TASK = "loop/fixable[]"
HOPELESS_TASK = "loop/hopeless[]"
DONE_TASK = "loop/done[]"
FIXABLE_OUTPUT = "doc/loop/fixable.md"
HOPELESS_OUTPUT = "doc/loop/hopeless.md"
FIXABLE_SPEC = "doc/tasks/fixable.md"
HOPELESS_SPEC = "doc/tasks/hopeless.md"
MARKER = "LOOP-FIXED-7Q2"
HOPELESS_TRIES = 2

ISSUE_REGEX = r"^(?P<file>[^\s:]+):(?P<line>\d+): (?P<msg>.+)$"
"""`path:line: message`, the shape both checks print."""

FIXABLE_TEXT = """\
# fixable

Write `doc/loop/fixable.md`: three short lines that describe the `tiny_timer` block of
this project (what it counts, its reset, one register). Plain text, no heading.
"""

HOPELESS_TEXT = """\
# hopeless

Write `doc/loop/hopeless.md`: one short line that names the `tiny_gpio` block of this
project.

This is an unattended run: nobody can answer questions. Always write your best attempt
and finish with `status: done`; do not stop to ask.
"""

_FIXABLE_CHECK = (
    "import pathlib, sys; p = pathlib.Path('doc/loop/fixable.md'); "
    "t = p.read_text() if p.is_file() else ''; "
    "lines = t.splitlines(); ok = bool(lines) and lines[0].strip() == '" + MARKER + "'; "
    "print('' if ok else 'doc/loop/fixable.md:1: the first line must be exactly "
    + MARKER
    + " (the project marker), then the text'); sys.exit(0 if ok else 1)"
)
_HOPELESS_CHECK = (
    "import sys; print('doc/loop/hopeless.md:1: the first line must be the signed-off "
    "tape-out code of the block'); sys.exit(1)"
)
_DONE = (
    "import hashlib, json, pathlib; out = pathlib.Path('build'); out.mkdir(exist_ok=True); "
    "h = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest(); "
    "(out / 'loop.done.json').write_text(json.dumps(dict("
    "fixable=h('doc/loop/fixable.md'), hopeless=h('doc/loop/hopeless.md'))))"
)
# (no braces in cmd tokens: the `cmd` tool formats them with the instance's params)

_PACK = {
    "name": "loop",
    "version": "0.1.0",
    "description": "M2-02b acceptance fixture: a fixable and a hopeless agent task on tinysoc.",
    "provides": {"rules": ["rules"]},
}

FIXABLE_RULE: dict[str, Any] = {
    "rule": "fixable",
    "kind": "agent",
    "role": "author",
    "description": f"Write {FIXABLE_OUTPUT} as {FIXABLE_SPEC} says; pass its check.",
    "inputs": [{"path": FIXABLE_SPEC}],
    "outputs": [FIXABLE_OUTPUT],
    "checks": ["loop_fixable_check"],
    "budget": {"tries": 3},
}

HOPELESS_RULE: dict[str, Any] = {
    "rule": "hopeless",
    "kind": "agent",
    "role": "author",
    "description": f"Write {HOPELESS_OUTPUT} as {HOPELESS_SPEC} says; pass its check.",
    "inputs": [{"path": HOPELESS_SPEC}],
    "outputs": [HOPELESS_OUTPUT],
    "checks": ["loop_hopeless_check"],
    "budget": {"tries": HOPELESS_TRIES},
}

DONE_RULE: dict[str, Any] = {
    "rule": "done",
    "kind": "gen",
    "inputs": [{"path": FIXABLE_OUTPUT}, {"path": HOPELESS_OUTPUT}],
    "outputs": ["build/loop.done.json"],
    "run": {"use": "cmd", "args": {"cmd": ["python3", "-c", _DONE]}},
}


def check(code: str) -> dict[str, Any]:
    """A profile `cmd` check running a Python one-liner, parsed as `path:line: msg`."""
    return {"use": "cmd", "cmd": ["python3", "-c", code], "regex": ISSUE_REGEX}


def _git(root: Path, *args: str) -> None:
    # No background maintenance: it can still hold `.git/objects/maintenance.lock` when a
    # test copies the project right after the commit.
    subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def make_loop_project(
    dest: Path,
    *,
    tiers: Mapping[str, str] | None = None,
    extra_rules: Iterable[Mapping[str, Any]] = (),
    extra_adapters: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    """Copy tinysoc to `dest`, add the `loop` pack and profile entries, commit."""
    shutil.copytree(EXAMPLE, dest)
    pack = dest / ".chipgraph" / "packs" / "loop"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text(yaml.safe_dump(_PACK, sort_keys=False))
    for rule in (FIXABLE_RULE, HOPELESS_RULE, DONE_RULE, *extra_rules):
        (pack / "rules" / f"{rule['rule']}.yml").write_text(
            yaml.safe_dump(dict(rule), sort_keys=False)
        )
    for rel, text in ((FIXABLE_SPEC, FIXABLE_TEXT), (HOPELESS_SPEC, HOPELESS_TEXT)):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(text)

    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = [*profile.get("packs", []), "loop"]
    adapters = profile.setdefault("adapters", {})
    adapters["loop_fixable_check"] = check(_FIXABLE_CHECK)
    adapters["loop_hopeless_check"] = check(_HOPELESS_CHECK)
    for name, cfg in (extra_adapters or {}).items():
        adapters[name] = dict(cfg)
    if tiers:
        profile.setdefault("models", {})["tiers"] = dict(tiers)
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m2-02b@example.invalid")
    _git(dest, "config", "user.name", "m2-02b")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with a fixable and a hopeless agent task")
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
    make_loop_project(args.dest, tiers=tiers or None)
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
