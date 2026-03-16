# Targon Setup Notes

This file captures the current live Bagbot/Arbos operating model and the Targon deployment layout.

## Runtime Layout

- Bagbot repo on the Targon box: `/data/bagbot`
- Arbos repo on the Targon box: `/data/bagbot-arbos/Arbos`
- Persistent wallet storage: `/data/bittensor-wallets`
- Wallet symlink expected by Bagbot: `/root/.bittensor/wallets -> /data/bittensor-wallets`
- Live processes:
  - `bagbot-core`
  - `bagbot-arbos`

## Important Files

- Core trading loop: `bagbot.py`
- Canary/live settings template: `bagbot_settings_falcon_5tao.py`
- Live runtime overrides on Targon: `/data/bagbot/bagbot_settings_overrides.py`
- Brains prompt: `Brains/arbos/PROMPT.md`
- Arbos live goal file on Targon: `/data/bagbot-arbos/Arbos/context/GOAL.md`
- Brains config: `Brains/config/threshold_farm.yaml`
- Brains threshold logic: `Brains/threshold_farm.py`
- Brains risk logic: `Brains/risk.py`
- Brains integration layer: `Brains/integration.py`
- Taostats helper: `Brains/taostats_api.py`
- Targon deploy helper for Arbos: `Brains/arbos/deploy.sh`

## Live Trading Behavior

- Bankroll is uncapped: `MAX_PORTFOLIO_TAO = None`
- Buy and sell sizing are unbounded at the config level: `MAX_TAO_PER_BUY = None`, `MAX_TAO_PER_SELL = None`
- Rotation is enabled and can happen even when cash is still available
- Atomic subnet-to-subnet rotation is enabled
- MEV-protected execution is enabled
- Live subnet discovery is dynamic: Brains scans the observed universe, keeps a bounded active roster, and continues managing held positions outside the current buy roster until they are exited
- A per-subnet allocation cap can be used to stop the bot from aping the entire bankroll into a single subnet; the Falcon canary template currently uses `MAX_SUBNET_ALLOCATION_RATIO = 0.35`
- A small execution fee buffer is intentionally kept so the bot does not fail on dust-level fee shortfalls
- Current design goal is an active book of roughly `5-7` positions, with permission to rotate out of weak inventory into better setups
- The bot must never transfer funds to another wallet

## Telegram UX

- The live Targon `arbos.py` runtime has a calm Telegram overlay applied on top of upstream Arbos
- Rapid operator messages are batched for a short debounce window before the bot replies
- Operator replies use a calmer status + final-answer pattern instead of constant token-stream edits
- Autonomous step status edits are throttled more heavily so the chat is less jumpy during normal operation
- Because Arbos is cloned from its own upstream repo at deploy time, this Telegram UX patch should be treated as a Targon runtime customization unless it is upstreamed separately

## Wallet Notes

- Current live wallet identity is `Falcon`
- Restore coldkey into persistent storage with:
  - `btcli wallet regen-coldkey --wallet-name Falcon --wallet-path /data/bittensor-wallets --use-password`
- Create the hotkey with:
  - `btcli wallet new-hotkey --wallet-name Falcon --wallet-path /data/bittensor-wallets --hotkey default --n-words 24 --no-use-password`
- Prefer `WALLET_PW_FILE` or `WALLET_PW_ENV` over a plaintext `WALLET_PW` in tracked config files
- On the Targon box, keep password files at mode `600`
- Do not commit live wallet passwords, mnemonics, or API keys into tracked files

## Targon Operations

- SSH uses a Targon rental user against `ssh.deployments.targon.com`
- Keep the SSH target in local `~/.ssh/config`; do not hardcode the ephemeral rental username in tracked repo files
- Arbos deploy script assumes an SSH host alias such as `bagbot-targon`

Useful commands on the Targon box:

```bash
pm2 status
pm2 restart bagbot-core
pm2 restart bagbot-arbos
pm2 logs bagbot-arbos --lines 50 --nostream
tail -n 100 /data/bagbot/staking.log
```

## Update Flow

1. Sync code to `/data/bagbot` and `/data/bagbot-arbos/Arbos`
2. Compile-check changed Python files with `python3 -m py_compile`
3. Restart the affected PM2 process
4. Watch `staking.log` for:
   - successful fills
   - rotation activity
   - fee-buffer messages
   - MEV or slippage errors
5. Confirm Arbos is still stepping and writing research output

## Safety Notes

- Keep secrets in local `.env` files or remote-only runtime files, not in Git
- Keep `/data/bagbot/bagbot_settings_overrides.py` and any password files at `600`
- Treat Taostats as read-only research input
- Do not add any workflow that transfers funds or uses a second wallet without explicit operator approval
