#!/usr/bin/env bash
# M1-14 acceptance run: /chipgraph:triage in real Claude Code, headless, on every sample log.
#
#   docs/triage-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-triage-<name>
#
# Env: MAIN_MODEL (default haiku: the main session only dispatches), SMALL_MODEL (default
# haiku) and LARGE_MODEL (default opus; sonnet if your plan does not allow opus): the
# decider tiers, written into the copy's profile (`models.tiers`); MAX_TURNS (default 30
# per sample); ONLY (space-separated sample ids, default every sample of the set); SET
# (default: the 22 samples of evals/triage/faults.yml; `holdout`: evals/triage/holdout.yml,
# logs from evals/triage/logs-holdout/, graded only, never used to tune rules).
#
# What it does:
#   1. copies examples/tinysoc to OUT_DIR/tinysoc (its own git repo; the example is not
#      changed), sets the decider tiers in its profile, runs `chipgraph ingest` there (the
#      spec lines a simulation failure is judged against), and copies the sample logs to
#      OUT_DIR/tinysoc/logs/ (only the logs: the labels stay in faults.yml);
#   2. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   3. for every sample: `claude -p "/chipgraph:triage logs/<id>.log [check]" --plugin-dir
#      OUT_DIR/plugin`, with the logged-in Claude Code account (no API key:
#      ANTHROPIC_API_KEY is unset), only the Agent tool and the three triage/decider MCP
#      tools allowed; one stream per sample in OUT_DIR/streams/;
#   4. report.py: per sample the triage results, the decider subagents and their models,
#      cost; writes OUT_DIR/answers.jsonl and grades it with evals/triage/grade.py
#      (>= 80 % correct; accuracy by backend: rule, small, large).
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-triage-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
SMALL_MODEL="${SMALL_MODEL:-haiku}"
LARGE_MODEL="${LARGE_MODEL:-opus}"
MAX_TURNS="${MAX_TURNS:-30}"
ONLY="${ONLY:-}"
SET="${SET:-default}"
case "$SET" in
  default) LOG_DIR="$REPO/evals/triage/logs" ;;
  holdout) LOG_DIR="$REPO/evals/triage/logs-holdout" ;;
  *) echo "run.sh: unknown SET=$SET (use default or holdout)" >&2; exit 2 ;;
esac

# OUT_DIR is deleted first: accept only /tmp/cg-triage-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-triage-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-triage-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT/streams"
PROJ="$OUT/tinysoc"

cp -R "$REPO/examples/tinysoc" "$PROJ"
rm -rf "$PROJ/.chipgraph/state" "$PROJ/build" "$PROJ/obj_dir"  # local run output, if any
cat >> "$PROJ/.chipgraph.yml" <<EOF

# Added by docs/triage-claude-code/run.sh: the models of the decider tiers.
models:
  tiers:
    small: $SMALL_MODEL
    large: $LARGE_MODEL
EOF
git -C "$PROJ" init -q -b main
git -C "$PROJ" add -A
git -C "$PROJ" -c user.email=triage@example.invalid -c user.name=triage commit -q -m "tinysoc"
(cd "$REPO" && uv run chipgraph -C "$PROJ" ingest) > "$OUT/ingest.txt" 2>&1
mkdir -p "$PROJ/logs"
cp "$LOG_DIR"/*.log "$PROJ/logs/"

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

TOOLS="mcp__plugin_chipgraph_chipgraph__triage mcp__plugin_chipgraph_chipgraph__pending_decisions"
TOOLS="$TOOLS mcp__plugin_chipgraph_chipgraph__answer_decision Agent"
# Triage needs none of these. Denying them for the session (and so for every subagent)
# keeps other plugins' agents and skills installed on this machine out of the run: in a
# first run, a Haiku main session sometimes reached for one of them instead of `triage`.
DENY="Bash Read Write Edit MultiEdit NotebookEdit Glob Grep Skill WebFetch WebSearch"

(cd "$REPO" && uv run python "$HERE/report.py" --set "$SET" --list) > "$OUT/samples.tsv"

start=$(date +%s)
while IFS=$'\t' read -r id check; do
  if [ -n "$ONLY" ] && [[ " $ONLY " != *" $id "* ]]; then
    continue
  fi
  [ "$check" = "-" ] && check=""
  args="logs/$id.log${check:+ $check}"
  echo "== $id: /chipgraph:triage $args"
  (
    cd "$PROJ"
    env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
      -u OTEL_LOGS_EXPORTER \
    claude -p "/chipgraph:triage $args" \
      --model "$MAIN_MODEL" \
      --max-turns "$MAX_TURNS" \
      --plugin-dir "$OUT/plugin" \
      --allowedTools "$TOOLS" \
      --disallowedTools "$DENY" \
      --output-format stream-json --verbose \
      < /dev/null > "$OUT/streams/$id.jsonl" 2> "$OUT/streams/$id.stderr"
  ) || echo "claude exited $?" >> "$OUT/streams/$id.stderr"
done < "$OUT/samples.tsv"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"

cd "$REPO"
uv run python "$HERE/report.py" --set "$SET" "$OUT"
