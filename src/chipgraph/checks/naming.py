"""`NamingCheck`: identifiers follow the project's naming rule, classified by pyslang.

The rule is data (`org:qnsc/naming-v1.yml`, a `chipgraph.checks._naming_rules.NamingRules`),
not code: per object kind an allowed pattern, plus lexical and vocabulary rules. The
check parses each in-scope SystemVerilog file into a pyslang **syntax tree** (no
elaboration, so a single file with missing packages still parses), classifies every
identifier the file *declares* by its syntax node kind (module, port, parameter vs
localparam, enum member, instance, signal vs memory, genvar), and checks each name
against the rule for its kind and the lexical/vocabulary rules. A regex over text cannot
tell a port from a signal; pyslang can, so this is more precise than the reference
text checker it is modelled on.

Example profile usage (`.chipgraph.yml`):

```yaml
adapters:
  naming: { use: naming, rules: "org:qnsc/naming-v1.yml", scope: ["design/{block}/rtl/**/*.sv"] }
```

`{block}` in a scope glob is substituted from `ctx.params["block"]`. `ignore` globs
(default: any path with a `vendor` segment) drop files that keep their upstream's
conventions. A per-declaration exemption comment (`// naming-check: ignore`) on the
declaration line suppresses that line. A parse error becomes an `error` issue for the
file, never an exception; a missing rules file or bad rules ref is a whole-check `error`.

Regenerate `schemas/formats/naming-rules.schema.json` with `python -m chipgraph.checks.naming`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chipgraph.checks._common import (
    ArgError,
    compute_idempotency_key,
    error_result,
    glob_to_regex,
    iter_repo_files,
    optional_list_of_str,
    require_list_of_str,
    require_str,
    sha256_bytes,
    sha256_file,
)
from chipgraph.checks._naming_pyslang import (
    Decl,
    ParseFailure,
    declared_identifiers,
    parse_errors,
    parse_file,
)
from chipgraph.checks._naming_rules import LexicalRule, NamingRules, VocabularyRule
from chipgraph.core.config.loader import resolve_data_ref
from chipgraph.core.contracts import CheckResult, CheckSpec, Issue
from chipgraph.core.contracts.types import CheckStatus
from chipgraph.core.plugin_api.types import ToolContext

_DEFAULT_IGNORE = ("**/vendor/**", "vendor/**")
"""By default, never check anything under a `vendor` path segment (D: it keeps upstream
conventions and is not ours to rename)."""


class NamingCheck:
    """Checks that declared identifiers follow the project's data-defined naming rule."""

    id = "naming"
    name = "Naming"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        start = time.monotonic()
        args = spec.args

        try:
            rules_ref = require_str(args, "rules")
            scope = _require_scope(args)
            ignore = optional_list_of_str(args, "ignore") or list(_DEFAULT_IGNORE)
            block = ctx.params.get("block")
            scope = [_substitute(glob, block) for glob in scope]
            rules_path = _resolve_rules_path(rules_ref, ctx.repo_root)
            rules, rules_bytes = _load_rules(rules_path)
        except (ArgError, _RulesError) as exc:
            return error_result(spec, str(exc), start)

        scope_regexes = [glob_to_regex(p) for p in scope]
        ignore_regexes = [glob_to_regex(p) for p in ignore]
        in_scope = [
            f
            for f in iter_repo_files(ctx.repo_root)
            if any(r.fullmatch(f) for r in scope_regexes)
            and not any(r.fullmatch(f) for r in ignore_regexes)
        ]

        compiled = _CompiledRules(rules)
        issues: list[Issue] = []
        for rel in in_scope:
            issues.extend(_check_file(ctx.repo_root / rel, rel, compiled))
        issues.sort(key=lambda i: (i.file or "", i.line or 0, i.rule))

        file_hashes = [(f, sha256_file(ctx.repo_root / f)) for f in in_scope]
        file_hashes.append(("<rules>", sha256_bytes(rules_bytes)))
        key = compute_idempotency_key(spec.id, args, file_hashes)
        status: CheckStatus = "fail" if issues else "pass"
        return CheckResult(
            check_id=spec.id,
            status=status,
            issues=tuple(issues),
            duration_s=time.monotonic() - start,
            idempotency_key=key,
        )


# --------------------------------------------------------------------------------------
# argument and rule loading
# --------------------------------------------------------------------------------------


class _RulesError(Exception):
    """Raised when the rules ref cannot be resolved or the rules file is invalid."""


def _require_scope(args: Any) -> list[str]:
    scope = require_list_of_str(args, "scope")
    if not scope:
        raise ArgError("args['scope'] must list at least one glob")
    return scope


def _substitute(glob: str, block: str | None) -> str:
    """Substitute `{block}` in a scope glob from `ctx.params`, or raise if it is unset."""
    if "{block}" not in glob:
        return glob
    if block is None:
        raise ArgError(f"scope glob {glob!r} uses {{block}} but no block is set in ctx.params")
    return glob.replace("{block}", block)


