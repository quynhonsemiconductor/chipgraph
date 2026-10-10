"""`ReviewReport`: a review task's structured reply (DESIGN.md 4.8, 5.1).

A role that writes no files (the Critic) replies with one JSON object, a `ReviewReport`;
the engine checks it against what the role was shown and writes it as the task's one
output (the role's `engine_writes`). What it was shown is a `ReviewScope`: the target,
the base and head refs, the files of the diff and the new-side line ranges of its hunks
(`diff_hunks` reads them from a unified diff).

`review_problems(report, scope)` is the pure validator; it lists every problem at once:

- `target`, `base` or `head` other than the scope's;
- `reviewed` empty, or (with no comments) missing a file of the diff: "no findings" is a
  claim about every file, so it must say it looked at them all;
- a duplicate comment id, an empty claim;
- an empty or unquoted `evidence`: it must quote what the claim rests on, as
  `path:line text` (or `path:start-end text`, or `model:<key> text`);
- a comment on a file not in `reviewed`, or on a `file:line` outside the diff's hunks;
- verdict `approve` with a `blocker` or `major` comment, and `changes_requested` with no
  comment.

`parse_review(raw)` turns the reply (an object or its JSON text) into a `ReviewReport`,
or the reasons it is not one. Generic: no harness, tool or project names.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

Severity = Literal["blocker", "major", "minor", "nit"]
"""How much a comment matters: `blocker` and `major` request changes; `minor` and `nit` do not."""

BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})
"""Severities an `approve` verdict must not carry."""

Verdict = Literal["approve", "changes_requested"]

CommentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
"""A comment id, unique in its review, e.g. `R1`."""

Category = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
"""A comment category, lower_snake_case; the review skill lists the ones to use."""

_EVIDENCE_RE = re.compile(
    r"^\s*(?:(?P<path>[^\s:]+):(?P<line>\d+)(?:-(?P<end>\d+))?|model:(?P<key>\S+))\s+\S"
)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


class ReviewComment(BaseModel):
    """One finding: where it is, what is wrong, what it rests on, what to do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CommentId = Field(description="Unique in the review, e.g. 'R1'.")
    severity: Severity = Field(description="blocker | major (request changes), minor | nit.")
    category: Category = Field(
        description="What kind of problem, lower_snake_case (the review skill lists them)."
    )
    file: str = Field(min_length=1, description="Repo-relative file the comment is on.")
    line: int = Field(ge=1, description="Line in the new version of `file`, inside the diff.")
    claim: str = Field(description="What is wrong, in one or two sentences.")
    evidence: str = Field(
        description=(
            "What the claim rests on, quoted: 'path:line text' (a spec or code line), "
            "'path:start-end text' or 'model:<key> text'. Mandatory."
        )
    )
    suggestion: str = Field(default="", description="What to change, if known.")
    req_id: str | None = Field(
        default=None, description="The requirement id the comment is about, if any."
    )


class ReviewReport(BaseModel):
    """A review task's reply: what was reviewed, the comments, the verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    target: str = Field(min_length=1, description="What was reviewed (the block), as given.")
    base: str = Field(min_length=1, description="The base ref of the diff, as given.")
    head: str = Field(min_length=1, description="The head ref of the diff, as given.")
    reviewed: tuple[str, ...] = Field(
        description="Repo-relative files looked at; every file of the diff when no comments."
    )
    comments: tuple[ReviewComment, ...] = Field(
        default=(), description="The findings; empty means no findings."
    )
    summary: str = Field(description="One short paragraph: what changed and the verdict's reason.")
    verdict: Verdict = Field(description="approve, or changes_requested.")


class ReviewScope(BaseModel):
    """What a review task was shown: the refs and the diff's files and hunks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    target: str = Field(description="What is reviewed (the block).")
    base: str = Field(description="The base ref of the diff.")
    head: str = Field(description="The head ref of the diff.")
    hunks: dict[str, tuple[tuple[int, int], ...]] = Field(
        default_factory=dict,
        description="Each file of the diff to the new-side line ranges of its hunks (inclusive).",
    )

    @property
    def files(self) -> tuple[str, ...]:
        """The files of the diff, sorted."""
        return tuple(sorted(self.hunks))


