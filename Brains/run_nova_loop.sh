#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT_DIR/.." && pwd)"

# ── resolve Nova workspace ──────────────────────────────────────────────────
if [ -d "/root/clawd/Nova" ]; then
  NOVA_DIR="/root/clawd/Nova"
elif [ -d "$HOME/clawd/Nova" ]; then
  NOVA_DIR="$HOME/clawd/Nova"
else
  NOVA_DIR="$ROOT_DIR/Nova"
fi

mkdir -p "$NOVA_DIR" "$NOVA_DIR/RUNS"

# ── load secrets ────────────────────────────────────────────────────────────
if [ -f "$HOME/.secrets/mining/shared_api.env" ]; then
  set -a
  source "$HOME/.secrets/mining/shared_api.env"
  set +a
fi

if [ -f "$PROJECT_ROOT/bagbot_chutes_api.env" ]; then
  set -a
  source "$PROJECT_ROOT/bagbot_chutes_api.env"
  set +a
fi

# ── model IDs ───────────────────────────────────────────────────────────────
# No hardcoded model IDs. Verify availability before launch:
#   curl -sH "Authorization: Bearer $CHUTES_API_KEY" \
#     https://llm.chutes.ai/v1/models | jq '.data[].id'
#
# Override via env or CLI:
STRATEGY_MODEL="${NOVA_STRATEGY_MODEL:-}"
RESEARCH_MODEL="${NOVA_RESEARCH_MODEL:-}"

if [ -z "$STRATEGY_MODEL" ] || [ -z "$RESEARCH_MODEL" ]; then
  echo "ERROR: NOVA_STRATEGY_MODEL and NOVA_RESEARCH_MODEL must be set." >&2
  echo "Example:" >&2
  echo "  export NOVA_STRATEGY_MODEL='Qwen/Qwen3-235B-A22B'" >&2
  echo "  export NOVA_RESEARCH_MODEL='deepseek-ai/DeepSeek-V3-0324'" >&2
  exit 1
fi

# ── launch ──────────────────────────────────────────────────────────────────
cd "$ROOT_DIR"

exec python3 nova_mining_loop.py \
  --cycle-seconds 5 \
  --health-seconds 20 \
  --strategy-seconds 300 \
  --research-seconds 7200 \
  --chutes-timeout 90 \
  --chutes-retries 3 \
  --strategy-model "$STRATEGY_MODEL" \
  --research-model "$RESEARCH_MODEL" \
  --strategy-max-tokens 2000 \
  --research-max-tokens 3000 \
  --nova-dir "$NOVA_DIR" \
  "$@"
