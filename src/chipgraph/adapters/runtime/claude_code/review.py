"""The context of a review task, and the reply it is checked against (DESIGN.md 4.8, 5.1).

A task whose role writes no files but has `engine_writes` (the Critic) reviews a change.
`get_context` gives it, with a fresh context (no conversation, no author reasoning, no
earlier review), a `review` section built here:

- `diff`: the target's files, base ref vs the working tree (tracked changes and new
  untracked files), each line led by its number in the new file (blank for a removed
  line), capped at `DIFF_CAP` characters with a note naming the files left out;
- `spec`: the target block's requirements, interface, registers and open items from
  the Design Model, each with its `file:line`;
- `model`: the block's Design Model slice (`model.block(name)`);
- `base`, `head`, `target`: what the reply must name.

The base ref is the instance param `base` when the rule's instance has one; otherwise the
merge-base of HEAD with the default branch (`origin/HEAD`, else `main`, else `master`),
which is HEAD itself on the default branch (the diff is then the uncommitted change). The
target's files are the paths of the build graph's other instances for the block, the
Design Model's module files and spec sources for it, and the profile's layout paths for
it; the review rule's own outputs are never among them.

What the task was shown is kept as a `ReviewScope` under the state directory
(`<state>/runtime/reviews/`), per dispatch, so `submit` checks the reply against exactly
that diff (`load_scope`).
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any

from chipgraph.adapters.tool.filelist import FilelistError, read_filelist
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts import RuleInstance, RuleSpec
from chipgraph.core.engine.graph import BuildGraph
from chipgraph.core.model import ModelQuery, ModelStore, QueryError, default_model_db_path
from chipgraph.core.runtime.queue import state_key
from chipgraph.core.runtime.roles import ReviewReport, ReviewScope, diff_hunks

DIFF_CAP = 60_000
"""Longest diff text `get_context` returns; the files after the cut are named instead."""

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""git's empty tree: the base of a repo with no commit yet."""

_DEFAULT_BRANCHES = ("main", "master")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_GLOB_CHARS = frozenset("*?[")
_SPEC_KINDS = ("requirement", "register", "field", "port", "open_item")


def is_review_role(role: Any) -> bool:
    """Whether a role replies with a review the engine writes (`engine_writes`)."""
    return role is not None and role.write_scope == "none" and bool(role.engine_writes)


# --- git ----------------------------------------------------------------------------------


async def _git(ctx: AppContext, *args: str, ok: tuple[int, ...] = (0,)) -> str | None:
    """`git <args>` in the project; its stdout, or `None` when it exits with another code."""
    runner = ctx.registry.get("runner", "local")
    result = await runner.run(["git", "-c", "core.quotePath=false", *args], cwd=ctx.root)
    return result.stdout if result.returncode in ok else None


