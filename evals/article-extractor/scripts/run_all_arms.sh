#!/usr/bin/env bash
# Run all nine arms. Env overrides: EPOCHS (default 3), SAMPLES (limit, for pilots).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }
# Pin the native Ollama app (Metal, full RAM) for the raw local arms; the
# harness arms get the same pin via ~/.hiveloom/models.yaml.
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434/v1}"
# qwen3.6-35B is served by mlx_lm.server (see ~/.hiveloom/models.yaml).
export MLX_BASE_URL="${MLX_BASE_URL:-http://127.0.0.1:8081/v1}"
export MLX_API_KEY="${MLX_API_KEY:-mlx}"
export MLX_MODEL_ID="${MLX_MODEL_ID:-mlx-community/Qwen3.6-35B-A3B-8bit}"
# One token budget for every arm — raw flags here, harness configs in
# setup_harnesses.sh (which inherits this export). The eval measures the
# scaffolding delta, so budgets must stay matched. Explicit rather than server
# defaults: mlx_lm.server caps at ~512, which a reasoning model spends
# entirely on thinking before emitting any JSON.
export MAX_TOKENS="${MAX_TOKENS:-4096}"

./scripts/setup_harnesses.sh

EPOCHS="${EPOCHS:-3}"
LIMIT_FLAG=""
[ -n "${SAMPLES:-}" ] && LIMIT_FLAG="--limit ${SAMPLES}"

uv run python dataset/check_dataset_urls.py

# Each arm registers its log dir here; the aggregate call consumes the list.
LOG_DIRS=()
run() { uv run inspect eval "$@" --epochs "$EPOCHS" $LIMIT_FLAG --display plain; }
harness() { LOG_DIRS+=("logs/$2"); run inspect_evals/task_harness.py@article_extractor_harness -T "harness_dir=harnesses/$1" --max-connections "$3" --log-dir "logs/$2"; }
raw() { LOG_DIRS+=("logs/$2"); run inspect_evals/task_raw.py@article_extractor_raw --model "$1" --max-tokens "$MAX_TOKENS" --max-connections "$3" --log-dir "logs/$2"; }
warm() { ollama run "$1" "ping" >/dev/null 2>&1 || echo "WARN: could not warm $1"; }

# Cloud arms.
harness harness-haiku haiku_harness 5
raw anthropic/claude-haiku-4-5 haiku_raw 5
raw anthropic/claude-sonnet-5 sonnet_raw 5

# Local arms: warm each model first (Ollama serializes inference and
# hiveloom's openai_compat provider has no retry on cold-load timeouts).
warm qwen3:4b-instruct
harness harness-qwen qwen_harness 1
raw ollama/qwen3:4b-instruct qwen_raw 1

warm gemma4:12b-mlx
harness harness-gemma gemma_harness 1
raw ollama/gemma4:12b-mlx gemma_raw 1

# Warm the mlx server (loads ~37GB into memory on first request). Hard-fail:
# unlike Ollama (auto-loads per request), a dead mlx server would make inspect
# retry-with-backoff through two whole arms instead of failing fast.
curl -sf --max-time 600 "$MLX_BASE_URL/chat/completions" -H "Content-Type: application/json" \
  -d '{"model": "'"$MLX_MODEL_ID"'", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}' >/dev/null \
  || { echo "ERROR: mlx server not reachable at $MLX_BASE_URL — start mlx_lm.server first" >&2; exit 1; }
harness harness-qwen35 qwen35_harness 1
raw "openai-api/mlx/$MLX_MODEL_ID" qwen35_raw 1

uv run python dataset/check_dataset_urls.py
uv run python scripts/aggregate_results.py "${LOG_DIRS[@]}" --out RESULTS.md
echo "Done — see RESULTS.md"
