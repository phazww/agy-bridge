#!/usr/bin/env bash
# Reproducible ~20s demo: agy-bridge warns that a port from .env is held by an unrelated process.
# No Docker needed. Creates everything in a temp dir and removes it (and the process) on exit.
#   scripts/demo-port-warning.sh            # port 3000
#   DEMO_PORT=3005 scripts/demo-port-warning.sh
#   DEMO_PAUSE=0 scripts/demo-port-warning.sh   # no pauses (CI / tests)
set -euo pipefail

PORT="${DEMO_PORT:-3000}"
PAUSE="${DEMO_PAUSE:-1.2}"
BRIDGE="$(cd "$(dirname "$0")/.." && pwd)/agy-bridge"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already busy on this machine. Run with DEMO_PORT=<free port>." >&2
  exit 1
fi

ROOT="$(mktemp -d)"
SRV_PID=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  [ -n "$SRV_PID" ] && wait "$SRV_PID" 2>/dev/null || true
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM

step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; sleep "$PAUSE"; }
run()  { printf '\033[2m$ %s\033[0m\n' "$1"; }

step "1/3  A project that needs port $PORT"
mkdir -p "$ROOT/my-app" "$ROOT/elsewhere"
cd "$ROOT/my-app"
git init -q -b main
git -c user.name=demo -c user.email=demo@example.com commit -q --allow-empty -m init
printf 'PORT=%s\n' "$PORT" > .env
run "cat .env"
cat .env

step "2/3  Something unrelated grabs $PORT (started outside the repo)"
run "cd ../elsewhere && python3 -m http.server $PORT &"
(cd "$ROOT/elsewhere" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SRV_PID=$!
for _ in $(seq 1 30); do
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
  sleep 0.1
done

step "3/3  agy-bridge sync  →  what the next agent sees"
run "agy-bridge sync"
AGY_BRIDGE_NO_DOCKER=1 "$BRIDGE" sync "$ROOT/my-app" --quiet >/dev/null 2>&1 || true
LINE="$(grep -F "Port \`$PORT\` needed by this project" .agy-bridge/LIVE_STATE.md || true)"
if [ -z "$LINE" ]; then
  echo "✗ warning line not found in LIVE_STATE" >&2
  exit 1
fi
printf '%s\n' "$LINE" | sed -e 's/\*\*//g' -e 's/`//g' -e 's/^- //'
sleep "$PAUSE"
printf '\n\033[2m(cleaned up: server stopped, temp repo removed)\033[0m\n'
