#!/usr/bin/env bash
# M2-11b acceptance run: add a block from an existing MAS with Claude Code, ending in a
# green PR. On a tinysoc copy whose block `pwm` has only its MAS, headless, with the
# logged-in Claude Code account; the script plays the person at both human gates.
#
#   docs/rtl-target-claude-code/run.sh [OUT_DIR]   # OUT_DIR must be /tmp/cg-rtl-target-<name>
#
# Env: MAIN_MODEL (default sonnet: the session that runs the commands); MEDIUM_MODEL
# (default sonnet: tier medium, the RTL and testbench authors); LARGE_MODEL (default
# sonnet, or opus: tier large, the planner and the Critic); PLAN_TURNS (default 40) and
# RTL_TURNS (default 80): --max-turns of the two sessions; SANDBOX=1 (the lead only):
# `review: {use: github}` against CHIPGRAPH_SANDBOX_REPO (owner/name, the name ending in
# `-sandbox`) with the token in CHIPGRAPH_GITHUB_TOKEN; it refuses to start without both.
#
# Phases:
#   0. OUT_DIR is checked and emptied; plugin/ is copied to OUT_DIR/plugin with a
#      .mcp.json that runs this checkout (`uv run --project <repo> chipgraph`);
#      ANTHROPIC_API_KEY is unset for every `claude` call.
#   1. fixture.py builds OUT_DIR/tinysoc (block `pwm`: its MAS only; packs digital-rtl and
#      dv; runtime claude-code; checks plan_check, format, lint, naming, style, hardcode,
#      tb_static; exemplars; fingerprints; `review: {use: file}`) with the local bare repo
#      OUT_DIR/origin.git as `origin`. SANDBOX=1: the sandbox repo is `origin`, the fixture's
#      `main` is pushed there as the PR's base branch `chipgraph-e2e-base-<stamp>`.
#   A. `claude -p "/chipgraph:plan pwm"`: the planner writes the plan, plan_check checks it;
#      headless, the command cannot ask, so it prints the approve command and stops at
#      the gate (OUT_DIR/plan.jsonl).
#   H1. The person: `chipgraph plan show pwm --json` (OUT_DIR/plan_show.json), a sanity
#      check of it (report.py --check-plan), then `chipgraph approve plan:pwm --by
#      e2e-human`. A plan that is not sane is not approved: phase B is skipped.
#   B. `claude -p "/chipgraph:rtl pwm"`: the RTL authors per module, the testbench
#      author, the Critic, then open_pr builds the branch chipgraph/<run>/pwm (pushed and
#      a PR opened with SANDBOX=1) and the gate pr:pwm waits (OUT_DIR/rtl.jsonl).
#   H2. The reviewer: `chipgraph approve pr:pwm --by e2e-reviewer` (file adapter), then
#      `chipgraph rtl pwm` once more finishes the target (exit 0). SANDBOX=1: the PR is
#      checked on GitHub (it exists, it holds exactly the block's files), then closed and
#      its branch and the base branch deleted, in a trap, whatever happened.
#   R. report.py OUT_DIR: the verdict (PASS criteria in its docstring), per-task models
#      and tries, the cost and the wall time.
set -euo pipefail
unset VIRTUAL_ENV  # uv uses the checkout's own .venv

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-/tmp/cg-rtl-target-run}"
MAIN_MODEL="${MAIN_MODEL:-sonnet}"
MEDIUM_MODEL="${MEDIUM_MODEL:-sonnet}"
LARGE_MODEL="${LARGE_MODEL:-sonnet}"
PLAN_TURNS="${PLAN_TURNS:-40}"
RTL_TURNS="${RTL_TURNS:-80}"
SANDBOX="${SANDBOX:-0}"

# OUT_DIR is deleted first: accept only /tmp/cg-rtl-target-<name> (after resolving symlinks).
parent="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P || true)"
name="$(basename "$OUT")"
tmp="$(cd /tmp && pwd -P)"
if [ "$parent" != "$tmp" ] || [[ "$name" != cg-rtl-target-?* ]]; then
  echo "run.sh: refusing OUT_DIR=$OUT (use /tmp/cg-rtl-target-<name>)" >&2
  exit 2
fi
case "$LARGE_MODEL" in sonnet|opus) ;; *)
  echo "run.sh: LARGE_MODEL must be sonnet or opus, not $LARGE_MODEL" >&2; exit 2 ;;
