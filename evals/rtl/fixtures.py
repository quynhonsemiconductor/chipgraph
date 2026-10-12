"""The QSoC files the `rtl` and `tb` evals are made of, each with a provenance header.

Every file under `evals/rtl/fixtures/` is a file of the public QSoC repository
(`quynhonsemiconductor/vlsi_deep_training`, Apache-2.0), copied byte for byte below a
header that names the source repository, the path in it, the commit and the licence::

    // chipgraph eval fixture (evals/rtl): the source file follows this header, unchanged.
    // source-repo: quynhonsemiconductor/vlsi_deep_training
    // source-path: design/rom/rtl/m_qnsc_rom_wr_resp.sv
    // source-commit: 58d1d91e858036276c387ad240063b72ab96c7e0 (pull request #51)
    // source-blob: 0a5c...  (git's object id of the file at that commit)
    // licence: Apache-2.0
    // chipgraph-fixture-end

The comment marker is the file type's (`//` for SystemVerilog, `#` for a filelist, an
HTML comment per line for Markdown). `strip_header` gives back the source bytes, which is
what an eval project gets. `blob_id` of those bytes must be the header's `source-blob`
(checked without a clone); `check_fixture` also compares them with the file at the
source commit in a QSoC clone.

Command line, from `evals/` (a QSoC clone that has the commits: `git fetch origin
'+refs/pull/*/head:refs/remotes/pr/*'` for the pull-request heads)::

    uv run python -m rtl.fixtures --repo /tmp/qsoc --check   # every fixture vs its commit
    uv run python -m rtl.fixtures --repo /tmp/qsoc --write   # (re)write them (problems.yml)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SOURCE_REPO = "quynhonsemiconductor/vlsi_deep_training"
LICENCE = "Apache-2.0"
END_MARKER = "chipgraph-fixture-end"
FIRST_LINE = "chipgraph eval fixture (evals/rtl): the source file follows this header, unchanged."

_COMMENT: dict[str, tuple[str, str]] = {
    ".sv": ("// ", ""),
    ".svh": ("// ", ""),
    ".v": ("// ", ""),
    ".f": ("# ", ""),
    ".md": ("<!-- ", " -->"),
}
_FIELD_RE = re.compile(r"^(?P<key>[a-z-]+): (?P<value>.+)$")


class FixtureError(ValueError):
    """A fixture without a valid header, or one that differs from its source."""


@dataclass(frozen=True)
class Header:
    """What a fixture's header says."""

    repo: str
    path: str
    commit: str
    pull_request: int | None
    blob: str
    licence: str


def _markers(path: str | Path) -> tuple[str, str]:
    suffix = Path(path).suffix
    if suffix not in _COMMENT:
        raise FixtureError(f"{path}: no header style for {suffix!r} files")
    return _COMMENT[suffix]


