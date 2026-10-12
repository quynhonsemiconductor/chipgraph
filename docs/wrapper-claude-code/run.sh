#!/usr/bin/env bash
# M2-12 acceptance run: the wrapper rule for vendor IP in real Claude Code, headless, on
# a tinysoc copy. `/chipgraph:run digital-rtl/wrapper` hands out one `rtl-author` task:
# write the source `rtl/emacs/tiny_fifo_wrap.src.sv` of the wrapper around the vendored
# FIFO (`vendor/vendor_fifo`), from the IP's port declaration, the naming rules and the
# skill `rtl/wrapper-auto` alone: the template and the structure, never the expanded
# instance. The engine then expands it with emacs verilog-mode (`wrap_check` is the
# rule's check, so a template that leaves an IP port unmapped is rejected with the
# expanded lines and the author tries again).
#
#   docs/wrapper-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-wrapper-<name>
#
# Needs: emacs with verilog-mode (`brew install emacs`, `apt install emacs-nox`) and
# Verilator on PATH, and the logged-in Claude Code account.
#
# Env: MAIN_MODEL (default sonnet: the session that runs /chipgraph:run); MEDIUM_MODEL
# (default sonnet) and LARGE_MODEL (default sonnet): the copy's `models.tiers`, so the
# rtl-author subagent (tier medium, escalating to large) runs on sonnet; MAX_TURNS
# (default 40).
#
# What it does:
#   1. builds OUT_DIR/tinysoc (fixture.py): examples/tinysoc without its sample wrapper
#      (source and expansion deleted), the `digital-rtl` pack only, the Design Model;
#      examples/tinysoc is not changed;
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. runs `claude -p "/chipgraph:run digital-rtl/wrapper" --plugin-dir OUT_DIR/plugin`
#      in the copy with the logged-in Claude Code account (no API key: ANTHROPIC_API_KEY
#      is unset), stream-json to OUT_DIR/stream.jsonl;
#   4. runs `chipgraph build digital-rtl/wrapper_gen[...]` in the copy (the generator,
#      once the source is accepted), output to OUT_DIR/build.json;
# then prints report.py's verdict. PASS iff
#   (a) the source was written by the subagent (a `Write` of that path) and the task is
#       accepted;
#   (b) the generated wrapper exists and passes `wrap_check`, `lint` and `naming`
#       (re-run here as the engine runs them);
#   (c) the vendor files are byte-identical to `vendor/MANIFEST.sha256` (and unchanged
#       in git);
#   (d) the subagent was `chipgraph:rtl-author` and used only `get_context` and `Write`:
#       no read tool, no shell.
# The report prints the checks of each attempt, and the run's cost.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-wrapper-run}"
MAIN_MODEL="${MAIN_MODEL:-sonnet}"
MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
MAX_TURNS="${MAX_TURNS:-40}"

# OUT_DIR is deleted first: accept only /tmp/cg-wrapper-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-wrapper-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-wrapper-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
for tool in emacs verilator; do
  command -v "$tool" >/dev/null || { echo "run.sh: $tool is not on PATH" >&2; exit 2; }
done
rm -rf "$OUT"
mkdir -p "$OUT"
PROJ="$OUT/tinysoc"

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

(cd "$REPO" && uv run python "$HERE/fixture.py" "$PROJ" \
  --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL") >/dev/null 2>"$OUT/fixture.log"

# Pre-approve what the loop needs. This gives the rtl-author no tool: a subagent gets only
# the tools in its agent file (no read tools for chipgraph:rtl-author), bounded by the guard.
TOOLS="mcp__plugin_chipgraph_chipgraph__next_task mcp__plugin_chipgraph_chipgraph__get_context"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__submit Agent Write Edit MultiEdit"

cd "$PROJ"
start=$(date +%s)
env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
  -u OTEL_LOGS_EXPORTER \
claude -p "/chipgraph:run digital-rtl/wrapper" \
  --model "$MAIN_MODEL" \
  --max-turns "$MAX_TURNS" \
  --plugin-dir "$OUT/plugin" \
  --allowedTools "$TOOLS" \
  --permission-mode acceptEdits \
  --output-format stream-json --verbose \
  > "$OUT/stream.jsonl" 2> "$OUT/stderr.log" || echo "claude exited $?" >> "$OUT/stderr.log"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

# The generator, once the source is accepted: the engine's own run, not the model's.
(cd "$REPO" && uv run chipgraph --json -C "$PROJ" build \
  "digital-rtl/wrapper_gen[block=top,wrapper=tiny_fifo_wrap]") \
  > "$OUT/build.json" 2>&1 || true

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
