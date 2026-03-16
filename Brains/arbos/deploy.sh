#!/bin/bash
# Deploy Arbos (Bagbot agent) to Targon rental
# Usage: bash Brains/arbos/deploy.sh
#
# Prerequisites:
#   - SSH access to bagbot-targon configured in ~/.ssh/config
#   - Token files in project root: bagbot_telegram_token.env, bagbot_chutes_api.env
#   - Optional: TAOSTATS_API_KEY exported in shell, or bagbot_taostats_api.env present

set -euo pipefail

REMOTE="bagbot-targon"
REMOTE_DIR="/data/bagbot-arbos"
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== Bagbot Arbos Deployment ==="
echo "Remote: $REMOTE"
echo "Remote dir: $REMOTE_DIR"
echo "Project dir: $PROJECT_DIR"
echo ""

# Read tokens from local env files
TELEGRAM_TOKEN=$(grep -oP 'TELEGRAM_TOKEN=\K.*' "$PROJECT_DIR/bagbot_telegram_token.env" | tr -d '"' | tr -d "'")
CHUTES_KEY=$(grep -oP 'CHUTES_API_KEY=\K.*' "$PROJECT_DIR/bagbot_chutes_api.env" | tr -d '"' | tr -d "'")
TAOSTATS_KEY="${TAOSTATS_API_KEY:-}"

if [ -z "$TAOSTATS_KEY" ] && [ -f "$PROJECT_DIR/bagbot_taostats_api.env" ]; then
    TAOSTATS_KEY=$(grep -oP 'TAOSTATS_API_KEY=\K.*' "$PROJECT_DIR/bagbot_taostats_api.env" | tr -d '"' | tr -d "'")
fi

if [ -z "$TELEGRAM_TOKEN" ] || [ -z "$CHUTES_KEY" ]; then
    echo "ERROR: Missing tokens in bagbot_telegram_token.env or bagbot_chutes_api.env"
    exit 1
fi

echo "1. Setting up remote directory..."
ssh "$REMOTE" "mkdir -p $REMOTE_DIR"

echo "2. Cloning/updating Arbos..."
ssh "$REMOTE" "
    if [ -d $REMOTE_DIR/Arbos/.git ]; then
        cd $REMOTE_DIR/Arbos && git pull --ff-only
    else
        cd $REMOTE_DIR && git clone https://github.com/unconst/Arbos.git
    fi
"

echo "3. Writing .env..."
ssh "$REMOTE" "cat > $REMOTE_DIR/Arbos/.env << ENVEOF
TAU_BOT_TOKEN=$TELEGRAM_TOKEN
PROVIDER=chutes
CHUTES_API_KEY=$CHUTES_KEY
# Keep the visible Claude-side model stable, but force Chutes routing explicitly
# so the deployment does not depend on the account-level `default` alias.
CLAUDE_MODEL=openai/gpt-oss-120b-TEE
CHUTES_ROUTING_AGENT=openai/gpt-oss-120b-TEE,MiniMaxAI/MiniMax-M2.5-TEE,deepseek-ai/DeepSeek-V3.1-TEE,moonshotai/Kimi-K2.5-TEE:throughput
CHUTES_ROUTING_BOT=openai/gpt-oss-120b-TEE,MiniMaxAI/MiniMax-M2.5-TEE,deepseek-ai/DeepSeek-V3.1-TEE,moonshotai/Kimi-K2.5-TEE:latency
PROXY_PORT=8090
COST_PER_M_INPUT=0
COST_PER_M_OUTPUT=0
TAOSTATS_API_KEY=$TAOSTATS_KEY
TAOSTATS_RATE_LIMIT_PER_MIN=5
ENVEOF
chmod 600 $REMOTE_DIR/Arbos/.env
"

echo "4. Copying PROMPT.md..."
scp "$PROJECT_DIR/Brains/arbos/PROMPT.md" "$REMOTE:$REMOTE_DIR/Arbos/PROMPT.md"

echo "5. Installing dependencies..."
ssh "$REMOTE" "bash -c '
    cd $REMOTE_DIR/Arbos
    # Create venv and install Python deps
    python3 -m venv .venv
    . .venv/bin/activate
    pip install requests python-dotenv pyTelegramBotAPI httpx fastapi uvicorn cryptography
    # Install pm2 if missing
    which pm2 >/dev/null 2>&1 || npm install -g pm2
'"

echo "6. Writing launch script..."
ssh "$REMOTE" "cat > $REMOTE_DIR/Arbos/.bagbot-launch.sh << 'LAUNCHEOF'
#!/usr/bin/env bash
set -e
export PATH=\"\$HOME/.local/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:\$PATH\"
cd \"/data/bagbot-arbos/Arbos\"
source .venv/bin/activate
exec python3 arbos.py 2>&1
LAUNCHEOF
chmod +x $REMOTE_DIR/Arbos/.bagbot-launch.sh
"

echo "7. Starting Arbos via pm2..."
ssh "$REMOTE" "
    cd $REMOTE_DIR/Arbos
    export PATH=\"\$HOME/.local/bin:\$PATH\"
    pm2 delete bagbot-arbos 2>/dev/null || true
    pm2 start .bagbot-launch.sh --name bagbot-arbos --cwd $REMOTE_DIR/Arbos
    pm2 save
"

echo ""
echo "=== Deployment complete ==="
echo "Arbos running on $REMOTE as 'bagbot-arbos'"
echo "Send /start to @BagBot on Telegram to claim ownership"
echo "Then /goal to set the agent's objective"
