#!/usr/bin/env bash
# M2-01 acceptance run: roles in real Claude Code, headless, on a tinysoc copy. One
# `author` task and one `tb-author` task whose instructions tell it to read the RTL; the
# tb-author must not be able to.
#
#   docs/roles-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-roles-<name>
#
# Env: MAIN_MODEL (default haiku: the session that runs /chipgraph:run); MEDIUM_MODEL
# (default haiku) and LARGE_MODEL (default sonnet): the copy's `models.tiers`, so both
# role subagents (tier medium, escalating to large) stay cheap; MAX_TURNS (default 40).
#
# What it does:
#   1. builds OUT_DIR/tinysoc: examples/tinysoc plus the fixture pack `roles`
#      (fixture.py: an author rule, a tb-author rule, a gen rule `roles/done` over both
#      outputs), a git repo; examples/tinysoc is not changed;
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. runs `claude -p "/chipgraph:run roles/done" --plugin-dir OUT_DIR/plugin` with the
#      logged-in Claude Code account (no API key: ANTHROPIC_API_KEY is unset), stream-json;
#   4. runs `chipgraph build roles/done` again and prints report.py's verdict: per task the
#      subagent type, model, tools attempted and denied; PASS only if the author wrote its
#      output, the tb-author wrote its output or stopped with needs_human, and no tool call
#      of the tb-author read any RTL.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-roles-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
MEDIUM_MODEL="${MEDIUM_MODEL:-haiku}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
MAX_TURNS="${MAX_TURNS:-40}"

# OUT_DIR is deleted first: accept only /tmp/cg-roles-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-roles-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-roles-<name>)" >&2
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

# Pre-approve what the loop and the author need. This does not give the tb-author any
# tool: a subagent gets only the tools in its agent file (and the guard bounds those).
TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Read Write Edit MultiEdit Glob Grep"

cd "$PROJ"
start=$(date +%s)
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
  -u OTEL_LOGS_EXPORTER \
claude -p "/chipgraph:run roles/done" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --plugin-dir "$OUT/plugin" \
  --allowedTools "$TOOLS" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

(cd "$REPO" && uv run chipgraph --json -C "$PROJ" build roles/done) \
  > "$OUT/build.json" 2>&1 || true

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
