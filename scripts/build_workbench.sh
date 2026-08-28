#!/usr/bin/env bash
# Build the `hiveloom-workbench` npm package: frontend first, then the tarball.
#
#   scripts/build_workbench.sh [--check]
#
#   --check   also install the tarball and launch it against this checkout,
#             verifying it serves the UI and reaches its own Python API.
#
# The package is one artifact carrying three things: the compiled UI, the Python
# API (`server.py` plus the copilot harness), and the Node launcher that puts
# them in front of the user on a single port. Shipping them together is what
# makes a version mismatch between UI and API impossible — they are the same
# release by construction.
#
# Node 22+ is required to build. A *user* needs Node only to run `npx`, and a
# Python with hiveloom for the API, which `uv add hiveloom` already gives them.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI="$REPO/devtools/ui"
OUT="$UI/pack"
CHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

command -v node >/dev/null || { echo "error: node 22+ is required to build" >&2; exit 1; }

cd "$UI"

if [[ ! -d node_modules ]]; then
  echo "installing UI dependencies…"
  # `npm ci` in a release build: the lockfile decides, not whatever resolves today.
  npm ci
fi

echo "==> typecheck"
npm run typecheck

echo "==> unit tests"
npm test

echo "==> frontend bundle"
# Removed rather than overwritten: vite leaves content-hashed assets behind, and
# a package carrying three generations of index-*.js is both bigger and a puzzle
# to debug.
rm -rf web
npm run build

echo "==> tarball"
rm -rf "$OUT"; mkdir -p "$OUT"
# `prepack` would rebuild the frontend; it has just been built deliberately, and
# the guards below check *this* bundle.
npm pack --ignore-scripts --pack-destination "$OUT" >/dev/null
TARBALL="$(ls "$OUT"/*.tgz)"

echo "==> contents"
contents() { tar tzf "$TARBALL" | sed 's|^package/||'; }
require()     { contents | grep -q "$1" || { echo "error: $2 missing from the package" >&2; exit 1; }; }
refuse()      { contents | grep -q "$1" && { echo "error: $2 leaked into the package" >&2; exit 1; }; return 0; }
require '^server.py$'          "the Python API (server.py)"
require '^copilot/harness.yaml$' "the bundled copilot harness"
require '^web/index.html$'     "the compiled interface"
require '^bin/cli.mjs$'        "the launcher"
refuse  '^src/'                "TypeScript source"
# `vite.config.ts` must never ship: server.py treats its presence as proof of a
# source checkout and would then write its state into the installed package.
refuse  '^vite.config.ts$'     "the build config"
refuse  'node_modules'         "node_modules"
refuse  '\.hiveloom/'          "local workbench state"
echo "    $(basename "$TARBALL") — $(contents | wc -l) files, $(du -h "$TARBALL" | cut -f1)"

if [[ "$CHECK" == 1 ]]; then
  echo "==> install check"
  STAGE="$(mktemp -d)"
  npm install --prefix "$STAGE" "$TARBALL" >/dev/null 2>&1
  CLI="$STAGE/node_modules/.bin/hiveloom-workbench"
  # A high, unprivileged port picked at random so parallel CI jobs on one
  # runner do not collide. Kept inside the valid range, which "8${RANDOM:0:3}9"
  # was not.
  PORT=$(( 20000 + RANDOM % 20000 ))
  HOME_DIR="$(mktemp -d)"
  # Run it against this checkout's interpreter, which has hiveloom.
  HIVELOOM_HOME="$HOME_DIR" "$CLI" --port "$PORT" --no-open \
    --python "$REPO/.venv/bin/python" >"$STAGE/run.log" 2>&1 &
  CLI_PID=$!
  trap 'kill "$CLI_PID" 2>/dev/null || true' EXIT
  for _ in $(seq 1 120); do
    curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null \
    || { echo "error: the launcher never became healthy"; cat "$STAGE/run.log"; exit 1; }
  curl -sf "http://127.0.0.1:$PORT/" | grep -q '<div id="root">' \
    || { echo "error: the launcher does not serve the interface" >&2; exit 1; }
  # The package must not be written into: it is disposable and shared.
  if find "$STAGE/node_modules/hiveloom-workbench" -name '*.db' -o -name '.hiveloom' | grep -q .; then
    echo "error: the workbench wrote state into its own package" >&2; exit 1
  fi
  kill "$CLI_PID" 2>/dev/null || true
  echo "    installed, served the interface, reached its API, wrote nothing into itself"
fi

echo
echo "built: $TARBALL"
