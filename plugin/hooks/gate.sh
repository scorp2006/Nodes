#!/usr/bin/env bash
# Nodes plugin, PreToolUse gate. Server up: its answer is the hook's answer. Server unreachable: block the tool in
# repos that ran `nodes init` (fail-closed, unless that repo chose --fail-mode open); in any other repo, do nothing.
port="${NODES_PORT:-8780}"
input="$(cat)"
if out="$(printf '%s' "$input" | curl -s -f --max-time 15 -X POST -H 'Content-Type: application/json' \
          --data-binary @- "http://127.0.0.1:$port/hooks/pre-tool-use")"; then
  printf '%s' "$out"
  exit 0
fi
common="$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 0
[ -f "$common/nodes.json" ] || exit 0
grep -q '"fail_mode": "open"' "$common/nodes.json" && exit 0
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Nodes server unreachable on 127.0.0.1:%s and this repo is gated fail-closed. Start it with: python -m nodes up (or re-run nodes init --fail-mode open)."}}\n' "$port"
