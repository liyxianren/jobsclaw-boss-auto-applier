#!/usr/bin/env bash
set -euo pipefail

# Force all agent-browser commands to use BOSS直聘 CDP endpoint.
#
# IMPORTANT: Do NOT use --session. The --session flag creates an isolated
# browser context with its own cookies, which means login state from the
# Chrome profile is NOT available. By omitting --session, agent-browser
# operates in the default context and shares cookies with the Chrome profile.
#
# After each command, the agent-browser daemon is killed to prevent it from
# continuously polling/refreshing the page (which triggers BOSS risk control).
#
# Usage:
#   bash ab_boss.sh open "https://www.zhipin.com/web/geek/job"
#   bash ab_boss.sh snapshot -i --json
#   bash ab_boss.sh eval "document.title"

CDP_PORT="${AGENT_BROWSER_CDP_PORT:-18801}"
CDP_URL="http://127.0.0.1:${CDP_PORT}/json/version"
LIST_URL="http://127.0.0.1:${CDP_PORT}/json/list"

has_page() {
  curl -sf "$LIST_URL" | python3 -c '
import json
import sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if any(item.get("type") == "page" for item in data) else 1)
'
}

ensure_page() {
  if has_page; then
    return 0
  fi
  # CDP can be alive with zero tabs; create a blank tab so agent-browser can attach.
  if curl -sf -X PUT "http://127.0.0.1:${CDP_PORT}/json/new?about:blank" >/dev/null; then
    :
  elif curl -sf "http://127.0.0.1:${CDP_PORT}/json/new?about:blank" >/dev/null; then
    :
  else
    return 1
  fi
  for _ in {1..12}; do
    if has_page; then
      return 0
    fi
    sleep 0.4
  done
  return 1
}

cleanup_daemon() {
  # Kill agent-browser daemon to stop it from polling/refreshing the page.
  pkill -f "agent-browser.*daemon" 2>/dev/null || true
}
trap cleanup_daemon EXIT

if [[ $# -eq 0 ]]; then
  echo "Usage: bash ab_boss.sh <agent-browser-subcommand> [args...]" >&2
  exit 1
fi

if ! curl -sf "$CDP_URL" >/dev/null; then
  cat >&2 <<EOF2
CDP endpoint is not reachable: $CDP_URL
Start Chrome with:
  bash ./start_boss_chrome.sh
EOF2
  exit 2
fi

if ! ensure_page; then
  echo "CDP is reachable but no page is attached. Open a Chrome tab and retry." >&2
  exit 3
fi

# Attach to the user's existing tab (tab 0) instead of agent-browser's
# default isolated Playwright context. This ensures scripts operate on
# the same page the user sees in their browser window.
agent-browser tab 0 --cdp "$CDP_PORT" 2>/dev/null || true

# Only inject --cdp if caller didn't already pass it.
HAS_CDP=0
for arg in "$@"; do
  if [[ "$arg" == "--cdp" ]]; then
    HAS_CDP=1
    break
  fi
done

if [[ "$HAS_CDP" -eq 1 ]]; then
  agent-browser "$@"
else
  agent-browser "$@" --cdp "$CDP_PORT"
fi
