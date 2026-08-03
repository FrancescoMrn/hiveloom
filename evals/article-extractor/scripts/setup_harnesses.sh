#!/usr/bin/env bash
# Build the per-arm harness dirs from the canonical harness at the repo root.
# Only the model block differs per arm, so the dirs are generated (and
# gitignored) rather than committed as ~700-line duplicates that could drift.
# Existing dirs are left alone: they accumulate run traces.
#
# Every arm sets its model block explicitly, including the one that matches the
# canonical harness: the canonical one ships a first-party `claude` model (the
# only provider registered without ~/.hiveloom/models.yaml, so CI can validate
# it), and an arm's model must not silently follow a change made there.
set -euo pipefail
cd "$(dirname "$0")/.."

CANONICAL=../../harnesses/article-extractor
HIVELOOM="${HIVELOOM_BIN:-../../.venv/bin/hiveloom}"
# Shared with run_all_arms.sh (which exports both): the eval measures the
# scaffolding delta, so the token budget must stay matched across arms —
# one definition, one override point.
MAX_TOKENS="${MAX_TOKENS:-4096}"
MLX_MODEL_ID="${MLX_MODEL_ID:-mlx-community/Qwen3.6-35B-A3B-8bit}"

# The per-arm dirs are gitignored, so on a fresh clone this parent does not
# exist yet and `cp -R` into it fails.
mkdir -p harnesses

make_arm() {
  local dir="harnesses/$1"
  if [ -d "$dir" ]; then echo "$dir exists, skipping"; return; fi
  cp -R "$CANONICAL" "$dir"
  rm -rf "$dir/.hiveloom"
  "$HIVELOOM" trust "$dir" --json >/dev/null
  "$HIVELOOM" set model "$2" --dir "$dir" --json >/dev/null
  echo "built $dir"
}

make_arm harness-qwen '{"provider": "ollama", "id": "qwen3:4b-instruct", "max_tokens": '"$MAX_TOKENS"', "temperature": 0.0}'
make_arm harness-haiku '{"provider": "claude", "id": "claude-haiku-4-5", "max_tokens": '"$MAX_TOKENS"', "temperature": 0.0}'
make_arm harness-gemma '{"provider": "ollama", "id": "gemma4:12b-mlx", "max_tokens": '"$MAX_TOKENS"', "temperature": 0.0}'
make_arm harness-qwen35 '{"provider": "mlx", "id": "'"$MLX_MODEL_ID"'", "max_tokens": '"$MAX_TOKENS"', "temperature": 0.0}'
