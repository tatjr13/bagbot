#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT_DIR/.." && pwd)"

if [ -f "$PROJECT_ROOT/bagbot_taostats_api.env" ]; then
  set -a
  source "$PROJECT_ROOT/bagbot_taostats_api.env"
  set +a
fi

cd "$ROOT_DIR"

exec python3 arbos_wallet_intel_loop.py \
  --cycle-seconds 900 \
  --timeout 20 \
  --force-first
