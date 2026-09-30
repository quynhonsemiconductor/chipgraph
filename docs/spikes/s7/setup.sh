#!/usr/bin/env bash
# S7 spike: build the throwaway project a run works on. Used by run.sh and selftest.py,
# so the self-test checks exactly the tree the real run sees.
#
#   docs/spikes/s7/setup.sh OUT_DIR
#
# Env: SUB_MODEL (default haiku). Creates OUT_DIR/tinysoc (a git repo whose first commit
# already holds .claude/, so the only changes after it are the agents' writes) and
# OUT_DIR/mcp.json.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="${1:?usage: setup.sh OUT_DIR}"
SUB_MODEL="${SUB_MODEL:-haiku}"

# Refuse anything but a fresh /tmp/s7-* (or the system temp dir) outside the repo: the
# directory is deleted first.
case "$(cd "$(dirname "$OUT")" 2>/dev/null && pwd -P)/$(basename "$OUT")" in
  /tmp/s7-*|/private/tmp/s7-*|"${TMPDIR%/}"/*|/private/var/folders/*) ;;
  *) echo "setup.sh: refusing OUT_DIR=$OUT (use /tmp/s7-<name>)" >&2; exit 2 ;;
esac
case "$OUT" in "$REPO"*) echo "setup.sh: OUT_DIR is inside the repo" >&2; exit 2 ;; esac

PROJ="$OUT/tinysoc"
rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$REPO/examples/tinysoc" "$PROJ"

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

# Commit after .claude/ exists: `submit` counts every change since this commit.
git -C "$PROJ" init -q -b main
git -C "$PROJ" add -A
git -C "$PROJ" -c user.name=s7 -c user.email=s7@example.invalid commit -qm "tinysoc copy for S7"

cat > "$OUT/mcp.json" <<EOF
{"mcpServers": {"chipgraph": {"command": "uv",
  "args": ["run", "--project", "$REPO", "python", "$HERE/s7_server.py", "--root", "$PROJ"]}}}
EOF
