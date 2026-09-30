#!/usr/bin/env bash
# S7 spike runner: one headless Claude Code session on a throwaway copy of tinysoc.
#
#   docs/spikes/s7/run.sh [OUT_DIR]
#
# Env: MAIN_MODEL (default haiku), SUB_MODEL (default haiku), MAX_TURNS (default 30).
# Uses the logged-in Claude Code account (the lead's plan), never an API key.
# Writes nothing outside OUT_DIR (default /tmp/s7-run).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="${1:-/tmp/s7-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
SUB_MODEL="${SUB_MODEL:-haiku}"
MAX_TURNS="${MAX_TURNS:-30}"
PROJ="$OUT/tinysoc"

rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$REPO/examples/tinysoc" "$PROJ"
git -C "$PROJ" init -q -b main
git -C "$PROJ" add -A
git -C "$PROJ" -c user.name=s7 -c user.email=s7@example.invalid commit -qm "tinysoc copy for S7"

mkdir -p "$PROJ/.claude/agents"
# The role subagent: limited tools (no Bash), its own model.
cat > "$PROJ/.claude/agents/rtl-author.md" <<EOF
---
name: rtl-author
description: Writes one chipgraph RTL task. Use for every task returned by next_task.
tools: Read, Write, Edit, Glob, Grep, mcp__chipgraph__get_context
model: $SUB_MODEL
---
You implement exactly one chipgraph task. Call mcp__chipgraph__get_context with the
task_id you were given, read the example RTL it names, then write only the files listed
in its outputs. If a write is refused, do not retry it elsewhere: report it and finish.
EOF

# The write guard, for the main session and every subagent.
cat > "$PROJ/.claude/settings.json" <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          {"type": "command", "command": "python3", "args": ["$HERE/hook_guard.py"]}
        ]
      }
    ]
  }
}
EOF

cat > "$OUT/mcp.json" <<EOF
{"mcpServers": {"chipgraph": {"command": "uv",
  "args": ["run", "--project", "$REPO", "python", "$HERE/s7_server.py", "--root", "$PROJ"]}}}
EOF

PROMPT='You drive a chipgraph build. Loop: call mcp__chipgraph__next_task. For every task it
returns, start one rtl-author subagent with the task_id, all of them in parallel, and wait
for them. Then call mcp__chipgraph__submit for each task and report the result. Call
next_task again; stop when it says done. Do not write files yourself.'

cd "$PROJ"
start=$(date +%s)
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_METRICS_EXPORTER=console \
OTEL_METRIC_EXPORT_INTERVAL=5000 \
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
