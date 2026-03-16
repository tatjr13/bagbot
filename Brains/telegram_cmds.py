"""Telegram notification stub for Brains strategy plugin.

The actual Telegram bot interface is handled by Arbos (github.com/unconst/Arbos),
which runs as a separate process on Targon. This module only provides a lightweight
send_async() logger so that Brains fill callbacks and notifications still work
when Arbos is not co-located with bagbot.
"""

import logging

logger = logging.getLogger(__name__)


class BrainsTelegramStub:
    """Noop Telegram stub — logs notifications to staking.log.

    Arbos handles the actual Telegram interaction. This stub exists so
    integration.py can call telegram.send_async() without crashing.
    """

    def send_async(self, msg: str, chat_id=None):
        logger.info(f'Brains notification: {msg}')


def setup_telegram(engine, token_path: str = ''):
    """Return a stub logger. Arbos handles real Telegram comms."""
    logger.info('Telegram interface handled by Arbos — using log stub')
    return BrainsTelegramStub()
