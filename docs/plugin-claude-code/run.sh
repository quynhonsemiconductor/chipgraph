#!/usr/bin/env bash
# M1-16 acceptance run: every v0 command of the chipgraph plugin in real Claude Code,
# headless, once each.
#
#   docs/plugin-claude-code/run.sh [OUT_DIR]      # OUT_DIR must be /tmp/cg-plugin-<name>
#
# Env: MAIN_MODEL (default haiku); SMALL_MODEL (default haiku) and LARGE_MODEL (default
# opus; sonnet if your plan does not allow opus): the decider tiers for /chipgraph:triage,
# written into the copy's profile; MAX_TURNS (default 30 per command); ONLY (space-separated
# case ids, default all: status trace trace_unknown ask triage init); ALLOW_MCP (default 1:
# pre-approve the chipgraph MCP tools for the session with --allowedTools, as a user who
# approved them once would have; 0: rely on each command's `allowed-tools` alone).
#
# What it does:
#   1. copies examples/tinysoc to OUT_DIR/tinysoc (its own git repo, with one sample log
#      that needs a model copied in as logs/triage.log; the example is not changed), sets
#      the decider tiers, runs `chipgraph ingest` and `chipgraph build '*'` there (so
#      /chipgraph:status has a run with gates waiting);
#   2. copies tinysoc again to OUT_DIR/fresh without its .chipgraph.yml, for
#      /chipgraph:init-chipgraph;
#   3. copies plugin/ to OUT_DIR/plugin with a .mcp.json that runs this checkout
#      (`uv run --project <repo> chipgraph`) instead of the PyPI release;
#   4. runs `claude -p "<command>" --plugin-dir OUT_DIR/plugin` once per case, with the
#      logged-in Claude Code account (no API key: ANTHROPIC_API_KEY is unset) and WITHOUT
#      any --disallowedTools: each command's own `disallowed-tools` frontmatter must keep
#      other tools, skills and agents out; one stream per case in OUT_DIR/streams/;
#   5. report.py: per case the tools called (main session and subagents), PASS or FAIL, and
#      the total cost.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-plugin-run}"
MAIN_MODEL="${MAIN_MODEL:-haiku}"
SMALL_MODEL="${SMALL_MODEL:-haiku}"
LARGE_MODEL="${LARGE_MODEL:-opus}"
MAX_TURNS="${MAX_TURNS:-30}"
ONLY="${ONLY:-}"
ALLOW_MCP="${ALLOW_MCP:-1}"

# OUT_DIR is deleted first: accept only /tmp/cg-plugin-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-plugin-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-plugin-<name>)" >&2
  exit 2
fi
OUT="$tmp/$name"
rm -rf "$OUT"
mkdir -p "$OUT/streams"
PROJ="$OUT/tinysoc"
FRESH="$OUT/fresh"

commit() {
  git -C "$1" init -q -b main
  git -C "$1" add -A
  git -C "$1" -c user.email=plugin@example.invalid -c user.name=plugin commit -q -m "$2"
}

# 1. tinysoc, ingested and built once.
cp -R "$REPO/examples/tinysoc" "$PROJ"
rm -rf "$PROJ/.chipgraph/state" "$PROJ/build" "$PROJ/obj_dir"  # local run output, if any
cat >> "$PROJ/.chipgraph.yml" <<EOF

# Added by docs/plugin-claude-code/run.sh: the models of the decider tiers.
models:
  tiers:
    small: $SMALL_MODEL
    large: $LARGE_MODEL
EOF
TRIAGE_LOG="$(cd "$REPO" && uv run python "$HERE/report.py" --triage-log)"
mkdir -p "$PROJ/logs"
cp "$REPO/evals/triage/logs/$TRIAGE_LOG.log" "$PROJ/logs/triage.log"  # the log only
commit "$PROJ" "tinysoc"
(cd "$REPO" && uv run chipgraph -C "$PROJ" ingest) > "$OUT/ingest.txt" 2>&1
(cd "$REPO" && uv run chipgraph -C "$PROJ" build '*') > "$OUT/build.txt" 2>&1 || true

# 2. a project with no .chipgraph.yml.
cp -R "$REPO/examples/tinysoc" "$FRESH"
rm -rf "$FRESH/.chipgraph" "$FRESH/build" "$FRESH/obj_dir" "$FRESH/.chipgraph.yml"
commit "$FRESH" "fresh"

# 3. the plugin, with its server from this checkout.
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

ALLOW=()
if [ "$ALLOW_MCP" = "1" ]; then
  # Pre-approval only (it removes nothing): the chipgraph MCP tools the plugin names.
  ALLOW=(--allowedTools "$(cd "$REPO" && uv run python "$HERE/report.py" --allow)")
fi

# 4. one headless session per case: id<TAB>project<TAB>prompt.
(cd "$REPO" && uv run python "$HERE/report.py" --cases) > "$OUT/cases.tsv"
start=$(date +%s)
while IFS=$'\t' read -r id project prompt; do
  if [ -n "$ONLY" ] && [[ " $ONLY " != *" $id "* ]]; then
    continue
  fi
  echo "== $id ($project): $prompt"
  (
    cd "$OUT/$project"
    env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY -u OTEL_METRICS_EXPORTER \
      -u OTEL_LOGS_EXPORTER \
    claude -p "$prompt" \
      --model "$MAIN_MODEL" \
      --max-turns "$MAX_TURNS" \
      --plugin-dir "$OUT/plugin" \
      ${ALLOW[@]+"${ALLOW[@]}"} \
      --output-format stream-json --verbose \
      < /dev/null > "$OUT/streams/$id.jsonl" 2> "$OUT/streams/$id.stderr"
  ) || echo "claude exited $?" >> "$OUT/streams/$id.stderr"
done < "$OUT/cases.tsv"
end=$(date +%s)
echo "wall_s=$((end - start))" > "$OUT/timing.txt"
git -C "$PROJ" status --porcelain > "$OUT/git-status-tinysoc.txt"
git -C "$FRESH" status --porcelain --untracked-files=all > "$OUT/git-status-fresh.txt"

# 5. the report.
cd "$REPO"
uv run python "$HERE/report.py" "$OUT"
