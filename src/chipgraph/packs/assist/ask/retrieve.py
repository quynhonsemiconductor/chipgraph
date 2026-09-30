"""`ask_context`: the sources an `/ask` answer may use (DESIGN 4.5: typed queries, no grep).

Two kinds of retrieval, merged:

1. **Typed lookups** (`ModelQuery`): a word of the question that names something the model
   knows (a block, module, register, `REG.FIELD`, port, requirement id, clock, reset, ...)
   gives that entity, with its facts and relations; a kind word (`register`, `interrupt`,
   `open items`, `memory map`, ...) lists the entities of that kind in the blocks the
   question is about. A port named `clk` exists in every module, so an entity in a block
   the question names ranks above one in another block.
2. **Full-text search** (FTS5 over entities and documents) with a safe query built from
   the question (`fts.fts_query`). A document hit cites `path:line` and carries the lines
   around it.

Entities and documents from `nda` files are never returned. An empty result is a valid
answer: `no_sources` is then true and the answer must be "I don't know".
"""

from __future__ import annotations

import re
import sqlite3

from chipgraph.app.context import AppContext
from chipgraph.core.model.entities import EntityBase
from chipgraph.packs.assist.ask._project import AskProject
from chipgraph.packs.assist.ask.contract import AskContext, AskSource, DocLine
from chipgraph.packs.assist.ask.fts import STOP_WORDS, fts_query

DEFAULT_LIMIT = 12
MAX_LIMIT = 50
CONTEXT_LINES = 2
"""Lines shown before and after a document hit."""

_WORD = re.compile(r"\w+(?:[-.]\w+)*")

_KIND_PRIORITY: dict[str, int] = {
    "requirement": 10,
    "register": 9,
    "field": 8,
    "interrupt": 8,
    "memory_region": 7,
    "block": 6,
    "module": 6,
    "port": 5,
    "clock": 5,
    "reset": 5,
    "parameter": 4,
    "interface": 4,
    "open_item": 4,
    "project": 3,
}

_KIND_WORDS: dict[str, str] = {
    "register": "register",
    "registers": "register",
    "field": "field",
    "fields": "field",
    "bitfield": "field",
    "bitfields": "field",
    "requirement": "requirement",
    "requirements": "requirement",
    "interrupt": "interrupt",
    "interrupts": "interrupt",
    "port": "port",
    "ports": "port",
    "signal": "port",
    "signals": "port",
    "module": "module",
    "modules": "module",
    "parameter": "parameter",
    "parameters": "parameter",
    "clock": "clock",
    "clocks": "clock",
    "reset": "reset",
    "resets": "reset",
    "block": "block",
    "blocks": "block",
    "bus": "interface",
    "buses": "interface",
    "interface": "interface",
    "interfaces": "interface",
    "address": "memory_region",
    "addresses": "memory_region",
}
_PHRASE_KINDS: tuple[tuple[str, str], ...] = (
    ("open item", "open_item"),
    ("open question", "open_item"),
    ("memory map", "memory_region"),
    ("address map", "memory_region"),
)

_GENERIC_WORDS = frozenset(
    {
        "access", "address", "base", "block", "clock", "config", "control", "count", "data",
        "enable", "field", "interrupt", "line", "mode", "module", "offset", "port", "register",
        "reset", "size", "status", "type", "value", "width",
    }
)  # fmt: skip
"""Words that are also common register or field names: they match an entity only when
written exactly as its name."""

_DIRECT_BONUS = 10
_KIND_WORD_BONUS = 6
_CONTEXT_BONUS = 5
_OFF_CONTEXT_PENALTY = 6
_MAX_PER_KIND_LISTING = 8


def ask_context(ctx: AppContext, question: str, limit: int = DEFAULT_LIMIT) -> AskContext:
    """The sources for `question` in the project of `ctx`, best first, at most `limit`."""
    project = AskProject.load(ctx)
    return retrieve(project, question, limit)


