#!/usr/bin/env bash
# M2-06 acceptance run: the testbench Author in real Claude Code, headless, on a tinysoc
# copy. `/chipgraph:run dv/tb_module` hands out one `tb-author` task per block (gpio,
# timer); each writes a cocotb test from the spec and the interface only, never the RTL.
#
#   docs/tb-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-tb-<name>
#
# Env: MAIN_MODEL (default haiku: the session that runs /chipgraph:run); MEDIUM_MODEL
# (default sonnet) and LARGE_MODEL (default sonnet): the copy's `models.tiers`, so the
# tb-author subagents (tier medium, escalating to large) run on sonnet. Haiku is cheaper,
# but an offline estimate is that it often writes cocotb 1.x idioms (`units=`,
# `.integer`, `.value.binstr`) that the static check cannot see and the simulation then
# rejects, so sonnet is the default here; set MEDIUM_MODEL=haiku to try it. MAX_TURNS
# (default 40); SIM_FEEDBACK=1 also runs the test on the RTL inside `tb_static` (the
# Author then gets the simulation's failures, filtered of RTL text, as redo feedback).
#
# What it does:
#   1. builds OUT_DIR/tinysoc: examples/tinysoc with the `dv` pack and the `tb_static`
#      check (fixture.py), only the blocks gpio and timer, a fingerprint comment in each
#      RTL body, a git repo, and the Design Model ingested; examples/tinysoc is not
#      changed;
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. runs `claude -p "/chipgraph:run dv/tb_module" --plugin-dir OUT_DIR/plugin` with the
#      logged-in Claude Code account (no API key: ANTHROPIC_API_KEY is unset), stream-json;
#   4. prints report.py's verdict: per task the tools used; PASS iff each task wrote its
#      test, the test passes `tb_static`, and no tool call read RTL and no RTL body line
#      is in the subagent's prompt or tool results; then (informational, not part of
#      PASS) runs each written test on the RTL with the `edalize` adapter (Verilator)
#      and prints pass/fail per test.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-tb-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
MAX_TURNS="${MAX_TURNS:-40}"
SIM_FEEDBACK="${SIM_FEEDBACK:-0}"

# OUT_DIR is deleted first: accept only /tmp/cg-tb-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-tb-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-tb-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT"
PROJ="$OUT/tinysoc"

SIM_ARG=()
if [ "$SIM_FEEDBACK" = "1" ]; then SIM_ARG=(--sim); fi
(cd "$REPO" && uv run python "$HERE/fixture.py" "$PROJ" \
  --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL" ${SIM_ARG[@]+"${SIM_ARG[@]}"}) \
  >/dev/null

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

# Pre-approve what the loop needs. This gives the tb-author no tool: a subagent gets only
# the tools in its agent file (no read tools for chipgraph:tb-author), bounded by the guard.
TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Write Edit MultiEdit"

cd "$PROJ"
start=$(date +%s)
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
  -u OTEL_LOGS_EXPORTER \
claude -p "/chipgraph:run dv/tb_module" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --plugin-dir "$OUT/plugin" \
  --allowedTools "$TOOLS" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

cd "$REPO"
uv run --extra sim python "$HERE/report.py" "$OUT"
