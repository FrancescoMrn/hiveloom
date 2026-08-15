#!/usr/bin/env bash
# Build the page-audit arms (exhaustiveness + arithmetic probe) from the
# shipped article-extractor harness. Same fetch tool, guardrails, and loop;
# only prompt, schema, and validators change — all via the hiveloom CLI.
set -euo pipefail
cd "$(dirname "$0")/.."

CANONICAL=../../harnesses/article-extractor
HIVELOOM="${HIVELOOM_BIN:-../../.venv/bin/hiveloom}"
MAX_TOKENS="${MAX_TOKENS:-16000}"

mkdir -p harnesses

echo "building canonical page-audit harness"
rm -rf harnesses/_canonical
cp -R "$CANONICAL" harnesses/_canonical
rm -rf harnesses/_canonical/.hiveloom
"$HIVELOOM" trust harnesses/_canonical --json >/dev/null
"$HIVELOOM" set name page-audit --dir harnesses/_canonical --json >/dev/null
# NB: no ': ' in the value — `set` parses it as a YAML scalar.
"$HIVELOOM" set description "Audit one web page - total H2 count, exhaustive verbatim H2 list, published date, and exact day count to 2026-01-01." --dir harnesses/_canonical --json >/dev/null
"$HIVELOOM" set system_prompt --file prompts/system_prompt.txt --dir harnesses/_canonical --json >/dev/null
cp schemas-src/output.json harnesses/_canonical/schemas/output.json
"$HIVELOOM" remove "validators/article_on_page.py:validate" --dir harnesses/_canonical --json >/dev/null
rm -f harnesses/_canonical/validators/article_on_page.py
cp validators-src/page_audit.py harnesses/_canonical/validators/page_audit.py
"$HIVELOOM" add validator --code "validators/page_audit.py:validate" \
  --description "h2 count and list must match the live page exactly, headings verbatim, published_date must exist on the page, days_to_2026 recomputed in code." \
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
    "$HIVELOOM" remove "validators/page_audit.py:validate" --dir "$dir" --json >/dev/null
    "$HIVELOOM" set loop.require_verification false --dir "$dir" --json >/dev/null
  fi
  "$HIVELOOM" validate "$dir" --json >/dev/null
  echo "built $dir"
}

make_arm opus-harness   claude/claude-opus-5
make_arm opus-raw       claude/claude-opus-5 raw
make_arm sonnet-harness claude/claude-sonnet-5
make_arm sonnet-raw     claude/claude-sonnet-5 raw

echo "all arms ready"
