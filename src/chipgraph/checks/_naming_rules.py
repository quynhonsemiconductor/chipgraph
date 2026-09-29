"""The `naming` check's rules, as a versioned pydantic data model.

A naming rule file (e.g. `src/chipgraph/orgs/qnsc/naming-v1.yml`) declares, as data:

- per **object kind** (`module`, `port`, `parameter`, `localparam`, `enum_value`,
  `instance`, `signal`, `memory`, `genvar`, ...) an allowed `pattern`, a `rule` id and a
  `message`;
- **lexical** rules that apply to every identifier declared in a file (a forbidden
  regex with an optional allow-if escape hatch);
- a **vocabulary** map of forbidden word -> project term;
- the `ignore_comment` that exempts a declaration line.

The check (`chipgraph.checks.naming`) reads this model; it does not hard-code any
pattern. Rule ids and messages follow the source document's numbering ("2.1 module",
"1.5 vocabulary", ...) so a reviewer can compare the file with the document line by line.

The JSON Schema of this model is committed at `schemas/formats/naming-rules.schema.json`
(regenerate with `python -m chipgraph.checks.naming`).
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The object kinds a rule file may constrain. These are the classifications the check
# derives from the pyslang syntax tree, not free-form strings.
ObjectKind = Literal[
    "module",
    "port",
    "parameter",
    "localparam",
    "enum_value",
    "instance",
    "signal",
    "memory",
    "genvar",
]


def _valid_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:  # pragma: no cover - exercised via model validation
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return pattern


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KindRule(_Model):
    """The allowed-name rule for one object kind: a pattern the whole name must match."""

    rule: str = Field(description="The rule id, e.g. '2.1 module'.")
    pattern: str = Field(description="A regex the identifier must fully match to pass.")
    message: str = Field(description="Human-readable message shown when the name is rejected.")

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, value: str) -> str:
        return _valid_regex(value)


class LexicalRule(_Model):
    """A lexical rule: a forbidden pattern with an optional allow-if escape hatch."""

    rule: str = Field(description="The rule id, e.g. '1.1 case'.")
    forbid_pattern: str = Field(description="A regex that, if found in a name, is a violation.")
    allow_if_matches: str | None = Field(
        default=None,
        description="If set, a name fully matching this regex is exempt from this rule.",
    )
    message: str = Field(description="Human-readable message shown when the rule fires.")

    @field_validator("forbid_pattern", "allow_if_matches")
    @classmethod
    def _check_pattern(cls, value: str | None) -> str | None:
        return _valid_regex(value) if value is not None else None


class VocabularyRule(_Model):
    """The mandatory-vocabulary rule: each forbidden word maps to the project term."""

    rule: str = Field(description="The rule id, e.g. '1.5 vocabulary'.")
    replace: dict[str, str] = Field(description="Map of forbidden word -> preferred project term.")
    message: str = Field(description="Human-readable message prefix shown when a word is used.")


class NamingRules(_Model):
    """A naming rule file: per-kind patterns, lexical rules, vocabulary and metadata."""

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    document: str = Field(description="Name of the source naming-rule document.")
    version: str = Field(description="Version of the source document these rules enforce.")
    ignore_comment: str = Field(
        default="naming-check: ignore",
        description="A trailing line comment matching this exempts the declaration line.",
    )
    identifiers: dict[ObjectKind, KindRule] = Field(
        description="Allowed-name rule per object kind (module, port, signal, ...)."
    )
    lexical: tuple[LexicalRule, ...] = Field(
        default=(), description="Lexical rules applied to every identifier declared in a file."
    )
    vocabulary: VocabularyRule | None = Field(
        default=None, description="The mandatory-vocabulary rule, if any."
    )


def schema_json() -> str:
    """The JSON Schema of a naming rule file, as committed under `schemas/formats/`."""
    return json.dumps(NamingRules.model_json_schema(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "KindRule",
    "LexicalRule",
    "NamingRules",
    "ObjectKind",
    "VocabularyRule",
    "schema_json",
]
