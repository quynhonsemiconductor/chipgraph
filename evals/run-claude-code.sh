#!/usr/bin/env bash
# Real-model acceptance run of the evals framework (task M1-17): every suite with runtime
# claude-code, as the `evals` CI job runs them, on this machine.
#
#   evals/run-claude-code.sh [OUT_DIR]      # default /tmp/cg-eval-run; must be /tmp/cg-eval-*
#
# Env: SUITES (default "ask triage triage-holdout triage-holdout2"), MAIN_MODEL (default
# haiku), BUDGET_USD (default 3, the cap per suite), plus anything `chipgraph eval` reads
# (CLAUDE_CODE_OAUTH_TOKEN; without it, the logged-in Claude Code account is used).
# Writes OUT_DIR/<suite>/ (Inspect log, answers.jsonl, summary.json, summary.md, streams/)
# and prints every summary. Exit 1 when a suite failed.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-/tmp/cg-eval-run}"
SUITES="${SUITES:-ask triage triage-holdout triage-holdout2}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
BUDGET_USD="${BUDGET_USD:-3}"

# OUT_DIR is deleted first: accept only /tmp/cg-eval-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-eval-?* ]]; then
  echo "run-claude-code.sh: refusing OUT_DIR=$OUT (use /tmp/cg-eval-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT"

failed=""
for suite in $SUITES; do
  echo "== chipgraph eval $suite (main model $MAIN_MODEL, budget \$$BUDGET_USD)"
  rc=0
  (cd "$REPO" && uv run chipgraph eval "$suite" --runtime claude-code \
    --main-model "$MAIN_MODEL" --budget-usd "$BUDGET_USD" --out "$OUT/$suite") || rc=$?
  [ "$rc" -eq 0 ] || failed="$failed $suite"
done
echo "== report: $OUT"
if [ -n "$failed" ]; then
  echo "failed:$failed"
  exit 1
fi
