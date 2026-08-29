#!/usr/bin/env bash
# Run the hiveloom workbench in development: API + Vite, hot reload on both.
#
# This is the contributor's mode. Users install `hiveloom-workbench`, whose
# server carries the compiled frontend and serves it on one port. Here Vite owns
# the browser origin instead and proxies /api to the Python process, so an edit
# to either half reloads without a restart.
#
#   devtools/ui/dev.sh [--dir HARNESS ...] [--scan-dir ROOT ...] [--host 0.0.0.0]
#
# Harnesses come from the registry and a recursive scan of this checkout's
# `harnesses/` directory. --dir adds one harness for this process;
# --scan-dir adds another recursively discovered tree.
#
#   --host 0.0.0.0   bind both servers on every interface. Needed whenever the
#                    browser is not on this machine — a container, a VM, a
#                    remote box — because the 127.0.0.1 default is reachable
#                    only from inside it. Equivalent: HIVELOOM_UI_HOST=0.0.0.0.
#
# Ports: HIVELOOM_UI_PORT (default 5173) and HIVELOOM_UI_API_PORT (default
# 8770). Both are checked before anything starts, so a port already taken by
# another stack is a clear message rather than a server that dies quietly.
#
# Credentials are inherited: a harness runs inside the API process, so whatever
# its spec's provider needs (ANTHROPIC_API_KEY, …) must be exported here, or
# live in the harness's own .env, which hiveloom loads.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UI="$REPO/devtools/ui"
cd "$REPO"

API_PORT="${HIVELOOM_UI_API_PORT:-8770}"
WEB_PORT="${HIVELOOM_UI_PORT:-5173}"
HOST="${HIVELOOM_UI_HOST:-127.0.0.1}"

DIRS=(--scan-dir "$REPO/harnesses")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIRS+=(--dir "$2"); shift 2 ;;
    --scan-dir) DIRS+=(--scan-dir "$2"); shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# --------------------------------------------------------------------- #
# Fail early and legibly on the two things that actually go wrong.
# --------------------------------------------------------------------- #
port_taken() {
  # `ss` is not everywhere; a bind attempt answers the question directly and
  # needs nothing installed.
  uv run python - "$1" "$2" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
probe = socket.socket()
probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    probe.close()
PY
}

for spec in "API:$API_PORT:HIVELOOM_UI_API_PORT" "UI:$WEB_PORT:HIVELOOM_UI_PORT"; do
  IFS=: read -r what port var <<<"$spec"
  if ! port_taken "$HOST" "$port"; then
    echo "error: $what port $port on $HOST is already in use." >&2
    echo "       Free it, or pick another: $var=<port> devtools/ui/dev.sh" >&2
    command -v ss >/dev/null && ss -ltnp 2>/dev/null | grep ":$port " >&2 || true
    exit 1
  fi
done

if [[ ! -d "$UI/node_modules" ]]; then
  echo "installing UI dependencies (first run only)…"
  (cd "$UI" && npm install)
fi

# --------------------------------------------------------------------- #
# Both processes, tied lifetimes.
# --------------------------------------------------------------------- #
uv run python devtools/ui/server.py --host "$HOST" --port "$API_PORT" "${DIRS[@]}" &
API_PID=$!
# No `exec` for Vite below: this shell has to outlive it to take the API down
# with it on exit.
trap 'kill "$API_PID" 2>/dev/null || true' EXIT

# The API is contacted over loopback regardless of what it is bound to.
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$API_PORT/api/harnesses" >/dev/null; then break; fi
  kill -0 "$API_PID" 2>/dev/null || { echo "API failed to start (see the error above)" >&2; exit 1; }
  sleep 0.25
done

echo
echo "  workbench   http://${HOST}:${WEB_PORT}"
echo "  api         http://${HOST}:${API_PORT}"
if [[ "$HOST" == "127.0.0.1" ]]; then
  echo "  (only reachable from this machine — use --host 0.0.0.0 if your browser is elsewhere)"
fi
echo

cd "$UI"
HIVELOOM_UI_API="http://127.0.0.1:$API_PORT" npx vite --host "$HOST" --port "$WEB_PORT" --strictPort
