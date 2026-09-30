#!/usr/bin/env bash
# M1-11 acceptance run: runtime claude-code in real Claude Code, headless, on a tinysoc copy.
#
#   docs/runtime-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-cc-<name>
#
# Env: MAIN_MODEL (default opus), MAX_TURNS (default 40), LINT (verilator | python; default
# verilator when it is on PATH). The subagent model comes from the task (`next_task` maps
# the rule's tier `small` to haiku).
#
# What it does:
#   1. builds OUT_DIR/tinysoc: examples/tinysoc plus the test fixture pack `pulse` (one
#      agent rule `pulse/tiny_pulse` writing rtl/tiny_pulse.sv, then a gen rule), a git
#      repo (tests/mcp/runtime_tools_helpers.py; examples/tinysoc is not changed);
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. runs `claude -p "/chipgraph:run pulse/pulse_manifest" --plugin-dir OUT_DIR/plugin`
#      with the logged-in Claude Code account (no API key: ANTHROPIC_API_KEY is unset);
#   4. runs `chipgraph build pulse/pulse_manifest` again and prints report.py's summary.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-cc-run}"
MAIN_MODEL="${MAIN_MODEL:-opus}"
MAX_TURNS="${MAX_TURNS:-40}"
if [ -z "${LINT:-}" ]; then
  if command -v verilator >/dev/null 2>&1; then LINT=verilator; else LINT=python; fi
fi

# OUT_DIR is deleted first: accept only /tmp/cg-cc-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-cc-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-cc-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT"
PROJ="$OUT/tinysoc"

(cd "$REPO" && uv run python tests/mcp/runtime_tools_helpers.py "$PROJ" --lint "$LINT") >/dev/null

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

TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Read Write Edit Glob Grep"

cd "$PROJ"
start=$(date +%s)
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
  -u OTEL_LOGS_EXPORTER \
claude -p "/chipgraph:run pulse/pulse_manifest" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --plugin-dir "$OUT/plugin" \
  --allowedTools "$TOOLS" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

(cd "$REPO" && uv run chipgraph --json -C "$PROJ" build pulse/pulse_manifest) \
  > "$OUT/build.json" 2>&1 || true

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
