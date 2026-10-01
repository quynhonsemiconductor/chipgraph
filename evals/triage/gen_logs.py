"""Generate the `/triage` sample logs from injected faults (task M1-14).

    uv run python evals/triage/gen_logs.py [--set holdout|holdout2] [--only ID ...] [--out DIR]
                                           [--check]

Every sample in `faults.yml` is produced the same way, so its label is fixed by the fault
that was injected and never by a model:

1. copy `examples/tinysoc` to a fresh temporary directory (its own git repo), plus the
   testbenches the sample names (`evals/triage/tb/<file>` -> `tb/<file>` in the copy);
2. apply the sample's `fault`: text replacements (`edit`), file removals (`delete`),
   permission changes (`chmod`), a reduced `PATH` (`path_only`: only these tools), and a
   shell resource limit for the command (`ulimit`, for example `-f 0`);
3. run `chipgraph ingest` in the copy if the sample sets `ingest: true` (after the fault,
   so the Design Model is built from the faulty files), then remove what `after_ingest`
   deletes;
4. run the sample's `cmd` with `/bin/sh -c` in the copy, with a clean environment
   (`HOME` in the temporary directory, `LC_ALL=C`), and write `logs/<id>.log` (stdout
   then stderr) and `logs/<id>.json` (command, exit code, check id, Verilator version).

The log is normalised so it is stable and holds no machine path: the copy's absolute path
becomes repo-relative, the Verilator install path becomes `$VERILATOR_ROOT`, and wall-clock
times, speeds and memory sizes become `N`. Running this twice gives byte-identical files;
`--check` regenerates into a temporary directory and compares, without writing.

`--set holdout` does the same for the holdout set: `holdout.yml` (ids `hold-NN`, used only
for grading, never for writing triage rules) into `logs-holdout/`; its testbenches are
named relative to `tb/` (for example `holdout/tb_soc_regs.sv`) and copied to `tb/<name>`.

`--set holdout2` is the second holdout set: `holdout2.yml` (ids `h2-NN`, grading only) into
`logs-holdout2/`, testbenches under `tb/holdout2/`. It adds one fault kind, `env`: extra
environment variables for the command (for example a stale `VERILATOR_ROOT`).

CI does not run this (it needs Verilator); `tests/evals/test_triage_samples.py` checks the
committed logs against `faults.yml`. Needs `verilator` (5.x) and `make` on `PATH`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TINYSOC = REPO / "examples" / "tinysoc"
FAULTS = HERE / "faults.yml"
LOGS = HERE / "logs"
TB_DIR = HERE / "tb"
HOLDOUT = HERE / "holdout.yml"
LOGS_HOLDOUT = HERE / "logs-holdout"
HOLDOUT2 = HERE / "holdout2.yml"
LOGS_HOLDOUT2 = HERE / "logs-holdout2"
# Sample sets: (faults file, logs directory, id prefix). `default` is the set the triage
# rules were written against; `holdout` and `holdout2` are for grading only.
SETS = {
    "default": (FAULTS, LOGS, "log"),
    "holdout": (HOLDOUT, LOGS_HOLDOUT, "hold"),
    "holdout2": (HOLDOUT2, LOGS_HOLDOUT2, "h2"),
}
LABELS = ("infra", "rtl", "tb", "spec")
DEFAULT_TIMEOUT_S = 300.0
# The variables every command gets from `_env`; a sample's `env` adds to them, never resets one.
_BASE_ENV = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "LC_ALL",
    "LANG",
    "TERM",
    "NO_COLOR",
    "COLUMNS",
    "PYTHONHASHSEED",
)

_TIMES = (
    (re.compile(r"[Ww]alltime \d+(?:\.\d+)? s(?: \([^)]*\))?"), "walltime N s"),
    (re.compile(r"\d+\.\d+ MB"), "N MB"),
    (re.compile(r"speed \d+(?:\.\d+)? [a-z]+/s"), "speed N /s"),
    (re.compile(r"cpu \d+(?:\.\d+)? s on \d+ threads"), "cpu N s on N threads"),
    (re.compile(r"allocated \d+(?:\.\d+)? [kMGT]?B"), "allocated N MB"),
    (re.compile(r"\b\d+(?:\.\d+)? ?(?:ms|sec|seconds)\b"), "N s"),
)


class FaultError(ValueError):
    """A sample in faults.yml is malformed or its fault cannot be applied."""


@dataclass(frozen=True)
class Edit:
    path: str
    find: str
    replace: str


@dataclass(frozen=True)
class Sample:
    id: str
    label: str
    description: str
    cmd: str
    check: str | None = None
    tb: tuple[str, ...] = ()
    ingest: bool = False
    edits: tuple[Edit, ...] = ()
    delete: tuple[str, ...] = ()
    after_ingest_delete: tuple[str, ...] = ()
    chmod: tuple[tuple[str, int], ...] = ()
    path_only: tuple[str, ...] | None = None
    ulimit: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    timeout_s: float = DEFAULT_TIMEOUT_S
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _strings(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FaultError(f"{where}: expected a list of strings")
    return tuple(value)


def load_samples(path: Path = FAULTS, *, prefix: str = "log") -> list[Sample]:
    """The samples of a faults file, validated (ids unique, labels known, faults well formed)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise FaultError(f"{path}: expected a mapping with a 'samples' list")
    samples: list[Sample] = []
    seen: set[str] = set()
    for item in data["samples"]:
        if not isinstance(item, dict):
            raise FaultError(f"{path}: every sample must be a mapping")
        sid = item.get("id")
        if not isinstance(sid, str) or not re.fullmatch(rf"{re.escape(prefix)}-\d{{2}}", sid):
            raise FaultError(f"{path}: sample id {sid!r} must look like '{prefix}-01'")
        if sid in seen:
            raise FaultError(f"{path}: duplicate sample id {sid!r}")
        seen.add(sid)
        label = item.get("label")
        if label not in LABELS:
            raise FaultError(f"{sid}: label {label!r} is not one of {LABELS}")
        for key in ("description", "cmd"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise FaultError(f"{sid}: '{key}' is required")
        fault = item.get("fault") or {}
        if not isinstance(fault, dict) or not fault:
            raise FaultError(f"{sid}: 'fault' must describe the injected fault")
        edits = []
        for edit in fault.get("edit") or []:
            if not isinstance(edit, dict) or not all(
                isinstance(edit.get(k), str) for k in ("path", "find", "replace")
            ):
                raise FaultError(f"{sid}: an edit needs 'path', 'find' and 'replace'")
            edits.append(Edit(edit["path"], edit["find"], edit["replace"]))
        chmod = []
        for entry in fault.get("chmod") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise FaultError(f"{sid}: a chmod needs 'path' and 'mode'")
            chmod.append((entry["path"], int(str(entry["mode"]), 8)))
        path_only = fault.get("path_only")
        ulimit = fault.get("ulimit")
        if ulimit is not None and (
            not isinstance(ulimit, str) or not re.fullmatch(r"-[a-z] \d+", ulimit)
        ):
            raise FaultError(f"{sid}: 'ulimit' must look like '-f 0'")
        env = fault.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", k) and isinstance(v, str)
            for k, v in env.items()
        ):
            raise FaultError(f"{sid}: 'env' must map variable names (A-Z0-9_) to strings")
        if set(env) & set(_BASE_ENV):
            raise FaultError(f"{sid}: 'env' may not set {sorted(set(env) & set(_BASE_ENV))}")
        samples.append(
            Sample(
                id=sid,
                label=label,
                description=item["description"].strip(),
                cmd=item["cmd"].strip(),
                check=item.get("check"),
                tb=_strings(item.get("tb"), f"{sid}.tb"),
                ingest=bool(item.get("ingest", False)),
                edits=tuple(edits),
                delete=_strings(fault.get("delete"), f"{sid}.fault.delete"),
                after_ingest_delete=_strings(
                    fault.get("after_ingest_delete"), f"{sid}.fault.after_ingest_delete"
                ),
                chmod=tuple(chmod),
                path_only=None
                if path_only is None
                else _strings(path_only, f"{sid}.fault.path_only"),
                ulimit=ulimit,
                env=tuple(sorted(env.items())),
                timeout_s=float(item.get("timeout_s", DEFAULT_TIMEOUT_S)),
                raw=item,
            )
        )
    return samples


