#!/usr/bin/env bash
# Build the article-digest arms from the shipped article-extractor harness.
# The digest task reuses the same fetch_clean tool, guardrails, and loop
# policy; only the prompt, schema, and validators change — all applied through
# the hiveloom CLI (never by hand-editing harness.yaml).
#
# Arms (all Claude API):
#   opus-harness / opus-raw       claude-opus-5
#   sonnet-harness / sonnet-raw   claude-sonnet-5
# "raw" = identical harness with the validators removed and verification off,
# so the measured delta is validators + retry-with-feedback only.
set -euo pipefail
cd "$(dirname "$0")/.."

CANONICAL=../../harnesses/article-extractor
HIVELOOM="${HIVELOOM_BIN:-../../.venv/bin/hiveloom}"
# Opus 5 / Sonnet 5 run adaptive thinking by default and max_tokens caps
# thinking + response together, so give generous headroom.
MAX_TOKENS="${MAX_TOKENS:-16000}"

mkdir -p harnesses

echo "building canonical digest harness"
rm -rf harnesses/_canonical
cp -R "$CANONICAL" harnesses/_canonical
rm -rf harnesses/_canonical/.hiveloom
"$HIVELOOM" trust harnesses/_canonical --json >/dev/null
"$HIVELOOM" set name article-digest --dir harnesses/_canonical --json >/dev/null
# NB: no ': ' in the value — `set` parses it as a YAML scalar.
"$HIVELOOM" set description "Fetch one article page and produce a structured digest of it - an original-prose summary, five verbatim quotes, and a heading outline." --dir harnesses/_canonical --json >/dev/null
"$HIVELOOM" set system_prompt --file prompts/system_prompt.txt --dir harnesses/_canonical --json >/dev/null
cp schemas-src/output.json harnesses/_canonical/schemas/output.json
"$HIVELOOM" remove "validators/article_on_page.py:validate" --dir harnesses/_canonical --json >/dev/null
rm -f harnesses/_canonical/validators/article_on_page.py
cp validators-src/digest_on_page.py harnesses/_canonical/validators/digest_on_page.py
"$HIVELOOM" add validator --code "validators/digest_on_page.py:validate" \
  --description "source_url must equal the run input; quotes and outline must appear verbatim on the live page; summary must be original prose in the required length band." \
  --dir harnesses/_canonical --json >/dev/null
"$HIVELOOM" validate harnesses/_canonical --json >/dev/null
echo "canonical OK"

make_arm() {
  local dir="harnesses/$1" model="$2" raw="${3:-}"
  if [ -d "$dir" ]; then echo "$dir exists, skipping"; return; fi
  cp -R harnesses/_canonical "$dir"
  rm -rf "$dir/.hiveloom"
  "$HIVELOOM" trust "$dir" --json >/dev/null
  "$HIVELOOM" set model "$model" --dir "$dir" --json >/dev/null
  "$HIVELOOM" set model.max_tokens "$MAX_TOKENS" --dir "$dir" --json >/dev/null
  if [ -n "$raw" ]; then
    "$HIVELOOM" remove output_schema --dir "$dir" --json >/dev/null
    "$HIVELOOM" remove "validators/digest_on_page.py:validate" --dir "$dir" --json >/dev/null
    "$HIVELOOM" set loop.require_verification false --dir "$dir" --json >/dev/null
  fi
  "$HIVELOOM" validate "$dir" --json >/dev/null
  echo "built $dir"
}

# Temperature stays at the spec default; the claude provider omits it on the
# wire for these models (Opus 5 rejects sampling params outright and Sonnet 5
# rejects non-default values).
make_arm opus-harness   claude/claude-opus-5
make_arm opus-raw       claude/claude-opus-5 raw
make_arm sonnet-harness claude/claude-sonnet-5
make_arm sonnet-raw     claude/claude-sonnet-5 raw

echo "all arms ready"
