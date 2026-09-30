#!/usr/bin/env bash
# M1-13 acceptance run: /chipgraph:ask in real Claude Code, headless, on a tinysoc copy.
#
#   docs/ask-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-ask-<name>
#
# Env: MAIN_MODEL (default opus: the main session), MAX_TURNS (default 12 per question),
# ONLY (space-separated question ids, default all 20 of evals/ask/tinysoc.yml). The
# answering subagent `chipgraph:asker` runs on haiku (its frontmatter, and the model the
# command passes to the Agent tool).
#
# What it does:
#   1. copies examples/tinysoc to OUT_DIR/tinysoc (its own git repo; the example is not
#      changed) and runs `chipgraph ingest` there (the model and the /ask documents);
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. for every question: `claude -p "/chipgraph:ask <question>" --plugin-dir
#      OUT_DIR/plugin`, with the logged-in Claude Code account (no API key:
#      ANTHROPIC_API_KEY is unset), only the Agent tool and the two ask MCP tools allowed;
#      one stream per question in OUT_DIR/streams/;
#   4. report.py: per question the Agent call, the asker's tools, the checked answer and
#      cost; writes OUT_DIR/answers.jsonl and grades it with evals/ask/grade.py
#      (>= 90 % correct citations on answerable questions, 0 invented answers).
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-ask-run}"
MAIN_MODEL="${MAIN_MODEL:-opus}"
MAX_TURNS="${MAX_TURNS:-12}"
ONLY="${ONLY:-}"

# OUT_DIR is deleted first: accept only /tmp/cg-ask-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-ask-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-ask-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT/streams"
PROJ="$OUT/tinysoc"

cp -R "$REPO/examples/tinysoc" "$PROJ"
rm -rf "$PROJ/.chipgraph/state" "$PROJ/build" "$PROJ/obj_dir"  # local run output, if any
git -C "$PROJ" init -q -b main
git -C "$PROJ" add -A
git -C "$PROJ" -c user.email=ask@example.invalid -c user.name=ask commit -q -m "tinysoc"
(cd "$REPO" && uv run chipgraph -C "$PROJ" ingest) > "$OUT/ingest.txt" 2>&1

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

TOOLS="mcp__plugin_chipgraph_chipgraph__ask_context mcp__plugin_chipgraph_chipgraph__ask_check"
TOOLS="$TOOLS Agent"

(cd "$REPO" && uv run python "$HERE/report.py" --list) > "$OUT/questions.tsv"

start=$(date +%s)
while IFS=$'\t' read -r id question; do
  if [ -n "$ONLY" ] && [[ " $ONLY " != *" $id "* ]]; then
    continue
  fi
  echo "== $id: $question"
  (
    cd "$PROJ"
    env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
      -u OTEL_LOGS_EXPORTER \
    claude -p "/chipgraph:ask $question" \
      --model "$MAIN_MODEL" \
      --max-turns "$MAX_TURNS" \
      --plugin-dir "$OUT/plugin" \
      --allowedTools "$TOOLS" \
      --output-format stream-json --verbose \
      < /dev/null > "$OUT/streams/$id.jsonl" 2> "$OUT/streams/$id.stderr"
  ) || echo "claude exited $?" >> "$OUT/streams/$id.stderr"
done < "$OUT/questions.tsv"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
