#!/usr/bin/env bash
# Claude Code hook -> Discord bridge.
# Usage: notify.sh <event-name>
# Reads hook JSON from stdin (per Claude Code hook spec), extracts useful
# fields, posts to local bridge. Never blocks the hook on failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "notify.sh: jq is required; install with: sudo dnf install -y jq" >&2
  exit 0
fi

if [ -z "${BRIDGE_TOKEN:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/.env"
  set +a
fi

if [ -z "${BRIDGE_TOKEN:-}" ]; then
  echo "notify.sh: BRIDGE_TOKEN is not set; skipping bridge notification" >&2
  exit 0
fi

EVENT="${1:-notify}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT="$(basename "$PROJECT_DIR")"
AGENT="${CCB_AGENT_NAME:-${CCB_AGENT:-default}}"
PORT="${BRIDGE_PORT:-7777}"

# Capture git branch if inside a repo (or worktree). Empty otherwise.
BRANCH=""
if command -v git >/dev/null 2>&1; then
  BRANCH="$(git -C "$PROJECT_DIR" branch --show-current 2>/dev/null || true)"
  if [ -z "$BRANCH" ]; then
    BRANCH="$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || true)"
    [ -n "$BRANCH" ] && BRANCH="@$BRANCH"  # mark detached HEAD
  fi
fi

# Discover tmux pane by walking PPID chain and matching against pane_pid
# in any tmux socket (default + named sockets like 'claude-bridge').
# Works even when $TMUX/$TMUX_PANE are stripped from env.
find_tmux_pane() {
  command -v tmux >/dev/null 2>&1 || return
  local sockets=("default" "claude-bridge")
  local pane_pids="" socket pid name ppid
  for socket in "${sockets[@]}"; do
    local arg=()
    [ "$socket" != "default" ] && arg=(-L "$socket")
    local out
    out="$(tmux "${arg[@]}" list-panes -a -F '#{pane_id}|#{pane_pid}|'"$socket" 2>/dev/null)" || continue
    pane_pids+="$out"$'\n'
  done
  [ -z "$pane_pids" ] && return
  pid=$$
  while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    local match
    match="$(printf '%s' "$pane_pids" | awk -F'|' -v p="$pid" '$2 == p { if ($3 == "default") print $1 "|" $2; else print $1 "@" $3 "|" $2; exit }')"
    if [ -n "$match" ]; then
      printf '%s' "$match"
      return
    fi
    ppid="$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null)"
    [ -z "$ppid" ] && return
    pid="$ppid"
  done
}

pane_pid_for_pane() {
  command -v tmux >/dev/null 2>&1 || return
  [ -n "${1:-}" ] || return
  local pane="$1" pane_id socket arg=() out
  if [[ "$pane" == *"@"* ]]; then
    pane_id="${pane%%@*}"
    socket="${pane#*@}"
    [ "$socket" != "default" ] && arg=(-L "$socket")
  else
    pane_id="$pane"
  fi
  out="$(tmux "${arg[@]}" display-message -p -t "$pane_id" '#{pane_pid}' 2>/dev/null)" || return
  [[ "$out" =~ ^[0-9]+$ ]] && printf '%s' "$out"
}

pane_from_env() {
  [ -n "${TMUX_PANE:-}" ] || return
  local socket_path socket_name pane pane_pid
  if [ -n "${TMUX:-}" ]; then
    socket_path="${TMUX%%,*}"
    socket_name="$(basename "$socket_path")"
    if [ -n "$socket_name" ] && [ "$socket_name" != "default" ]; then
      pane="${TMUX_PANE}@${socket_name}"
      pane_pid="$(pane_pid_for_pane "$pane")"
      printf '%s|%s' "$pane" "$pane_pid"
      return
    fi
  fi
  pane="$TMUX_PANE"
  pane_pid="$(pane_pid_for_pane "$pane")"
  printf '%s|%s' "$pane" "$pane_pid"
}

PANE_INFO="$(pane_from_env)"
[ -z "$PANE_INFO" ] && PANE_INFO="$(find_tmux_pane)"
PANE=""
PANE_PID=""
if [[ "$PANE_INFO" == *"|"* ]]; then
  PANE="${PANE_INFO%%|*}"
  PANE_PID="${PANE_INFO#*|}"
else
  PANE="$PANE_INFO"
fi

INPUT=""
if [ ! -t 0 ]; then
  INPUT="$(cat)"
fi

MSG=""
TRANSCRIPT=""
if [ -n "$INPUT" ]; then
  MSG="$(printf '%s' "$INPUT" | jq -r '
    .message //
    .notification.message //
    .tool_response.error //
    empty
  ' 2>/dev/null || true)"
  TRANSCRIPT="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
fi

PAYLOAD="$(jq -n \
  --arg project    "$PROJECT" \
  --arg branch     "$BRANCH" \
  --arg agent      "$AGENT" \
  --arg event      "$EVENT" \
  --arg msg        "$MSG" \
  --arg pane       "$PANE" \
  --arg pane_pid   "$PANE_PID" \
  --arg transcript "$TRANSCRIPT" \
  '{project:$project, branch:$branch, agent:$agent, event:$event, message:$msg, tmux_pane:$pane, transcript_path:$transcript}
   + (if ($pane_pid | test("^[0-9]+$")) then {tmux_pane_pid:($pane_pid | tonumber)} else {} end)')"

curl -s --max-time 3 \
  -X POST "http://127.0.0.1:${PORT}/notify" \
  -H 'Content-Type: application/json' \
  -H "X-Bridge-Token: ${BRIDGE_TOKEN}" \
  -d "$PAYLOAD" >/dev/null 2>&1 || true

exit 0
