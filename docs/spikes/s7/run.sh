#!/usr/bin/env bash
# S7 spike runner: one headless Claude Code session on a throwaway copy of tinysoc.
#
#   docs/spikes/s7/run.sh [OUT_DIR]        # OUT_DIR must be /tmp/s7-<name>
#
# Env: MAIN_MODEL (default haiku), SUB_MODEL (default haiku), MAX_TURNS (default 30).
# Uses the logged-in Claude Code account (the lead's plan), never an API key.
# Tokens and cost come from the final `result` event of the stream (usage,
# total_cost_usd, modelUsage). OpenTelemetry is left off: its console exporter would
# print into the same stdout as the stream.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-/tmp/s7-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
MAX_TURNS="${MAX_TURNS:-30}"

"$HERE/setup.sh" "$OUT"
PROJ="$OUT/tinysoc"

PROMPT='You drive a chipgraph build. Loop: call mcp__chipgraph__next_task. For every task it
returns, start one rtl-author subagent with the task_id, all of them in parallel, and wait
for them. Then call mcp__chipgraph__submit for each task and report the result. Call
next_task again; stop when it says done. Do not write files yourself.'

cd "$PROJ"
start=$(date +%s)
env -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER -u OTEL_LOGS_EXPORTER \
claude -p "$PROMPT" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --mcp-config "$OUT/mcp.json" --strict-mcp-config \
  --allowedTools "mcp__chipgraph__next_task mcp__chipgraph__get_context mcp__chipgraph__submit Agent Read Write Edit Glob Grep" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

python3 "$HERE/report.py" "$OUT"