esac
SANDBOX_ARGS=()
if [ "$SANDBOX" = "1" ]; then
  if [ -z "${CHIPGRAPH_SANDBOX_REPO:-}" ] || [ -z "${CHIPGRAPH_GITHUB_TOKEN:-}" ]; then
    echo "run.sh: SANDBOX=1 needs CHIPGRAPH_SANDBOX_REPO and CHIPGRAPH_GITHUB_TOKEN" >&2
    exit 2
  fi
  if [[ ! "$CHIPGRAPH_SANDBOX_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+-sandbox$ ]]; then
    echo "run.sh: CHIPGRAPH_SANDBOX_REPO must be owner/<name>-sandbox" >&2
    exit 2
  fi
  SANDBOX_BASE="chipgraph-e2e-base-$(date +%Y%m%d%H%M%S)"
  SANDBOX_ARGS=(--sandbox-repo "$CHIPGRAPH_SANDBOX_REPO" --sandbox-base "$SANDBOX_BASE")
elif [ "$SANDBOX" = "0" ]; then
  unset CHIPGRAPH_GITHUB_TOKEN  # offline: no token reaches chipgraph or a session
else
  echo "run.sh: SANDBOX must be 0 or 1" >&2
  exit 2
fi

OUT="$tmp/$name"
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

cg() { (cd "$REPO" && uv run chipgraph -C "$PROJ" "$@"); }
py() { (cd "$REPO" && uv run python "$@"); }

# 1. The fixture.
py "$HERE/fixture.py" "$PROJ" --origin "$OUT/origin.git" \
  --medium-model "$MEDIUM_MODEL" --large-model "$LARGE_MODEL" \
  ${SANDBOX_ARGS[@]+"${SANDBOX_ARGS[@]}"} \
  > /dev/null 2> "$OUT/fixture.log"
if [ "$SANDBOX" = "1" ]; then
  # Close the PR and delete both branches on every exit, whatever happened.
  trap 'py "$HERE/sandbox.py" cleanup "$OUT" >> "$OUT/sandbox.log" 2>&1 || true' EXIT
  py "$HERE/sandbox.py" push-base "$OUT" "$SANDBOX_BASE" >> "$OUT/sandbox.log" 2>&1
fi

# What the sessions may use without asking (pre-approval only, the commands' own
# disallowed-tools still apply). `approve` is NOT pre-approved: no model approves here.
MCP=mcp__plugin_chipgraph_chipgraph__
TOOLS="${MCP}next_task ${MCP}get_context ${MCP}submit ${MCP}plan_show ${MCP}status"
TOOLS="$TOOLS Agent Read Glob Grep Write Edit MultiEdit"

session() {  # session <name> <prompt> <max turns>
  local start end
  start=$(date +%s)
  (cd "$PROJ" && env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_ENABLE_TELEMETRY \
    -u OTEL_METRICS_EXPORTER -u OTEL_LOGS_EXPORTER \
    claude -p "$2" \
      --model "$MAIN_MODEL" \
      --max-turns "$3" \
      --plugin-dir "$OUT/plugin" \
      --allowedTools "$TOOLS" \
      --permission-mode acceptEdits \
      --output-format stream-json --verbose \
      > "$OUT/$1.jsonl" 2> "$OUT/$1.stderr.log") || echo "claude exited $?" >> "$OUT/$1.stderr.log"
  end=$(date +%s)
  echo "$1_s=$((end - start))" >> "$OUT/timing.txt"
}

T0=$(date +%s)

# A. /chipgraph:plan, headless: it stops at the gate.
session plan "/chipgraph:plan pwm" "$PLAN_TURNS"

# H1. The person reads the plan, checks it and approves it.
cg --json plan show pwm > "$OUT/plan_show.json" 2> "$OUT/plan_show.stderr.log" || true
if py "$HERE/report.py" --check-plan "$OUT/plan_show.json" > "$OUT/plan_check.txt" 2>&1; then
  INSTANCE="digital-rtl/plan_expand[block=pwm]"
  cg approve plan:pwm --instance "$INSTANCE" --by e2e-human \
    --note "read with chipgraph plan show pwm; sane (report.py --check-plan)" \
    > "$OUT/approve_plan.txt" 2>&1

  # B. /chipgraph:rtl: the modules, the testbench, the Critic, the branch or PR.
  session rtl "/chipgraph:rtl pwm" "$RTL_TURNS"

  # H2. The reviewer.
  if [ "$SANDBOX" = "1" ]; then
    py "$HERE/sandbox.py" check "$OUT" >> "$OUT/sandbox.log" 2>&1 || true
  else
    cg approve pr:pwm --instance "digital-rtl/pr_gate[block=pwm]" --by e2e-reviewer \
      --note "branch read" > "$OUT/approve_pr.txt" 2>&1 || true
    set +e
    cg rtl pwm > "$OUT/final.txt" 2>&1
    echo "$?" > "$OUT/final.exit"
    set -e
  fi
else
  echo "the plan was not sane: not approved, phase B skipped" >> "$OUT/plan_check.txt"
fi
echo "total_s=$(( $(date +%s) - T0 ))" >> "$OUT/timing.txt"

# R. The verdict.
py "$HERE/report.py" "$OUT"