def retrieve(project: AskProject, question: str, limit: int = DEFAULT_LIMIT) -> AskContext:
    """`ask_context` on an already loaded `AskProject`."""
    limit = max(1, min(limit, MAX_LIMIT))
    typed = _typed_sources(project, question, limit)
    query = fts_query(question)
    searched = _search_sources(project, query, limit) if query is not None else []

    sources: list[AskSource] = []
    seen: set[str] = set()

    def take(items: list[AskSource], cap: int) -> None:
        for source in items:
            if len(sources) >= cap:
                return
            if source.citation not in seen:
                seen.add(source.citation)
                sources.append(source)

    take(typed, max(1, (limit + 1) // 2))
    take(searched, limit)
    take(typed, limit)

    no_sources = not sources
    note = (
        "No source matches the question: answer that you do not know (unknown: true)."
        if no_sources
        else "Answer only from these sources. Cite every claim with a source's `citation` "
        "(or its `defined_at`) exactly as given. If they do not answer the question, set "
        "unknown: true and say what is missing. Do not use outside knowledge."
    )
    return AskContext(
        question=question,
        query=query,
        sources=tuple(sources),
        no_sources=no_sources,
        note=note,
    )


# --- typed lookups ----------------------------------------------------------------------


def _typed_sources(project: AskProject, question: str, limit: int) -> list[AskSource]:
    """Typed lookups on the names and kind words of the question, best first.

    Score = kind priority, + `_DIRECT_BONUS` for an entity the question names, +
    `_KIND_WORD_BONUS` when its kind is named too (or it is listed for a kind word in the
    blocks the question is about), +/- the block context. An entity that only shares its
    name with a named block (the `timer` memory region when the question says "timer")
    is not "named" unless its kind is.
    """
    words = [m.group(0).strip("-.") for m in _WORD.finditer(question)]
    words = [w for w in words if len(w) >= 2]
    kinds = _kinds_named(words)
    names: dict[str, list[EntityBase]] = {}
    for entity in project.model.entities.values():
        if entity.kind in _KIND_PRIORITY and project.visible(entity):
            names.setdefault(entity.name.lower(), []).append(entity)

    direct: dict[str, EntityBase] = {}
    for word in words:
        lowered = word.lower()
        if lowered in STOP_WORDS:
            continue
        for entity in names.get(lowered, ()):
            # A generic word ("reset value", "status") names an entity only when it is
            # written exactly as that entity's name (`RESET`, `STATUS`).
            if lowered in _GENERIC_WORDS and word != entity.name:
                continue
            direct.setdefault(entity.key, entity)
        if "." in word:
            for entity in _dotted_fields(project, word):
                direct.setdefault(entity.key, entity)
    block_names = {e.name.lower() for e in direct.values() if e.kind == "block"}
    context_blocks, context_modules = _context(project, list(direct.values()))

    # A kind word lists that kind in the blocks the question is about; with no such
    # block, only when the whole project has a few of them (its one clock, its resets).
    listed: dict[str, EntityBase] = {}
    for kind in kinds:
        members = [
            e
            for e in sorted(project.model.by_kind(kind), key=lambda e: e.key)
            if project.visible(e) and (not context_blocks or project.blocks_of(e) & context_blocks)
        ]
        if context_blocks or len(members) <= _MAX_PER_KIND_LISTING:
            for entity in members[:_MAX_PER_KIND_LISTING]:
                listed.setdefault(entity.key, entity)

    scored: list[tuple[int, str, EntityBase]] = []
    for key, entity in {**listed, **direct}.items():
        kind_named = entity.kind in kinds
        named = key in direct and (
            entity.kind == "block" or entity.name.lower() not in block_names or kind_named
        )
        score = _KIND_PRIORITY.get(entity.kind, 1)
        if named:
            score += _DIRECT_BONUS
        if kind_named and (named or context_blocks):
            score += _KIND_WORD_BONUS
        score += _context_adjustment(project, entity, context_blocks, context_modules)
        scored.append((score, key, entity))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [_model_source(project, entity, "lookup") for _, _, entity in scored[:limit]]


def _dotted_fields(project: AskProject, word: str) -> list[EntityBase]:
    """`CTRL.EN` (register.field) or `timer.CTRL` (block.register) as model entities."""
    parts = word.lower().split(".")
    found = []
    for entity in project.model.entities.values():
        if entity.kind not in ("field", "register") or not project.visible(entity):
            continue
        key_parts = entity.key.partition(":")[2].lower().split(".")
        if key_parts[-len(parts) :] == parts:
            found.append(entity)
    return found


def _context(project: AskProject, direct: list[EntityBase]) -> tuple[set[str], set[str]]:
    """The blocks and modules the question is about.

    The blocks and modules it names; if it names none, the blocks of the registers,
    fields, requirements and memory regions it names.
    """
    modules = {e.key for e in direct if e.kind == "module"}
    blocks: set[str] = set()
    for entity in direct:
        if entity.kind in ("block", "module"):
            blocks |= project.blocks_of(entity)
    if not blocks:
        for entity in direct:
            if entity.kind in ("register", "field", "requirement", "memory_region"):
                blocks |= project.blocks_of(entity)
    return blocks, modules


def _context_adjustment(
    project: AskProject,
    entity: EntityBase,
    context_blocks: set[str],
    context_modules: set[str],
) -> int:
    if entity.kind in ("block", "project"):
        return 0
    module = project.module_of(entity)
    blocks = project.blocks_of(entity)
    if context_modules and module is not None:
        in_context = module in context_modules
    elif context_blocks and blocks:
        in_context = bool(blocks & context_blocks)
    else:
        return 0
    return _CONTEXT_BONUS if in_context else -_OFF_CONTEXT_PENALTY


def _kinds_named(words: list[str]) -> list[str]:
    """The entity kinds the question names ("registers", "open items", "memory map")."""
    lowered = [w.lower() for w in words]
    kinds: list[str] = []
    for index, word in enumerate(lowered):
        following = lowered[index + 1] if index + 1 < len(lowered) else ""
        pair = f"{word} {following}"
        named = next((k for p, k in _PHRASE_KINDS if pair.startswith(p)), None)
        if named is None:
            if word == "reset" and following.startswith("value"):
                continue  # "reset value" asks about a register, not about a reset
            named = _KIND_WORDS.get(word)
        if named is not None and named not in kinds:
            kinds.append(named)
    return kinds


def _model_source(project: AskProject, entity: EntityBase, origin: str) -> AskSource:
    return AskSource(
        citation=f"model:{entity.key}",
        kind="model",
        text=project.summary(entity),
        origin=origin,  # type: ignore[arg-type]
        defined_at=project.defined_at(entity),
    )


# --- full-text search -------------------------------------------------------------------


def _search_sources(project: AskProject, query: str, limit: int) -> list[AskSource]:
    try:
        hits = project.store.search(query, limit=limit * 2)
    except sqlite3.Error:
        return []  # a query the FTS5 parser still refuses: no search result, never an error
    sources: list[AskSource] = []
    for hit in hits:
        if hit.key is not None:
            entity = project.get(hit.key)
            if entity is not None:
                sources.append(_model_source(project, entity, "search"))
        elif hit.file is not None and hit.line is not None:
            if project.labels.label_for(hit.file) == "nda":
                continue
            sources.append(_document_source(project, hit.file, hit.line, hit.text))
    return sources


def _document_source(project: AskProject, path: str, line: int, text: str) -> AskSource:
    around = project.docs.lines(path, max(1, line - CONTEXT_LINES), line + CONTEXT_LINES)
    return AskSource(
        citation=f"{path}:{line}",
        kind="document",
        text=text,
        origin="search",
        context=tuple(DocLine(line=n, text=t) for n, t in around if n != line),
    )


__all__ = ["CONTEXT_LINES", "DEFAULT_LIMIT", "MAX_LIMIT", "ask_context", "retrieve"]
