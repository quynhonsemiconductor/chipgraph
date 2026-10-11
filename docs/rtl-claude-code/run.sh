#!/usr/bin/env bash
# M2-04 acceptance run: the RTL Author in real Claude Code, headless, on two tinysoc
# copies that differ only in their style exemplars. `/chipgraph:run digital-rtl/rtl_module`
# hands out one `rtl-author` task for the new module `tiny_pwm` of an approved plan; it
# writes the module from the plan, the requirements, the interface, the naming rules and
# the exemplars, never from a testbench. The style should follow the exemplars.
#
#   docs/rtl-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-rtl-<name>
#
# Env: MAIN_MODEL (default haiku: the session that runs /chipgraph:run); MEDIUM_MODEL
# (default sonnet) and LARGE_MODEL (default sonnet): the copies' `models.tiers`, so the
# rtl-author subagent (tier medium, escalating to large) runs on sonnet; MAX_TURNS
# (default 40).
#
# What it does, for each exemplar set S in a, b (exemplars/a: two-space indent, `begin`
# at the end of the line, asynchronous active-low reset, `_q`/`_d`, a comment header,
# padded ports; exemplars/b: four-space indent, `begin` on its own line, synchronous
# active-low reset, `_r`/`_next`, no header, unpadded ports):
#   1. builds OUT_DIR/S/tinysoc (fixture.py): examples/tinysoc with a new block `pwm`
#      (its MAS, chip.yml entry and filelist), the `digital-rtl` pack only, the RTL rule's
#      checks (format with a whitespace-only formatter, so the measured style is the
#      author's; lint; tinysoc's naming rule; style; hardcode), the exemplar set S as
#      `style.exemplars`, a fingerprint comment in every testbench file under dv/, the
#      Design Model, and the plan for `pwm` written, approved and expanded; examples/
#      tinysoc is not changed;
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release (once);
#   3. runs `claude -p "/chipgraph:run digital-rtl/rtl_module" --plugin-dir OUT_DIR/plugin`
#      in the copy with the logged-in Claude Code account (no API key: ANTHROPIC_API_KEY
#      is unset), stream-json to OUT_DIR/S/stream.jsonl;
# then prints report.py's verdict: per run the tools used; PASS iff, in both runs, the
# module was written and accepted, lint and naming pass, no tool call read a testbench
# and no testbench line is in the subagent's prompt or tool results, and the module's
# style distance to its own exemplars is smaller than to the other set's (both
# distances are printed).
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-rtl-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
MAX_TURNS="${MAX_TURNS:-40}"

# OUT_DIR is deleted first: accept only /tmp/cg-rtl-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-rtl-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-rtl-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT"

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

# Pre-approve what the loop needs. This gives the rtl-author no tool: a subagent gets only
# the tools in its agent file (no read tools for chipgraph:rtl-author), bounded by the guard.
TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Write Edit MultiEdit"

for SET in a b; do
  RUN="$OUT/$SET"
  PROJ="$RUN/tinysoc"
  mkdir -p "$RUN"
  (cd "$REPO" && uv run python "$HERE/fixture.py" "$PROJ" --exemplars "$SET" \
    --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL") >/dev/null 2>"$RUN/fixture.log"

  cd "$PROJ"
  start=$(date +%s)
  env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
    -u OTEL_LOGS_EXPORTER \
  claude -p "/chipgraph:run digital-rtl/rtl_module" \
    --model "$MAIN_MODEL" \
    --max-turns "$MAX_TURNS" \
    --plugin-dir "$OUT/plugin" \
    --allowedTools "$TOOLS" \
    --permission-mode acceptEdits \
    --output-format stream-json --verbose \
    > "$RUN/stream.jsonl" 2> "$RUN/stderr.log" || echo "claude exited $?" >> "$RUN/stderr.log"
  end=$(date +%s)
  echo "wall_s=$((end - start))" > "$RUN/timing.txt"
done

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
