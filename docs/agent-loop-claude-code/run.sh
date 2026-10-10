#!/usr/bin/env bash
# M2-02b acceptance run: the agent loop in runtime claude-code, in real Claude Code,
# headless, on a tinysoc copy. One task fails its check and is fixed on the second try;
# one task can never pass and must end in HANDOFF after its 2 tries.
#
#   docs/agent-loop-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-loop-<name>
#
# Env: MAIN_MODEL (default haiku: the session that runs /chipgraph:run); MEDIUM_MODEL
# (default haiku) and LARGE_MODEL (default sonnet): the copy's `models.tiers`, so the
# author subagents (tier medium, escalating to large after a rejection) stay cheap;
# MAX_TURNS (default 40: a hard cap on the main session; the report's PASS bound is
# lower, see report.py).
#
# What it does:
#   1. builds OUT_DIR/tinysoc: examples/tinysoc plus the fixture pack `loop` (fixture.py:
#      `loop/fixable`, `loop/hopeless` with budget.tries 2, and the gen rule `loop/done`
#      over both outputs), a git repo; examples/tinysoc is not changed;
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. runs `claude -p "/chipgraph:run loop/done" --plugin-dir OUT_DIR/plugin` with the
#      logged-in Claude Code account (no API key: ANTHROPIC_API_KEY is unset), stream-json;
#   4. runs `chipgraph build loop/done` again and prints report.py's verdict.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-loop-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
MEDIUM_MODEL="${MEDIUM_MODEL:-haiku}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
MAX_TURNS="${MAX_TURNS:-40}"

# OUT_DIR is deleted first: accept only /tmp/cg-loop-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-loop-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-loop-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT"
PROJ="$OUT/tinysoc"

(cd "$REPO" && uv run python "$HERE/fixture.py" "$PROJ" \
  --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL") >/dev/null

cp -R "$REPO/plugin" "$OUT/plugin"
cat > "$OUT/plugin/.mcp.json" <<EOF
{
  "mcpServers": {
    "chipgraph": {
      "command": "uv",
      "args": ["run", "--project", "$REPO", "chipgraph", "-C", "\${CLAUDE_PROJECT_DIR}", "mcp"]
    }
  }
}
EOF

# Pre-approve what the loop and the author need; the guard bounds the subagents.
TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Read Write Edit MultiEdit Glob Grep"

cd "$PROJ"
start=$(date +%s)
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
  -u OTEL_LOGS_EXPORTER \
claude -p "/chipgraph:run loop/done" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --plugin-dir "$OUT/plugin" \
  --allowedTools "$TOOLS" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

(cd "$REPO" && uv run chipgraph --json -C "$PROJ" build loop/done) \
  > "$OUT/build.json" 2>&1 || true

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
