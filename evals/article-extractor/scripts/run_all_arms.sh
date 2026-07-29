#!/usr/bin/env bash
# Run all four arms. Env overrides: EPOCHS (default 3), SAMPLES (limit, for pilots).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }

./scripts/setup_harnesses.sh

EPOCHS="${EPOCHS:-3}"
LIMIT_FLAG=""
[ -n "${SAMPLES:-}" ] && LIMIT_FLAG="--limit ${SAMPLES}"

uv run python dataset/check_dataset_urls.py

run() { uv run inspect eval "$@" --epochs "$EPOCHS" $LIMIT_FLAG --display plain; }

run inspect_evals/task_harness.py@article_extractor_harness \
    -T harness_dir=harnesses/harness-haiku --max-connections 5 --log-dir logs/haiku_harness

run inspect_evals/task_raw.py@article_extractor_raw \
    --model anthropic/claude-haiku-4-5 --max-connections 5 --log-dir logs/haiku_raw

run inspect_evals/task_raw.py@article_extractor_raw \
    --model anthropic/claude-sonnet-5 --max-connections 5 --log-dir logs/sonnet_raw

# Warm the local model first; Ollama serializes inference and hiveloom's
# openai_compat provider has no retry on cold-load timeouts.
ollama run qwen3:4b-instruct "ping" >/dev/null 2>&1 || echo "WARN: could not warm qwen via ollama"
run inspect_evals/task_harness.py@article_extractor_harness \
    -T harness_dir=harnesses/harness-qwen --max-connections 1 --log-dir logs/qwen_harness

uv run python dataset/check_dataset_urls.py
uv run python scripts/aggregate_results.py logs/haiku_harness logs/haiku_raw logs/sonnet_raw logs/qwen_harness --out RESULTS.md
echo "Done — see RESULTS.md"
