"""Thin integration wrapper: StrategyEngine.on_tick() and on_fill() wiring."""

import os
import time
import logging
from typing import Dict, Optional, Set

from Brains import config
from Brains.models import ThresholdPatch, FillRecord, SubnetState
from Brains.state import PriceBarStore, StrategyStateStore
from Brains.signals import inventory_ratio
from Brains.threshold_farm import compute_signals, compute_thresholds
from Brains.risk import get_preset, passes_subnet_universe_filter

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Orchestrates the Brains strategy plugin.

    Wired into bagbot.py via:
    - on_tick(): called after refresh_stats, records bars + computes patches
    - on_fill(): called after confirmed trade, updates cost basis + cooldowns
    - get_patch(): returns current ThresholdPatch for a subnet (or None)
    """

    def __init__(self, bagbot_settings):
        self.cfg = config.load_config()
        self.bar_store = PriceBarStore()
        self.state_store = StrategyStateStore()
        self.telegram = None  # set externally if telegram enabled
        self.patches: Dict[int, ThresholdPatch] = {}
        self.risk_mode = self.cfg.get('risk_mode_default', 'conservative')
        self.max_live = self.cfg.get('max_live_subnets', 3)
        self.bar_minutes = self.cfg.get('bar_size_minutes', 15)
        self.dry_run = True
        self.tradable_netuids: Set[int] = set()
        self.refresh_runtime_settings(bagbot_settings)

        # Telegram notifications via stub logger (Arbos handles actual Telegram UI)
        from Brains.telegram_cmds import setup_telegram
        self.telegram = setup_telegram(self)

        logger.info(
            f'Brains initialized: dry_run={self.dry_run}, risk={self.risk_mode}, '
            f'tradable_netuids={self.tradable_netuids}, '
            f'telegram={"ON" if self.telegram else "OFF"}'
        )

    def refresh_strategy_config(self):
        """Refresh Brains YAML config that may change during runtime."""
        latest_cfg = config.load_config()
        latest_risk_mode = latest_cfg.get('risk_mode_default', 'conservative')
        latest_max_live = latest_cfg.get('max_live_subnets', 3)
        latest_bar_minutes = latest_cfg.get('bar_size_minutes', 15)

        if (
            latest_risk_mode != self.risk_mode or
            latest_max_live != self.max_live or
            latest_bar_minutes != self.bar_minutes
        ):
            logger.info(
                'Brains strategy config refreshed: '
                f'risk={latest_risk_mode}, max_live={latest_max_live}, '
                f'bar_minutes={latest_bar_minutes}'
            )

        self.cfg = latest_cfg
        self.risk_mode = latest_risk_mode
        self.max_live = latest_max_live
        self.bar_minutes = latest_bar_minutes

    def refresh_runtime_settings(self, bagbot_settings):
        """Refresh settings-derived runtime state without losing bar history."""
        new_dry_run = getattr(bagbot_settings, 'BRAINS_DRY_RUN', True)
        new_tradable_netuids: Set[int] = set(
            getattr(bagbot_settings, 'SUBNET_SETTINGS', {}).keys()
        )

        removed_netuids = set(self.patches.keys()) - new_tradable_netuids
        for netuid in removed_netuids:
            self.patches.pop(netuid, None)

        settings_changed = (
            new_dry_run != self.dry_run or
            new_tradable_netuids != self.tradable_netuids
        )

        self.dry_run = new_dry_run
        self.tradable_netuids = new_tradable_netuids

        if settings_changed or removed_netuids:
            logger.info(
                'Brains runtime settings refreshed: '
                f'dry_run={self.dry_run}, tradable_netuids={sorted(self.tradable_netuids)}'
            )

    def on_tick(self, stats: Dict, subnet_grids: Dict, stake_info: Dict, balance: float):
        """Called each bot tick after refresh_stats().

        Records price bars for all observed subnets, computes strategy
        patches only for tradable subnets that pass universe filters.
        """
        self.refresh_strategy_config()
        now = time.time()
        preset = get_preset(self.risk_mode)

        # Compute portfolio value for turnover limits
        portfolio_value = balance
        for hotkey in stake_info:
            for netuid in stake_info[hotkey]:
                stake_obj = stake_info[hotkey].get(netuid)
                if stake_obj and float(stake_obj.stake) > 0 and netuid in stats:
                    portfolio_value += float(stake_obj.stake) * stats[netuid]['price']

        # Record price bars for ALL observed subnets
        for netuid, sdata in stats.items():
            price = sdata.get('price', 0)
            tao_in = sdata.get('tao_in', 0)
            alpha_in = sdata.get('alpha_in', 0)
            if price > 0:
                self.bar_store.record_tick(
                    netuid, price, tao_in, alpha_in,
                    timestamp=now, bar_minutes=self.bar_minutes,
                )

        # Compute strategy patches only for tradable subnets
        active_patches = 0
        for netuid in self.tradable_netuids:
            if netuid not in stats:
                continue
            if active_patches >= self.max_live:
                break

            sdata = stats[netuid]
            grid = subnet_grids.get(netuid, {})
            max_alpha = grid.get('max_alpha', 0)
            if max_alpha <= 0:
                continue

            # Current holdings across all validators
            current_alpha = 0.0
            for hotkey in stake_info:
                stake_obj = stake_info[hotkey].get(netuid)
                if stake_obj:
                    current_alpha += float(stake_obj.stake)

            max_buy_tao = grid.get('max_tao_per_buy', preset.max_buy_tao)

            # Universe filter
            history_h = self.bar_store.get_history_hours(netuid, now)
            passes, reason = passes_subnet_universe_filter(
                netuid, sdata.get('tao_in', 0), history_h, max_buy_tao,
                allowed_netuids=self.tradable_netuids,
            )
            if not passes:
                logger.debug(f'Brains sn{netuid}: skipped - {reason}')
                # Still observe, just don't trade adaptively
                continue

            # Compute signals
            snap = compute_signals(
                netuid=netuid,
                spot_price=sdata['price'],
                tao_in=sdata.get('tao_in', 0),
                alpha_in=sdata.get('alpha_in', 0),
                current_alpha=current_alpha,
                max_alpha=max_alpha,
                max_buy_tao=max_buy_tao,
                bar_store=self.bar_store,
                now=now,
            )

            # Get state and daily turnover
            state = self.state_store.get(netuid)
            daily_buy, daily_sell = self.bar_store.get_daily_turnover(netuid, now)

            # Compute thresholds
            patch = compute_thresholds(
                snap=snap,
                state=state,
                preset=preset,
                original_grid=grid,
                daily_buy_tao=daily_buy,
                daily_sell_tao=daily_sell,
                portfolio_value_tao=portfolio_value,
                dry_run=self.dry_run,
                now=now,
            )

            if patch is not None:
                self.patches[netuid] = patch
                # Update state
                state.last_patch_at = now
                state.last_buy_lower = patch.buy_lower
                state.last_buy_upper = patch.buy_upper
                state.last_sell_lower = patch.sell_lower
                state.last_sell_upper = patch.sell_upper
                state.regime = patch.regime
                self.state_store.save()
                active_patches += 1

        # Prune old bars periodically
        self.bar_store.prune(max_hours=96)

    def on_fill(self, netuid: int, side: str, tao_amount: float,
                alpha_amount: float, price: float, tx_hash: str = ''):
        """Called after a confirmed trade execution.

        Updates cost basis, daily turnover, and trade cooldowns.
        """
        now = time.time()
        fill = FillRecord(
            netuid=netuid, side=side, tao_amount=tao_amount,
            alpha_amount=alpha_amount, price=price, timestamp=now,
            tx_hash=tx_hash,
        )

        # Record in SQLite
        self.bar_store.record_fill(fill)

        # Update cost basis
        self.state_store.update_cost_basis(netuid, fill)

        # Update trade timestamp
        state = self.state_store.get(netuid)
        state.last_trade_at = now
        self.state_store.save()

        logger.info(
            f'Brains sn{netuid}: fill recorded - {side} {tao_amount:.4f} TAO / '
            f'{alpha_amount:.2f} alpha @ {price:.6f}'
        )

        # Send telegram notification if available
        if self.telegram:
            msg = (f'Fill sn{netuid}: {side.upper()} {tao_amount:.4f} TAO / '
                   f'{alpha_amount:.2f} alpha @ {price:.6f}')
            self.telegram.send_async(msg)

    def get_patch(self, netuid: int) -> Optional[ThresholdPatch]:
        """Get the current strategy patch for a subnet, or None."""
        return self.patches.get(netuid)

    def get_all_patches(self) -> Dict[int, ThresholdPatch]:
        """Get all current strategy patches."""
        return dict(self.patches)