def blob_id(text: str) -> str:
    """git's object id of a file with this text (`git hash-object`)."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def make_header(
    fixture_name: str, *, path: str, commit: str, pull_request: int | None, blob: str
) -> str:
    """The header of a fixture copied from `path` at `commit` (`fixture_name` picks the
    comment style by its suffix)."""
    start, end = _markers(fixture_name)
    where = f"{commit} (pull request #{pull_request})" if pull_request else commit
    lines = [
        FIRST_LINE,
        f"source-repo: {SOURCE_REPO}",
        f"source-path: {path}",
        f"source-commit: {where}",
        f"source-blob: {blob}",
        f"licence: {LICENCE}",
        END_MARKER,
    ]
    return "".join(f"{start}{line}{end}\n" for line in lines)


def split(text: str, name: str | Path) -> tuple[Header, str]:
    """(the header, the source text after it) of a fixture's text; `FixtureError` when
    the header is missing or malformed."""
    start, end = _markers(name)
    lines = text.splitlines(keepends=True)
    fields: dict[str, str] = {}
    for index, raw in enumerate(lines):
        line = raw.rstrip("\n")
        if not (line.startswith(start) and line.endswith(end)):
            raise FixtureError(f"{name}: header line {index + 1} is not a {start.strip()} comment")
        body = line[len(start) : len(line) - len(end)] if end else line[len(start) :]
        if index == 0:
            if body != FIRST_LINE:
                raise FixtureError(f"{name}: no chipgraph fixture header")
            continue
        if body == END_MARKER:
            source = "".join(lines[index + 1 :])
            break
        match = _FIELD_RE.match(body)
        if match is None:
            raise FixtureError(f"{name}: malformed header line {index + 1}: {body!r}")
        fields[match["key"]] = match["value"]
    else:
        raise FixtureError(f"{name}: the header has no {END_MARKER!r} line")
    missing = {"source-repo", "source-path", "source-commit", "source-blob", "licence"} - set(
        fields
    )
    if missing:
        raise FixtureError(f"{name}: header misses {', '.join(sorted(missing))}")
    commit, _, rest = fields["source-commit"].partition(" ")
    pr = re.fullmatch(r"\(pull request #(\d+)\)", rest)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or (rest and pr is None):
        raise FixtureError(f"{name}: bad source-commit {fields['source-commit']!r}")
    header = Header(
        repo=fields["source-repo"],
        path=fields["source-path"],
        commit=commit,
        pull_request=int(pr[1]) if pr else None,
        blob=fields["source-blob"],
        licence=fields["licence"],
    )
    return header, source


def read(rel: str) -> tuple[Header, str]:
    """A fixture by its path under `evals/rtl/fixtures/`: its header and source text."""
    path = FIXTURES / rel
    return split(path.read_text(encoding="utf-8"), path)


def strip_header(rel: str) -> str:
    """The source text of a fixture: exactly the file at its source commit."""
    return read(rel)[1]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else ""


def _git_show(repo: Path, commit: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def has_commit(repo: Path, commit: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def check_offline(rel: str) -> Header:
    """`FixtureError` unless fixture `rel` names the QSoC repository and its licence and
    its text is the blob its header names."""
    header, text = read(rel)
    if header.repo != SOURCE_REPO or header.licence != LICENCE:
        raise FixtureError(f"{rel}: source {header.repo} / licence {header.licence}")
    if blob_id(text) != header.blob:
        raise FixtureError(f"{rel}: its text is not blob {header.blob} (edited?)")
    return header


def check_fixture(rel: str, repo: Path) -> None:
    """`FixtureError` unless fixture `rel` is its source file at its commit in `repo`."""
    header, text = read(rel)
    check_offline(rel)
    if not has_commit(repo, header.commit):
        raise FixtureError(f"{rel}: commit {header.commit} is not in {repo}")
    source = _git_show(repo, header.commit, header.path)
    if source is None:
        raise FixtureError(f"{rel}: {header.path} is not in commit {header.commit}")
    if source != text:
        raise FixtureError(f"{rel}: differs from {header.path} at {header.commit}")
    blob = _git(repo, "rev-parse", f"{header.commit}:{header.path}").strip()
    if blob != header.blob:
        raise FixtureError(f"{rel}: {header.path} at {header.commit} is blob {blob}")


def all_fixtures() -> list[str]:
    """Every fixture file, as a path under `evals/rtl/fixtures/`, sorted."""
    return sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file())


def write_fixture(rel: str, repo: Path, *, path: str, commit: str, pr: int | None) -> Path:
    """Copy `path` at `commit` from `repo` to the fixture `rel`, with its header."""
    source = _git_show(repo, commit, path)
    if source is None:
        raise FixtureError(f"{path} is not in commit {commit} of {repo}")
    out = FIXTURES / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    header = make_header(rel, path=path, commit=commit, pull_request=pr, blob=blob_id(source))
    out.write_text(header + source, encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    from .problems import load_problems

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True, help="a QSoC clone")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    data = load_problems()
    if args.write:
        for source in data.sources():
            write_fixture(
                source.fixture, args.repo, path=source.path, commit=source.commit, pr=source.pr
            )
            print(f"wrote {source.fixture}")
        return 0
    failed = 0
    for rel in all_fixtures():
        try:
            check_fixture(rel, args.repo)
            print(f"ok    {rel}")
        except FixtureError as exc:
            failed += 1
            print(f"FAIL  {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
