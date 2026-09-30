"""`/ask` (task M1-13): questions about the project, answered only with checked citations.

- `ask_context(ctx, question, limit)` (`retrieve`): the sources an answer may use, from
  typed Design Model lookups (`ModelQuery`) and a safe FTS5 search over entities and the
  indexed documents (`fts`). Nothing from an `nda` file.
- `ask_check(ctx, answer)` (`check`): every citation of an `AskAnswer` must name an indexed
  `path:line` or a `model:<key>`; an answer that is not `unknown` needs at least one.
- `run_ask` / `answer_question` (`answer`): the API-runtime answerer behind `chipgraph
  ask`, with the same contract and checker. In Claude Code, `/chipgraph:ask` runs the
  `chipgraph:asker` subagent over the `ask_context` and `ask_check` MCP tools instead.
- `index_documents` (`documents`): the document set `chipgraph ingest` indexes after it
  writes the model store (which drops earlier documents).
"""

from chipgraph.packs.assist.ask.answer import (
    AskResult,
    answer_question,
    make_provider,
    parse_answer,
    run_ask,
)
from chipgraph.packs.assist.ask.check import ask_check, check_answer, check_citation
from chipgraph.packs.assist.ask.contract import (
    UNKNOWN_ANSWER,
    AskAnswer,
    AskCheck,
    AskContext,
    AskSource,
    CitationCheck,
    DocLine,
)
from chipgraph.packs.assist.ask.documents import DocumentIndexReport, index_documents
from chipgraph.packs.assist.ask.fts import fts_query, question_terms
from chipgraph.packs.assist.ask.retrieve import ask_context, retrieve

__all__ = [
    "UNKNOWN_ANSWER",
    "AskAnswer",
    "AskCheck",
    "AskContext",
    "AskResult",
    "AskSource",
    "CitationCheck",
    "DocLine",
    "DocumentIndexReport",
    "answer_question",
    "ask_check",
    "ask_context",
    "check_answer",
    "check_citation",
    "fts_query",
    "index_documents",
    "make_provider",
    "parse_answer",
    "question_terms",
    "retrieve",
    "run_ask",
]
