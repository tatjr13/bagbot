#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT_DIR/.." && pwd)"

if [ -f "$PROJECT_ROOT/bagbot_chutes_api.env" ]; then
  set -a
  source "$PROJECT_ROOT/bagbot_chutes_api.env"
  set +a
fi

if [ -f "$PROJECT_ROOT/bagbot_taostats_api.env" ]; then
  set -a
  source "$PROJECT_ROOT/bagbot_taostats_api.env"
  set +a
fi

cd "$ROOT_DIR"

exec python3 arbos_terminal_loop.py \
  --cycle-seconds 12 \
  --status-seconds 30 \
  --wallet-seconds 300 \
  --chutes-seconds 24 \
  --chutes-timeout 45 \
  --chutes-retries 1 \
  --max-tokens 900 \
  --log-path "$PROJECT_ROOT/staking.log"
