#!/usr/bin/env bash
set -euo pipefail

CDP_PORT="${AGENT_BROWSER_CDP_PORT:-18801}"
PROFILE_DIR="${AGENT_BROWSER_BOSS_PROFILE_DIR:-${HOME}/.openclaw/browser/boss-main/user-data}"
START_URL="${AGENT_BROWSER_BOSS_START_URL:-https://www.zhipin.com/web/geek/jobs?city=101280600&jobType=1901&salary=405&experience=104&degree=203&scale=303,304,305,306}"
HEADLESS="${AGENT_BROWSER_HEADLESS:-0}"
FORCE_RELAUNCH="${AGENT_BROWSER_FORCE_RELAUNCH:-0}"
CDP_URL="http://127.0.0.1:${CDP_PORT}/json/version"
LIST_URL="http://127.0.0.1:${CDP_PORT}/json/list"

find_chrome_bin() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local mac_bin="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if [[ -x "$mac_bin" ]]; then
      echo "$mac_bin"
      return 0
    fi
    return 1
  fi

  for bin in google-chrome google-chrome-stable chromium-browser chromium; do
    if command -v "$bin" >/dev/null 2>&1; then
      command -v "$bin"
      return 0
    fi
  done
  return 1
}

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

kill_profile_processes() {
  # If another Chrome instance is using the same profile on a different
  # remote-debugging port, a new launch will fail due to profile lock.
  local pids
  pids="$(ps ax -o pid= -o command= | awk -v profile="--user-data-dir=$PROFILE_DIR" 'index($0, profile) > 0 {print $1}')"
  if [[ -n "$pids" ]]; then
    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 1
  fi
}

wait_ready() {
  for _ in {1..20}; do
    if curl -sf "$CDP_URL" >/dev/null && has_page; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

open_tab() {
  local url="$1"

  if curl -sf -X PUT "http://127.0.0.1:${CDP_PORT}/json/new?${url}" >/dev/null; then
    return 0
  fi
  if curl -sf "http://127.0.0.1:${CDP_PORT}/json/new?${url}" >/dev/null; then
    return 0
  fi

  if [[ "$(uname -s)" == "Darwin" ]]; then
    open -a "Google Chrome" "$url"
    return 0
  fi

  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  else
    "$CHROME_BIN" "$url" >/dev/null 2>&1 &
  fi
}

CHROME_BIN="$(find_chrome_bin || true)"
if [[ -z "$CHROME_BIN" ]]; then
  echo "Google Chrome/Chromium not found. Install Chrome first." >&2
  exit 1
fi

# Detect whether running instance is headless via User-Agent in /json/version.
current_mode() {
  local ua
  ua="$(curl -sf "$CDP_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("User-Agent",""))' 2>/dev/null || true)"
  if [[ "$ua" == *"HeadlessChrome"* ]]; then
    echo "headless"
  else
    echo "headed"
  fi
}

mode_matches() {
  local cur
  cur="$(current_mode)"
  if [[ "$HEADLESS" == "1" && "$cur" == "headless" ]]; then
    return 0
  fi
  if [[ "$HEADLESS" != "1" && "$cur" == "headed" ]]; then
    return 0
  fi
  return 1
}

if curl -sf "$CDP_URL" >/dev/null; then
  if [[ "$FORCE_RELAUNCH" == "1" ]]; then
    kill_profile_processes
    kill $(lsof -ti :"$CDP_PORT" 2>/dev/null) 2>/dev/null || true
    sleep 1
  elif ! mode_matches; then
    echo "Mode mismatch on :$CDP_PORT (want $([ "$HEADLESS" == "1" ] && echo headless || echo headed), got $(current_mode)). Relaunching..."
    kill_profile_processes
    kill $(lsof -ti :"$CDP_PORT" 2>/dev/null) 2>/dev/null || true
    sleep 1
  else
    if has_page; then
      echo "Chrome CDP already ready on :$CDP_PORT (page attached)"
      exit 0
    fi
    open_tab "$START_URL"
    if wait_ready; then
      echo "Chrome CDP ready on :$CDP_PORT (page created)"
      exit 0
    fi
    echo "CDP is reachable but failed to create an attached page on :$CDP_PORT" >&2
    exit 2
  fi
fi

# Ensure no stale Chrome is holding this profile before new launch.
kill_profile_processes

HEADLESS_FLAGS=()
if [[ "$HEADLESS" == "1" ]]; then
  HEADLESS_FLAGS=(--headless=new --disable-gpu --window-size=1920,1080)
fi

if [[ "$HEADLESS" == "1" ]]; then
  "$CHROME_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-allow-origins="*" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    "${HEADLESS_FLAGS[@]}" \
    "$START_URL" >/dev/null 2>&1 &
elif [[ "$(uname -s)" == "Darwin" ]]; then
  # Use Chrome binary directly; `open -na` truncates URLs at '&'
  "$CHROME_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-allow-origins="*" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    "$START_URL" >/dev/null 2>&1 &
else
  "$CHROME_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-allow-origins="*" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    "$START_URL" >/dev/null 2>&1 &
fi

if wait_ready; then
  echo "Chrome CDP is ready on :$CDP_PORT (page attached)"
  exit 0
fi

echo "Failed to start Chrome CDP on :$CDP_PORT" >&2
exit 2
