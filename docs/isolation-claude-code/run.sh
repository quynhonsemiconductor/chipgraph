#!/usr/bin/env bash
# M2-11c acceptance run: isolated workspaces and best-of-N selection in Claude Code.
# On a tinysoc copy with `fanout.isolate: true`, the real `timer` block (not a stub), headless.
#
#   docs/isolation-claude-code/run.sh [OUT_DIR]   # OUT_DIR must be /tmp/cg-isolation-<name>
#
# Env: MAIN_MODEL (default sonnet: the session that runs the commands); MEDIUM_MODEL
# (default sonnet: tier medium, the RTL and testbench authors); LARGE_MODEL (default
# sonnet: tier large, the planner and the Critic); PLAN_TURNS (default 40) and
# RTL_TURNS (default 80): --max-turns of the two sessions.
#
# What it verifies (report.py):
#   Phase 1 (isolation only):
#   (a) isolation: every Write went to a workspace, never to the project root
#   (b) acceptance: every module of the approved plan has its RTL file in the user's tree
#   (c) workspaces: all workspaces are cleaned up at the end
#   (e) branch: the PR branch/outputs are tracked
#   (f) independence: RTL authors didn't see TB fingerprints, vice versa
#   (g) headless: no model approved anything; the gates are the script's people
#
#   Phase 2 (best-of-2):
#   (d) best-of: for each best-of module, >= 2 candidates were dispatched and exactly one landed
#
# Phases:
#   0. OUT_DIR is checked and emptied; plugin/ is copied with the dev .mcp.json
#   ---- PHASE 1: ISOLATION ONLY ----
#   1. fixture.py builds OUT_DIR/p1/tinysoc with fanout.isolate: true (no best_of)
#   A1. `claude -p "/chipgraph:plan timer"`: planner writes the plan
#   H1. The person approves the plan (chipgraph approve plan:timer)
#   B1. `claude -p "/chipgraph:rtl timer"`: RTL authors (parallel, isolated), testbench, Critic
#   H2. The reviewer approves the PR (chipgraph approve pr:timer)
#   R1. report.py OUT_DIR/p1 --phase 1: the phase 1 verdict
#   ---- PHASE 2: BEST-OF-2 ----
#   2. fixture.py builds OUT_DIR/p2/tinysoc with fanout.isolate: true AND best_of: 2
#   A2. `claude -p "/chipgraph:plan timer"`: planner writes the plan
#   H3. The person approves the plan
#   B2. `claude -p "/chipgraph:rtl timer"`: RTL with best-of-2 on rtl_module
#   H4. The reviewer approves the PR
#   R2. report.py OUT_DIR/p2 --phase 2: the phase 2 verdict (checks best-of candidates)
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-isolation-run}"
MAIN_MODEL="${MAIN_MODEL:-sonnet}"
MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
PLAN_TURNS="${PLAN_TURNS:-40}"
RTL_TURNS="${RTL_TURNS:-80}"

# OUT_DIR is deleted first: accept only /tmp/cg-isolation-<name>
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-isolation-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-isolation-<name>)" >&2
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

py() { (cd "$REPO" && uv run python "$@"); }

# Tools the sessions may use (pre-approval only).
MCP=mcp__plugin_chipgraph_chipgraph__
TOOLS="${MCP}next_task ${MCP}get_context ${MCP}submit ${MCP}plan_show ${MCP}status"
TOOLS="$TOOLS Agent Read Glob Grep Write Edit MultiEdit"

session() {  # session <phase_dir> <name> <prompt> <max turns>
  local phase_dir="$1" name="$2" prompt="$3" turns="$4"
  local proj="$phase_dir/tinysoc"
  local start end
  start=$(date +%s)
  (cd "$proj" && env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY \
    -u OTEL_METRICS_EXPORTER -u OTEL_LOGS_EXPORTER \
    claude -p "$prompt" \
      --model "$MAIN_MODEL" \
      --max-turns "$turns" \
      --plugin-dir "$OUT/plugin" \
      --allowedTools "$TOOLS" \
      --permission-mode acceptEdits \
      --output-format stream-json --verbose \
      > "$phase_dir/$name.jsonl" 2> "$phase_dir/$name.stderr.log") || echo "claude exited $?" >> "$phase_dir/$name.stderr.log"
  end=$(date +%s)
  echo "${name}_s=$((end - start))" >> "$phase_dir/timing.txt"
}