def diff_hunks(diff: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """The new-side line ranges (inclusive, context lines included) per file of a diff.

    `diff` is unified diff text (`git diff` output). A deleted file has no new side and is
    left out; a hunk that only removes lines covers the line before and after the cut.
    """
    hunks: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    prev = ""
    for line in diff.splitlines():
        header, prev = line.startswith("+++ ") and prev.startswith("--- "), line
        if header:
            new = line[4:].strip()
            if new == "/dev/null":
                current = None
            else:
                current = new[2:] if new.startswith("b/") else new
                hunks.setdefault(current, [])
            continue
        match = _HUNK_RE.match(line)
        if match and current is not None:
            start = int(match.group("start"))
            count = int(match.group("count") or "1")
            if count == 0:
                hunks[current].append((max(start, 1), start + 1))
            else:
                hunks[current].append((start, start + count - 1))
    return {path: tuple(ranges) for path, ranges in hunks.items()}


def _clean_path(path: str) -> bool:
    text = path.strip()
    if not text or text.startswith("/") or "\\" in text:
        return False
    norm = posixpath.normpath(text)
    return norm == text and norm not in (".", "..") and not norm.startswith("../")


def _ranges(ranges: Iterable[tuple[int, int]]) -> str:
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def evidence_ok(evidence: str) -> bool:
    """Whether `evidence` quotes a source: `path:line text`, `path:a-b text`, `model:<key> text`."""
    return bool(_EVIDENCE_RE.match(evidence))


def review_problems(report: ReviewReport, scope: ReviewScope) -> list[str]:
    """Every reason `report` is not a valid review of `scope` (empty: valid)."""
    problems: list[str] = []
    for name in ("target", "base", "head"):
        got, want = getattr(report, name), getattr(scope, name)
        if got != want:
            problems.append(f"{name} {got!r} is not the task's {name} {want!r}")

    reviewed = set(report.reviewed)
    if not report.reviewed:
        problems.append("reviewed is empty: list the files you looked at")
    bad = [p for p in report.reviewed if not _clean_path(p)]
    if bad:
        problems.append(f"reviewed has paths that are not repo-relative: {', '.join(bad)}")
    if not report.comments:
        missing = [f for f in scope.files if f not in reviewed]
        if missing:
            problems.append(
                "a review with no comments says the whole diff is fine, so reviewed must "
                f"list every file of the diff; missing: {', '.join(missing)}"
            )

    seen: set[str] = set()
    for comment in report.comments:
        where = f"comment {comment.id}"
        if comment.id in seen:
            problems.append(f"{where}: duplicate comment id {comment.id!r}")
        seen.add(comment.id)
        if not comment.claim.strip():
            problems.append(f"{where}: claim is empty")
        if not comment.evidence.strip():
            problems.append(
                f"{where}: evidence is empty; quote the spec line or code the claim rests "
                "on, as 'path:line text'"
            )
        elif not evidence_ok(comment.evidence):
            problems.append(
                f"{where}: evidence {comment.evidence[:80]!r} does not quote a source; "
                "write 'path:line text' (or 'path:start-end text', 'model:<key> text')"
            )
        if comment.file not in reviewed:
            problems.append(f"{where}: file {comment.file!r} is not in reviewed")
        ranges = scope.hunks.get(comment.file)
        if ranges is None:
            problems.append(
                f"{where}: {comment.file}:{comment.line} is outside the diff "
                f"({comment.file!r} is not one of its files: {', '.join(scope.files) or 'none'})"
            )
        elif not any(a <= comment.line <= b for a, b in ranges):
            problems.append(
                f"{where}: {comment.file}:{comment.line} is outside the diff (its hunks "
                f"cover lines {_ranges(ranges)} of {comment.file})"
            )

    blocking = [c.id for c in report.comments if c.severity in BLOCKING_SEVERITIES]
    if report.verdict == "approve" and blocking:
        problems.append(
            f"verdict 'approve' with blocker or major comments ({', '.join(blocking)}); "
            "use 'changes_requested' or lower their severity"
        )
    if report.verdict == "changes_requested" and not report.comments:
        problems.append("verdict 'changes_requested' with no comments; say what to change")
    return problems


def _validation_lines(exc: ValidationError) -> list[str]:
    lines = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error.get("loc", ())) or "review"
        lines.append(f"review.{loc}: {error.get('msg', 'invalid')}")
    return lines


def parse_review(raw: Mapping[str, Any] | str) -> tuple[ReviewReport | None, list[str]]:
    """The reply as a `ReviewReport`, or `None` and why it is not one.

    `raw` is the reply object, or its JSON text (a fenced ```json block is unwrapped).
    """
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n(?P<body>.*)\n```", text, flags=re.DOTALL)
        if fenced:
            text = fenced.group("body")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, [f"review is not valid JSON: {exc}"]
    if not isinstance(data, Mapping):
        return None, [f"review must be a JSON object, not {type(data).__name__}"]
    try:
        return ReviewReport.model_validate(dict(data)), []
    except ValidationError as exc:
        return None, _validation_lines(exc)


__all__ = [
    "BLOCKING_SEVERITIES",
    "Category",
    "CommentId",
    "ReviewComment",
    "ReviewReport",
    "ReviewScope",
    "Severity",
    "Verdict",
    "diff_hunks",
    "evidence_ok",
    "parse_review",
    "review_problems",
]