# --- running one sample ------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    identity = ["-c", "user.email=triage@example.invalid", "-c", "user.name=triage"]
    subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True)


def _prepare(sample: Sample, root: Path) -> None:
    shutil.copytree(TINYSOC, root, ignore=shutil.ignore_patterns("build", "obj_dir", "state"))
    for name in sample.tb:
        source = TB_DIR / name
        if not source.is_file():
            raise FaultError(f"{sample.id}: no testbench {source.relative_to(REPO)}")
        (root / "tb").mkdir(exist_ok=True)
        shutil.copy2(source, root / "tb" / Path(name).name)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tinysoc")
    for edit in sample.edits:
        target = root / edit.path
        text = target.read_text(encoding="utf-8")
        count = text.count(edit.find)
        if count != 1:
            raise FaultError(
                f"{sample.id}: {edit.path} has {count} matches of {edit.find!r} (needs 1)"
            )
        target.write_text(text.replace(edit.find, edit.replace), encoding="utf-8")
    for rel in sample.delete:
        _remove(root / rel)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _tool_path(sample: Sample, work: Path) -> str:
    """The `PATH` for the command: this interpreter's bin (for `chipgraph`) and the tools."""
    if sample.path_only is None:
        return os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    bin_dir = work / "bin"
    bin_dir.mkdir()
    for tool in sample.path_only:
        found = shutil.which(tool)
        if found is None:
            raise FaultError(f"{sample.id}: tool {tool!r} is not on PATH")
        (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def _env(path: str, work: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": path,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LC_ALL": "C",
        "LANG": "C",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "COLUMNS": "100",
        "PYTHONHASHSEED": "0",
    }


def verilator_root() -> str | None:
    """Where Verilator is installed (`verilator --getenv VERILATOR_ROOT`), if it is."""
    if shutil.which("verilator") is None:
        return None
    out = subprocess.run(
        ["verilator", "--getenv", "VERILATOR_ROOT"], capture_output=True, text=True, check=False
    )
    root = out.stdout.strip()
    return root or None


def verilator_version() -> str | None:
    if shutil.which("verilator") is None:
        return None
    out = subprocess.run(["verilator", "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"Verilator (\d+\.\d+)", out.stdout)
    return match.group(1) if match else None


def normalise(text: str, *, roots: list[Path], replacements: dict[str, str]) -> str:
    """The log with machine paths made repo-relative and run-to-run noise replaced."""
    for root in sorted({str(r) for r in roots}, key=len, reverse=True):
        text = text.replace(root + "/", "").replace(root, ".")
    for old, new in sorted(replacements.items(), key=lambda kv: len(kv[0]), reverse=True):
        if old:
            text = text.replace(old, new)
    for pattern, repl in _TIMES:
        text = pattern.sub(repl, text)
    return text


def run_sample(sample: Sample) -> tuple[str, dict[str, Any]]:
    """Inject `sample`'s fault into a tinysoc copy, run its command: (log, metadata)."""
    with tempfile.TemporaryDirectory(prefix="cg-triage-") as tmp:
        work = Path(tmp)
        root = work / "tinysoc"
        _prepare(sample, root)
        base_env = {**_env(_tool_path(sample, work), work), **dict(sample.env)}
        if sample.ingest:
            # Setup, not the sample: ingest always runs with the full PATH.
            ingest_env = _env(_tool_path(_with_full_path(sample), work), work)
            done = subprocess.run(
                ["chipgraph", "ingest"],
                cwd=root,
                env=ingest_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode not in (0, 1):
                raise FaultError(f"{sample.id}: chipgraph ingest failed:\n{done.stderr}")
        for rel in sample.after_ingest_delete:
            _remove(root / rel)
        for rel, mode in sample.chmod:
            (root / rel).chmod(mode)
        cmd = sample.cmd if sample.ulimit is None else f"ulimit {sample.ulimit}; {sample.cmd}"
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=root,
                env=base_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=sample.timeout_s,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        finally:
            for rel, _ in sample.chmod:
                (root / rel).chmod(0o644)
        roots = [root, root.resolve(), work, work.resolve()]
        replacements = {str(Path(sys.executable).parent) + "/": ""}
        vroot = verilator_root()
        if vroot:
            replacements[vroot] = "$VERILATOR_ROOT"
        replacements[str(Path.home())] = "~"
        log = normalise(stdout + stderr, roots=roots, replacements=replacements)
        meta = {
            "id": sample.id,
            "cmd": sample.cmd,
            "exit_code": code,
            "check": sample.check,
            "verilator": verilator_version(),
        }
        return log, meta


def _with_full_path(sample: Sample) -> Sample:
    """`sample` without its `path_only`, for the setup steps (ingest needs chipgraph)."""
    return Sample(**{**sample.__dict__, "path_only": None})


def write_sample(sample: Sample, out: Path) -> None:
    log, meta = run_sample(sample)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{sample.id}.log").write_text(log, encoding="utf-8")
    (out / f"{sample.id}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the /triage sample logs.")
    parser.add_argument("--only", nargs="*", default=None, help="sample ids to generate")
    parser.add_argument(
        "--set", choices=sorted(SETS), default="default", help="sample set (default: the 22)"
    )
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temporary directory and compare with --out, writing nothing",
    )
    args = parser.parse_args(argv)
    faults, logs, prefix = SETS[args.set]
    if args.out is None:
        args.out = logs
    try:
        samples = load_samples(faults, prefix=prefix)
    except (FaultError, OSError, yaml.YAMLError) as exc:
        print(f"gen_logs.py: {exc}", file=sys.stderr)
        return 2
    if args.only:
        unknown = set(args.only) - {s.id for s in samples}
        if unknown:
            print(f"gen_logs.py: unknown sample ids {sorted(unknown)}", file=sys.stderr)
            return 2
        samples = [s for s in samples if s.id in args.only]
    if shutil.which("verilator") is None:
        print("gen_logs.py: verilator is not on PATH", file=sys.stderr)
        return 2

    if not args.check:
        for sample in samples:
            write_sample(sample, args.out)
            print(f"{sample.id}  {sample.label:5}  {sample.description}")
        return 0

    differ = []
    with tempfile.TemporaryDirectory(prefix="cg-triage-check-") as tmp:
        for sample in samples:
            write_sample(sample, Path(tmp))
            for suffix in (".log", ".json"):
                name = sample.id + suffix
                new = (Path(tmp) / name).read_bytes()
                old_path = args.out / name
                if not old_path.is_file() or old_path.read_bytes() != new:
                    differ.append(name)
    for name in differ:
        print(f"differs: {name}")
    print("identical" if not differ else f"{len(differ)} file(s) differ")
    return 0 if not differ else 1


if __name__ == "__main__":
    sys.exit(main())