cg() {  # cg <proj> <args...>
  local proj="$1"
  shift
  (cd "$REPO" && uv run chipgraph -C "$proj" "$@")
}

run_phase() {  # run_phase <phase_num> <phase_dir> [--best-of]
  local phase="$1" phase_dir="$2" best_of_flag="${3:-}"
  local proj="$phase_dir/tinysoc"

  echo "=== PHASE $phase: ${best_of_flag:+BEST-OF-2}${best_of_flag:-ISOLATION ONLY} ==="

  # Build the fixture
  mkdir -p "$phase_dir"
  py "$HERE/fixture.py" "$proj" --origin "$phase_dir/origin.git" \
    --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL" \
    $best_of_flag \
    > /dev/null 2> "$phase_dir/fixture.log"

  # A. /chipgraph:plan, headless: it stops at the gate.
  session "$phase_dir" plan "/chipgraph:plan timer" "$PLAN_TURNS"

  # H1. The person reads the plan and approves it.
  cg "$proj" --json plan show timer > "$phase_dir/plan_show.json" 2> "$phase_dir/plan_show.stderr.log" || true

  # Check plan sanity before approving
  if py "$HERE/report.py" --check-plan "$phase_dir/plan_show.json" > "$phase_dir/plan_check.txt" 2>&1; then
    INSTANCE="digital-rtl/plan_expand[block=timer]"
    cg "$proj" approve plan:timer --instance "$INSTANCE" --by e2e-human \
      --note "approved in isolation acceptance run phase $phase" \
      > "$phase_dir/approve_plan.txt" 2>&1 || true

    # B. /chipgraph:rtl: the modules (isolated, optionally with best-of-2), the testbench, the Critic.
    session "$phase_dir" rtl "/chipgraph:rtl timer" "$RTL_TURNS"

    # H2. The reviewer.
    cg "$proj" approve pr:timer --instance "digital-rtl/pr_gate[block=timer]" --by e2e-reviewer \
      --note "branch read" > "$phase_dir/approve_pr.txt" 2>&1 || true
    set +e
    cg "$proj" rtl timer > "$phase_dir/final.txt" 2>&1
    echo "$?" > "$phase_dir/final.exit"
    set -e
  else
    echo "the plan was not sane: not approved, rtl phase skipped" >> "$phase_dir/plan_check.txt"
  fi

  # Record phase timing
  echo "phase${phase}_total_s=$(( $(date +%s) - T_PHASE ))" >> "$phase_dir/timing.txt"
}

T0=$(date +%s)

# ---- PHASE 1: ISOLATION ONLY ----
T_PHASE=$(date +%s)
run_phase 1 "$OUT/p1"

# R1. Phase 1 verdict
echo ""
echo "=== PHASE 1 REPORT ==="
py "$HERE/report.py" "$OUT/p1" --phase 1 || P1_FAIL=1

# ---- PHASE 2: BEST-OF-2 ----
T_PHASE=$(date +%s)
run_phase 2 "$OUT/p2" "--best-of"

# R2. Phase 2 verdict
echo ""
echo "=== PHASE 2 REPORT ==="
py "$HERE/report.py" "$OUT/p2" --phase 2 || P2_FAIL=1

echo "total_s=$(( $(date +%s) - T0 ))" >> "$OUT/timing.txt"

# Final summary
echo ""
echo "=== FINAL SUMMARY ==="
echo "Phase 1 (isolation): ${P1_FAIL:+FAIL}${P1_FAIL:-PASS}"
echo "Phase 2 (best-of-2): ${P2_FAIL:+FAIL}${P2_FAIL:-PASS}"

# Calculate total cost
cost1=$(py -c "import json; p='$OUT/p1/plan.jsonl'; r=sum(json.loads(l).get('total_cost_usd',0) for l in open(p) if l.strip().startswith('{') and 'total_cost_usd' in l) if __import__('os').path.isfile(p) else 0; print(r)" 2>/dev/null || echo 0)
cost2=$(py -c "import json; p='$OUT/p2/plan.jsonl'; r=sum(json.loads(l).get('total_cost_usd',0) for l in open(p) if l.strip().startswith('{') and 'total_cost_usd' in l) if __import__('os').path.isfile(p) else 0; print(r)" 2>/dev/null || echo 0)
# Note: actual cost calculation from report.py is more accurate; this is just an estimate

if [ -n "${P1_FAIL:-}" ] || [ -n "${P2_FAIL:-}" ]; then
  exit 1
fi