async def _default_branch(ctx: AppContext) -> str | None:
    remote = await _git(ctx, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if remote and remote.strip():
        return remote.strip().removeprefix("refs/remotes/")
    for name in _DEFAULT_BRANCHES:
        if await _git(ctx, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is not None:
            return name
    return None


async def resolve_refs(ctx: AppContext, instance: RuleInstance) -> tuple[str, str, str]:
    """`(base, how, head)`: the base commit, how it was chosen, and the HEAD commit."""
    head_out = await _git(ctx, "rev-parse", "--verify", "--quiet", "HEAD")
    head = head_out.strip() if head_out else ""
    param = instance.params.get("base")
    if param:
        found = await _git(ctx, "rev-parse", "--verify", "--quiet", f"{param}^{{commit}}")
        if not found:
            raise AppError(f"task {instance.instance_id!r}: base ref {param!r} is not a commit")
        return found.strip(), f"rule param base={param}", head or EMPTY_TREE
    if not head:
        return EMPTY_TREE, "no commit yet: the empty tree", EMPTY_TREE
    branch = await _default_branch(ctx)
    if branch is None:
        return head, "HEAD (no default branch found)", head
    base = await _git(ctx, "merge-base", "HEAD", branch)
    if not base:
        return head, f"HEAD (no merge-base with {branch})", head
    return base.strip(), f"merge-base of HEAD and {branch}", head


# --- the target's files -------------------------------------------------------------------


def _clean(path: str | None) -> str | None:
    if not path:
        return None
    text = path.replace("\\", "/").strip()
    if not text or text.startswith("/") or _GLOB_CHARS & set(text):
        return None
    norm = posixpath.normpath(text)
    if norm in (".", "..") or norm.startswith("../"):
        return None
    return norm


def _fill(template: str, block: str) -> str | None:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "block":
            return block
        if key == "BLOCK":
            return block.upper()
        return "*"

    return _clean(_PLACEHOLDER_RE.sub(_replace, template))


def _model(ctx: AppContext) -> tuple[Any, Any] | None:
    db = default_model_db_path(ctx.layout.root)
    if not db.is_file():
        return None
    store = ModelStore(db)
    return store.read(), store


def _entity_block(entity: Any) -> str | None:
    block = getattr(entity, "block", None)
    if isinstance(block, str):
        return block
    attr = entity.attrs.get("block")
    return attr if isinstance(attr, str) else None


def target_files(
    ctx: AppContext, graph: BuildGraph, rule: RuleSpec, instance: RuleInstance
) -> list[str]:
    """The repo-relative files a review of `instance` covers, sorted."""
    own = {
        ref.path
        for inst in graph.instances.values()
        if inst.rule_id == rule.id
        for ref in inst.outputs
        if ref.path is not None
    }
    paths: set[str | None] = {ref.path for ref in instance.inputs}
    block = instance.params.get("block")
    if block:
        for inst in graph.instances.values():
            if inst.rule_id != rule.id and inst.params.get("block") == block:
                paths.update(ref.path for ref in (*inst.inputs, *inst.outputs))
        profile = ctx.require_profile().profile
        layout: dict[str, Any] = dict(profile.layout)
        override = profile.blocks.get(block)
        if override is not None:
            layout.update(override.layout)
        for value in layout.values():
            for template in (value,) if isinstance(value, str) else value:
                paths.add(_fill(template, block))
        loaded = _model(ctx)
        if loaded is not None:
            model, _ = loaded
            key = f"block:{block}"
            modules = set()
            for entity in model.by_kind("module"):
                if getattr(entity, "block", None) == key:
                    modules.add(entity.key)
                    paths.add(getattr(entity, "file", None))
            for kind in _SPEC_KINDS:
                for entity in model.by_kind(kind):
                    if _entity_block(entity) == key or getattr(entity, "module", None) in modules:
                        paths.add(entity.source.file)
    cleaned = {c for c in (_clean(p) for p in paths) if c is not None}
    cleaned |= _filelist_sources(ctx.root, sorted(p for p in cleaned if p.endswith(".f")))
    return sorted(cleaned - own)


def _filelist_sources(root: Path, filelists: list[str]) -> set[str]:
    """The repo files the filelists among the target's files list (new files too).

    Paths are tried both relative to the filelist and to the project root, as ingest does;
    only those inside the project are kept.
    """
    found: set[str] = set()
    top = root.resolve()
    for rel in filelists:
        path = root / rel
        if not path.is_file():
            continue
        for style in ("filelist", "cwd"):
            try:
                listed = read_filelist(path, relative_to=style, root=root)
            except FilelistError:
                continue
            for source in listed.sources:
                if not source.is_file():
                    continue
                try:
                    found.add(source.resolve().relative_to(top).as_posix())
                except ValueError:
                    continue
    return {c for c in (_clean(p) for p in found) if c is not None}


# --- the diff -----------------------------------------------------------------------------


async def full_diff(ctx: AppContext, base: str, files: list[str]) -> str:
    """Unified diff of `files`, `base` vs the working tree, new untracked files included."""
    if not files:
        return ""
    tracked = await _git(
        ctx, "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U3", base, "--", *files
    )
    parts = [tracked or ""]
    untracked = await _git(ctx, "ls-files", "-z", "--others", "--exclude-standard", "--", *files)
    for path in sorted(p for p in (untracked or "").split("\0") if p):
        added = await _git(
            ctx, "diff", "--no-color", "--no-index", "--", "/dev/null", path, ok=(0, 1)
        )
        parts.append(added or "")
    return "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts)


def _sections(diff: str) -> list[tuple[str, list[str]]]:
    """The diff split per file: (path, lines), in order."""
    sections: list[tuple[str, list[str]]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git ") or not sections:
            sections.append(("", []))
        name, lines = sections[-1]
        lines.append(line)
        if line.startswith("+++ ") and not name:
            new = line[4:].strip()
            sections[-1] = (new[2:] if new.startswith("b/") else new, lines)
        elif line.startswith("--- ") and not name:
            old = line[4:].strip()
            if old != "/dev/null":
                sections[-1] = (old[2:] if old.startswith("a/") else old, lines)
    return sections


def annotate(diff: str) -> list[tuple[str, str]]:
    """Per file: (path, text), each hunk line led by its line number in the new file."""
    out: list[tuple[str, str]] = []
    for path, lines in _sections(diff):
        shown: list[str] = []
        new_line = 0
        in_hunk = False
        for line in lines:
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                new_line = int(match.group(1))
                in_hunk = True
                shown.append(line)
            elif in_hunk and line[:1] in ("+", " "):
                shown.append(f"{new_line:>5} {line}")
                new_line += 1
            elif in_hunk and line[:1] == "-":
                shown.append(f"{'':>5} {line}")
            else:
                if line.startswith("diff --git "):
                    in_hunk = False
                shown.append(line)
        out.append((path, "\n".join(shown) + "\n"))
    return out


def capped(sections: list[tuple[str, str]], cap: int | None = None) -> tuple[str, list[str]]:
    """The annotated diff within `cap` (default `DIFF_CAP`) characters, and the files left
    out (or cut)."""
    cap = DIFF_CAP if cap is None else cap
    text = ""
    omitted: list[str] = []
    for path, body in sections:
        if omitted or len(text) + len(body) > cap:
            if not text:
                text = body[:cap]
            omitted.append(path)
            continue
        text += body
    return text, omitted


# --- the spec and model slices ------------------------------------------------------------


def _where(entity: Any) -> str | None:
    src = entity.source
    if not src.file:
        return None
    return f"{src.file}:{src.line}" if src.line else src.file


def spec_slice(ctx: AppContext, block: str | None) -> dict[str, Any]:
    """The block's requirements, interface, registers and open items, from the model."""
    if not block:
        return {"note": "the task has no block param: no spec slice"}
    loaded = _model(ctx)
    if loaded is None:
        return {"note": "no Design Model (run `chipgraph ingest` first)"}
    model, _ = loaded
    key = f"block:{block}"
    modules = {e.key for e in model.by_kind("module") if getattr(e, "block", None) == key}
    requirements = [
        {"id": e.name, "text": getattr(e, "text", None), "source": _where(e)}
        for e in model.by_kind("requirement")
        if _entity_block(e) == key
    ]
    interface = [
        {
            "name": e.name,
            "direction": e.direction,
            "width": e.width,
            "description": e.attrs.get("description"),
            "source": _where(e),
        }
        for e in model.by_kind("port")
        if _entity_block(e) == key and e.attrs.get("origin") == "spec"
    ]
    rtl_ports = [
        {
            "module": e.module,
            "name": e.name,
            "direction": e.direction,
            "width": e.width,
            "source": _where(e),
        }
        for e in model.by_kind("port")
        if e.module in modules
    ]
    fields: dict[str, list[dict[str, Any]]] = {}
    for e in model.by_kind("field"):
        fields.setdefault(str(getattr(e, "register", "")), []).append(
            {
                "name": e.name,
                "msb": e.msb,
                "lsb": e.lsb,
                "access": e.access,
                "reset_value": e.reset_value,
                "source": _where(e),
            }
        )
    registers = [
        {
            "name": e.name,
            "offset": e.offset,
            "access": e.access,
            "reset_value": e.reset_value,
            "source": _where(e),
            "fields": fields.get(e.key, []),
        }
        for e in model.by_kind("register")
        if getattr(e, "block", None) == key
    ]
    open_items = [
        {"name": e.name, "status": getattr(e, "status", None), "source": _where(e)}
        for e in model.by_kind("open_item")
        if _entity_block(e) == key
    ]
    return {
        "requirements": sorted(requirements, key=lambda r: str(r["id"])),
        "interface": interface,
        "rtl_ports": rtl_ports,
        "registers": registers,
        "open_items": open_items,
    }


def model_slice(ctx: AppContext, block: str | None) -> dict[str, Any]:
    """`model.block(name)` as JSON, or a note."""
    loaded = _model(ctx) if block else None
    if loaded is None:
        return {"note": "not available from the Design Model"}
    model, store = loaded
    try:
        info = ModelQuery(model, store).block(str(block))
    except QueryError as exc:
        return {"note": str(exc)}
    result: dict[str, Any] = info.model_dump(mode="json")
    return result


# --- the scope, kept per dispatch ---------------------------------------------------------


def _scope_path(ctx: AppContext, task_id: str) -> Path:
    return ctx.layout.state_dir / "runtime" / "reviews" / f"{state_key(task_id)}.json"


def save_scope(ctx: AppContext, task_id: str, dispatch: int, scope: ReviewScope) -> None:
    path = _scope_path(ctx, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"task_id": task_id, "dispatch": dispatch, "scope": scope.model_dump(mode="json")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_scope(ctx: AppContext, task_id: str, dispatch: int) -> ReviewScope | None:
    """The scope `get_context` showed this dispatch of the task, if it did."""
    path = _scope_path(ctx, task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("task_id") != task_id or data.get("dispatch") != dispatch:
        return None
    return ReviewScope.model_validate(data["scope"])


async def _scope_and_diff(
    ctx: AppContext, graph: BuildGraph, rule: RuleSpec, instance: RuleInstance
) -> tuple[ReviewScope, str, str, list[str]]:
    base, how, head = await resolve_refs(ctx, instance)
    files = target_files(ctx, graph, rule, instance)
    diff = await full_diff(ctx, base, files)
    target = instance.params.get("block") or instance.instance_id
    scope = ReviewScope(target=target, base=base, head=head, hunks=diff_hunks(diff))
    return scope, diff, how, files


async def review_scope(
    ctx: AppContext, graph: BuildGraph, rule: RuleSpec, instance: RuleInstance
) -> ReviewScope:
    """The scope of a review task as it is now (when `get_context` kept none)."""
    scope, _, _, _ = await _scope_and_diff(ctx, graph, rule, instance)
    return scope


async def build_review(
    ctx: AppContext, graph: BuildGraph, rule: RuleSpec, instance: RuleInstance
) -> tuple[ReviewScope, dict[str, Any]]:
    """The scope of a review task and the `review` section of its context."""
    scope, diff, how, files = await _scope_and_diff(ctx, graph, rule, instance)
    base, head = scope.base, scope.head
    block = instance.params.get("block")
    text, omitted = capped(annotate(diff))
    note = (
        "Each hunk line starts with its line number in the new file (blank for a removed "
        "line); comment on those numbers. The diff is the base vs the working tree."
    )
    if omitted:
        note += (
            f" The diff was cut at {DIFF_CAP} characters; not shown (read them with your "
            f"read tools): {', '.join(omitted)}."
        )
    if not scope.hunks:
        note += " The diff is empty: nothing of this target changed since the base."
    view: dict[str, Any] = {
        "target": scope.target,
        "base": base,
        "base_from": how,
        "head": head,
        "files": files,
        "changed": list(scope.files),
        "diff": text,
        "truncated": bool(omitted),
        "omitted": omitted,
        "note": note,
        "spec": spec_slice(ctx, block),
        "model": model_slice(ctx, block),
        "reply_schema": ReviewReport.model_json_schema(),
    }
    return scope, view


__all__ = [
    "DIFF_CAP",
    "EMPTY_TREE",
    "annotate",
    "build_review",
    "capped",
    "full_diff",
    "is_review_role",
    "load_scope",
    "resolve_refs",
    "review_scope",
    "save_scope",
    "spec_slice",
    "target_files",
]