def _resolve_rules_path(ref: str, repo_root: Path) -> Path:
    try:
        return resolve_data_ref(ref, repo_root)
    except Exception as exc:
        raise _RulesError(f"cannot resolve rules ref {ref!r}: {exc}") from exc


def _load_rules(path: Path) -> tuple[NamingRules, bytes]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise _RulesError(f"cannot read rules file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise _RulesError(f"invalid YAML in rules file {path}: {exc}") from exc
    try:
        return NamingRules.model_validate(data), raw_bytes
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise _RulesError(f"invalid rules file {path}: {loc}: {first['msg']}") from exc


# --------------------------------------------------------------------------------------
# compiled rules and file checking
# --------------------------------------------------------------------------------------


class _CompiledRules:
    """`NamingRules` with its regexes compiled once, ready to check names against."""

    def __init__(self, rules: NamingRules) -> None:
        self.rules = rules
        self.kind_patterns: dict[str, tuple[str, re.Pattern[str], str]] = {
            str(kind): (rule.rule, re.compile(rule.pattern), rule.message)
            for kind, rule in rules.identifiers.items()
        }
        self.lexical = [_CompiledLexical(lx) for lx in rules.lexical]
        self.vocabulary = _CompiledVocabulary(rules.vocabulary) if rules.vocabulary else None
        self.ignore = re.compile(r"//\s*" + re.escape(rules.ignore_comment))


class _CompiledLexical:
    def __init__(self, rule: LexicalRule) -> None:
        self.rule = rule.rule
        self.message = rule.message
        self.forbid = re.compile(rule.forbid_pattern)
        self.allow = re.compile(rule.allow_if_matches) if rule.allow_if_matches else None

    def violated_by(self, name: str) -> bool:
        if self.allow is not None and self.allow.fullmatch(name):
            return False
        return self.forbid.search(name) is not None


class _CompiledVocabulary:
    def __init__(self, rule: VocabularyRule) -> None:
        self.rule = rule.rule
        self.message = rule.message
        self.replace = dict(rule.replace)

    def offending_word(self, name: str) -> tuple[str, str] | None:
        low = name.lower()
        for bad, good in self.replace.items():
            if re.search(rf"(?:^|_){re.escape(bad)}(?:_|$)", low):
                return bad, good
        return None


def _check_file(path: Path, rel: str, rules: _CompiledRules) -> list[Issue]:
    try:
        text = path.read_text(errors="ignore")
        tree = parse_file(path)
    except OSError as exc:
        return [Issue(file=rel, rule="parse", severity="error", msg=f"cannot read file: {exc}")]

    failures = parse_errors(tree)
    if failures:
        return [_parse_issue(rel, f) for f in failures]

    exempt_lines = _exempt_lines(text, rules.ignore)
    issues: list[Issue] = []
    for decl in declared_identifiers(tree):
        if decl.line in exempt_lines:
            continue
        issues.extend(_check_decl(rel, decl, rules))
    return issues


def _parse_issue(rel: str, failure: ParseFailure) -> Issue:
    return Issue(
        file=rel,
        line=failure.line,
        rule="parse",
        severity="error",
        msg=f"parse error: {failure.message}",
    )


def _exempt_lines(text: str, ignore: re.Pattern[str]) -> frozenset[int]:
    return frozenset(i for i, line in enumerate(text.splitlines(), start=1) if ignore.search(line))


def _check_decl(rel: str, decl: Decl, rules: _CompiledRules) -> Iterable[Issue]:
    issues: list[Issue] = []
    kind_rule = rules.kind_patterns.get(decl.kind)
    if kind_rule is not None:
        rule_id, pattern, message = kind_rule
        if not pattern.fullmatch(decl.name):
            issues.append(
                Issue(
                    file=rel,
                    line=decl.line,
                    rule=rule_id,
                    severity="error",
                    msg=f"{decl.kind} '{decl.name}' {message}",
                )
            )

    for lx in rules.lexical:
        if lx.violated_by(decl.name):
            issues.append(
                Issue(
                    file=rel,
                    line=decl.line,
                    rule=lx.rule,
                    severity="error",
                    msg=f"'{decl.name}' {lx.message}",
                )
            )

    if rules.vocabulary is not None:
        hit = rules.vocabulary.offending_word(decl.name)
        if hit is not None:
            bad, good = hit
            issues.append(
                Issue(
                    file=rel,
                    line=decl.line,
                    rule=rules.vocabulary.rule,
                    severity="error",
                    msg=f"'{decl.name}' uses '{bad}'; {rules.vocabulary.message} '{good}'",
                )
            )
    return issues


def main() -> None:
    """Write the naming-rules JSON Schema (`python -m chipgraph.checks.naming [PATH]`)."""
    import sys

    from chipgraph.checks._naming_rules import schema_json

    out = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas/formats/naming-rules.schema.json")
    )
    out.write_text(schema_json(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()


__all__ = ["NamingCheck"]
