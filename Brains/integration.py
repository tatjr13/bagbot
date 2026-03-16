"""Thin integration wrapper: StrategyEngine.on_tick() and on_fill() wiring."""

import logging
import math
import os
import time
from datetime import datetime, timezone
from dataclasses import replace
from typing import Dict, List, Optional, Set

from Brains import config
from Brains.models import ThresholdPatch, FillRecord, SubnetState
from Brains.state import PriceBarStore, StrategyStateStore
from Brains import taostats_api
from Brains.threshold_farm import compute_signals, compute_thresholds
from Brains.risk import get_preset, passes_subnet_universe_filter

logger = logging.getLogger(__name__)
RAO_PER_TAO = 1_000_000_000.0


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rao_to_tao(value) -> float:
    return _float_or_zero(value) / RAO_PER_TAO


def _age_days_from_timestamp(value, now: float) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
        else:
            normalized = str(value).strip().replace('Z', '+00:00')
            ts = datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
        return max(0.0, (now - ts) / 86400.0)
    except (TypeError, ValueError):
        return None


class TaoStatsFlowCache:
    """Cached Taostats subnet flow snapshot used for roster ranking."""

    def __init__(self, cfg: Dict):
        self.by_netuid: Dict[int, Dict[str, float]] = {}
        self.last_refresh_at = 0.0
        self.last_error = ""
        self.refresh_seconds = 600.0
        self.timeout_seconds = 20.0
        self.enabled = False
        self.refresh_config(cfg)

    def refresh_config(self, cfg: Dict) -> None:
        refresh_minutes = float(cfg.get('taostats_refresh_minutes', 10.0) or 10.0)
        self.refresh_seconds = max(60.0, refresh_minutes * 60.0)
        self.enabled = bool(cfg.get('taostats_flow_enabled', True)) and bool(
            os.environ.get('TAOSTATS_API_KEY', '').strip()
        )
        if not self.enabled:
            self.by_netuid = {}
            self.last_refresh_at = 0.0

    def get(self, netuid: int) -> Optional[Dict[str, float]]:
        if not self.enabled:
            return None
        if not self.by_netuid:
            return None
        if self.last_refresh_at <= 0:
            return None
        if (time.time() - self.last_refresh_at) >= self.refresh_seconds:
            return None
        return self.by_netuid.get(netuid)

    def maybe_refresh(self, force: bool = False) -> None:
        if not self.enabled:
            return

        now = time.time()
        if not force and self.by_netuid and (now - self.last_refresh_at) < self.refresh_seconds:
            return

        try:
            url = taostats_api.build_url('/api/subnet/latest/v1', ['page=1', 'limit=200'])
            taostats_api.enforce_rate_limit(
                float(os.environ.get('TAOSTATS_RATE_LIMIT_PER_MIN', 5) or 5),
                no_wait=False,
            )
            payload = taostats_api.fetch(url, self.timeout_seconds)
            rows = payload.get('data', []) if isinstance(payload, dict) else payload
            next_by_netuid: Dict[int, Dict[str, float]] = {}
            for row in rows or []:
                netuid = int(row.get('netuid', -1))
                if netuid < 0:
                    continue
                subnet_age_days = _age_days_from_timestamp(row.get('registration_timestamp'), now)
                next_by_netuid[netuid] = {
                    'net_flow_1d_tao': _rao_to_tao(row.get('net_flow_1_day')),
                    'net_flow_7d_tao': _rao_to_tao(row.get('net_flow_7_days')),
                    'net_flow_30d_tao': _rao_to_tao(row.get('net_flow_30_days')),
                    'tao_flow_tao': _rao_to_tao(row.get('tao_flow')),
                    'ema_tao_flow_tao': _rao_to_tao(row.get('ema_tao_flow')),
                    'fee_rate': _float_or_zero(row.get('fee_rate')),
                    'subnet_age_days': subnet_age_days,
                }

            if next_by_netuid:
                self.by_netuid = next_by_netuid
                self.last_refresh_at = now
                self.last_error = ""
                logger.info(
                    f'Taostats flow snapshot refreshed for {len(self.by_netuid)} subnets'
                )
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning(f'Taostats flow refresh failed: {exc}')


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
        self.dynamic_subnet_grids: Dict[int, Dict] = {}
        self.risk_mode = self.cfg.get('risk_mode_default', 'conservative')
        self.max_live = self.cfg.get('max_live_subnets', 3)
        self.bar_minutes = self.cfg.get('bar_size_minutes', 15)
        self.taostats_flow_cache = TaoStatsFlowCache(self.cfg)
        self.dry_run = True
        self.tradable_netuids: Set[int] = set()
        self.runtime_netuids: Set[int] = set()
        self.buy_roster_netuids: Set[int] = set()
        self.runtime_ordered_netuids: List[int] = []
        self.refresh_runtime_settings(bagbot_settings)

        # Telegram notifications via stub logger (Arbos handles actual Telegram UI)
        from Brains.telegram_cmds import setup_telegram
        self.telegram = setup_telegram(self)

        logger.info(
            f'Brains initialized: dry_run={self.dry_run}, risk={self.risk_mode}, '
            f'seed_netuids={sorted(self.tradable_netuids)}, '
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
        self.taostats_flow_cache.refresh_config(latest_cfg)

    def refresh_runtime_settings(self, bagbot_settings):
        """Refresh settings-derived runtime state without losing bar history."""
        new_dry_run = getattr(bagbot_settings, 'BRAINS_DRY_RUN', True)
        new_tradable_netuids: Set[int] = set(
            getattr(bagbot_settings, 'SUBNET_SETTINGS', {}).keys()
        )

        settings_changed = (
            new_dry_run != self.dry_run or
            new_tradable_netuids != self.tradable_netuids
        )

        self.dry_run = new_dry_run
        self.tradable_netuids = new_tradable_netuids

        if settings_changed:
            logger.info(
                'Brains runtime settings refreshed: '
                f'dry_run={self.dry_run}, seed_netuids={sorted(self.tradable_netuids)}'
            )

    def _build_dynamic_grid(self, sdata: Dict) -> Dict:
        """Create a provisional grid so Brains can trade newly discovered subnets."""
        spot_price = max(float(sdata.get('price', 0.0) or 0.0), 1e-9)
        max_alpha_tao = float(self.cfg.get('dynamic_max_alpha_tao', 25.0) or 25.0)
        buy_upper_discount = float(self.cfg.get('dynamic_buy_upper_discount_pct', 0.015) or 0.015)
        buy_lower_discount = float(self.cfg.get('dynamic_buy_lower_discount_pct', 0.045) or 0.045)
        sell_lower_premium = float(self.cfg.get('dynamic_sell_lower_premium_pct', 0.020) or 0.020)
        sell_upper_premium = float(self.cfg.get('dynamic_sell_upper_premium_pct', 0.080) or 0.080)

        return {
            'buy_lower': spot_price * max(0.01, 1.0 - buy_lower_discount),
            'buy_upper': spot_price * max(0.01, 1.0 - buy_upper_discount),
            'sell_lower': spot_price * (1.0 + sell_lower_premium),
            'sell_upper': spot_price * (1.0 + sell_upper_premium),
            'max_alpha': max(1.0, max_alpha_tao / spot_price),
        }

    def _candidate_score(
        self,
        snap,
        patch: ThresholdPatch,
        is_configured: bool,
        is_held: bool,
        was_live: bool,
    ) -> float:
        """Rank candidates for the live roster while keeping existing positions manageable."""
        buy_edge = 0.0
        if patch.buy_upper > 0:
            buy_edge = max(0.0, (patch.buy_upper - snap.spot_price) / patch.buy_upper)

        discount_score = max(0.0, -snap.ema_distance)
        range_score = max(0.0, 0.65 - snap.range_pos_24h) * 0.05
        volume_bonus = max(0.0, min(snap.volume_score - 1.0, 2.0)) * 0.03
        liquidity_bonus = min(max(snap.tao_in_pool / 5000.0, 0.0), 1.0) * 0.03
        confidence_bonus = min(max(snap.confidence, 0.0), 0.25)
        slippage_penalty = min(max(snap.est_slippage_pct / 100.0, 0.0), 0.5)
        momentum_penalty = max(0.0, snap.momentum_6h) * 0.5
        inventory_penalty = snap.inventory_ratio * 0.10
        flow_day_scale = max(float(self.cfg.get('taostats_flow_day_norm_tao', 500.0) or 500.0), 1.0)
        flow_week_scale = max(float(self.cfg.get('taostats_flow_week_norm_tao', 350.0) or 350.0), 1.0)
        flow_ema_scale = max(float(self.cfg.get('taostats_flow_ema_norm_tao', 0.01) or 0.01), 1e-6)
        flow_day_signal = math.tanh(snap.net_flow_1d_tao / flow_day_scale)
        flow_week_signal = math.tanh((snap.net_flow_7d_tao / 7.0) / flow_week_scale)
        flow_ema_signal = math.tanh(snap.ema_tao_flow_tao / flow_ema_scale)
        flow_bonus = (
            max(0.0, flow_day_signal) * 0.18
            + max(0.0, flow_week_signal) * 0.10
            + max(0.0, flow_ema_signal) * 0.05
        )
        flow_alignment_bonus = 0.0
        if flow_day_signal > 0.0 and flow_week_signal > 0.0:
            flow_alignment_bonus = min(flow_day_signal, flow_week_signal) * 0.06
        flow_penalty = (
            max(0.0, -flow_day_signal) * 0.20
            + max(0.0, -flow_week_signal) * 0.10
            + max(0.0, -flow_ema_signal) * 0.05
        )
        configured_bonus = 0.05 if is_configured else 0.0
        held_bonus = 0.04 if is_held else 0.0
        live_bonus = 0.03 if was_live else 0.0

        score = (
            (buy_edge * 2.0)
            + discount_score
            + range_score
            + volume_bonus
            + liquidity_bonus
            + confidence_bonus
            + flow_bonus
            + flow_alignment_bonus
            + configured_bonus
            + held_bonus
            + live_bonus
            - slippage_penalty
            - momentum_penalty
            - inventory_penalty
            - flow_penalty
        )
        if patch.regime == 'pump':
            score -= 0.10
        return score

    def get_runtime_subnet_grids(self, configured_grids: Dict[int, Dict]) -> Dict[int, Dict]:
        """Return the active trading roster in execution order."""
        if not self.runtime_ordered_netuids:
            return {
                netuid: dict(grid)
                for netuid, grid in configured_grids.items()
            }

        runtime_grids: Dict[int, Dict] = {}
        for netuid in self.runtime_ordered_netuids:
            if netuid in configured_grids:
                runtime_grids[netuid] = dict(configured_grids[netuid])
            elif netuid in self.dynamic_subnet_grids:
                runtime_grids[netuid] = dict(self.dynamic_subnet_grids[netuid])
        return runtime_grids

    def on_tick(self, stats: Dict, subnet_grids: Dict, stake_info: Dict, balance: float):
        """Called each bot tick after refresh_stats().

        Records price bars for all observed subnets, computes strategy
        patches for a dynamic roster built from all observed subnets.
        """
        self.refresh_strategy_config()
        self.taostats_flow_cache.maybe_refresh()
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

        held_netuids: Set[int] = set()
        for hotkey in stake_info:
            for netuid, stake_obj in stake_info[hotkey].items():
                if stake_obj and float(stake_obj.stake) > 0 and netuid in stats:
                    held_netuids.add(netuid)
        held_fallback_grids = {
            netuid: dict(subnet_grids.get(netuid, {}) or self._build_dynamic_grid(stats[netuid]))
            for netuid in held_netuids
            if netuid in stats
        }

        candidate_rows = []
        next_patches: Dict[int, ThresholdPatch] = dict(self.patches)
        next_dynamic_subnet_grids: Dict[int, Dict] = dict(self.dynamic_subnet_grids)
        bootstrap_refresh = not self.patches

        for netuid in sorted(stats.keys()):
            if netuid not in stats:
                continue

            sdata = stats[netuid]
            is_configured = netuid in self.tradable_netuids
            grid = dict(subnet_grids.get(netuid, {}) or self._build_dynamic_grid(sdata))
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
            flow_snapshot = self.taostats_flow_cache.get(netuid)

            # Universe filter
            history_h = self.bar_store.get_history_hours(netuid, now)
            passes, reason = passes_subnet_universe_filter(
                netuid, sdata.get('tao_in', 0), history_h, max_buy_tao,
                allowed_netuids=None,
                subnet_age_days=flow_snapshot.get('subnet_age_days') if flow_snapshot else None,
                cfg=self.cfg,
            )
            if not passes:
                logger.debug(f'Brains sn{netuid}: skipped - {reason}')
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
                cfg=self.cfg,
            )
            if flow_snapshot is not None:
                snap = replace(snap, **flow_snapshot)

            # Get state and daily turnover
            state = self.state_store.get(netuid)
            state_for_compute = replace(state, last_patch_at=0.0) if bootstrap_refresh else state
            daily_buy, daily_sell = self.bar_store.get_daily_turnover(netuid, now)

            # Compute thresholds
            fresh_patch = compute_thresholds(
                snap=snap,
                state=state_for_compute,
                preset=preset,
                original_grid=grid,
                daily_buy_tao=daily_buy,
                daily_sell_tao=daily_sell,
                portfolio_value_tao=portfolio_value,
                dry_run=self.dry_run,
                now=now,
                cfg=self.cfg,
            )

            patch = fresh_patch or self.patches.get(netuid)
            if patch is not None:
                score = self._candidate_score(
                    snap=snap,
                    patch=patch,
                    is_configured=is_configured,
                    is_held=netuid in held_netuids,
                    was_live=netuid in self.buy_roster_netuids,
                )
                next_patches[netuid] = patch
                if not is_configured:
                    next_dynamic_subnet_grids[netuid] = grid
                candidate_rows.append({
                    'netuid': netuid,
                    'score': score,
                    'grid': grid,
                    'patch': patch,
                    'is_configured': is_configured,
                    'is_held': netuid in held_netuids,
                })

            if fresh_patch is not None:
                next_patches[netuid] = fresh_patch
                # Update state
                state.last_patch_at = now
                state.last_buy_lower = fresh_patch.buy_lower
                state.last_buy_upper = fresh_patch.buy_upper
                state.last_sell_lower = fresh_patch.sell_lower
                state.last_sell_upper = fresh_patch.sell_upper
                state.regime = fresh_patch.regime
                self.state_store.save()

        ranked_rows = sorted(candidate_rows, key=lambda row: row['score'], reverse=True)
        top_rows = ranked_rows[:self.max_live]
        top_netuids = [row['netuid'] for row in top_rows]
        ranked_netuids = {row['netuid'] for row in ranked_rows}
        missing_held_netuids = [
            netuid for netuid in sorted(held_netuids)
            if netuid not in ranked_netuids and netuid not in top_netuids
        ]
        exit_only_netuids = [
            row['netuid']
            for row in sorted(ranked_rows, key=lambda row: row['score'])
            if row['is_held'] and row['netuid'] not in top_netuids
        ]

        managed_netuids = set(top_netuids) | held_netuids
        for netuid in managed_netuids:
            patch = next_patches.get(netuid)
            if patch is None:
                continue
            if netuid in held_netuids and netuid not in top_netuids:
                patch.enable_buys = False
                if 'managed_exit_only' not in patch.reason:
                    patch.reason = f'{patch.reason}; managed_exit_only'

        new_runtime_order = missing_held_netuids + exit_only_netuids + top_netuids
        new_buy_roster = {
            row['netuid']
            for row in top_rows
            if next_patches.get(row['netuid']) is not None
            and next_patches[row['netuid']].enable_buys
        }

        if (
            new_runtime_order != self.runtime_ordered_netuids
            or new_buy_roster != self.buy_roster_netuids
        ):
            flow_summaries = []
            for row in top_rows[:3]:
                flow_tao = next_patches.get(row['netuid']) and self.taostats_flow_cache.get(row['netuid'])
                if not flow_tao:
                    continue
                flow_summaries.append(
                    f"sn{row['netuid']}:{flow_tao.get('net_flow_1d_tao', 0.0):+.0f}t/d"
                )
            logger.info(
                'Brains runtime roster refreshed: '
                f'live={top_netuids}, buy_enabled={sorted(new_buy_roster)}, '
                f'exit_only={exit_only_netuids}, '
                f'flow_1d={flow_summaries}'
            )

        self.runtime_ordered_netuids = new_runtime_order
        self.runtime_netuids = managed_netuids
        self.buy_roster_netuids = new_buy_roster
        self.dynamic_subnet_grids = {
            netuid: grid
            for netuid, grid in next_dynamic_subnet_grids.items()
            if netuid in managed_netuids
        }
        for netuid in missing_held_netuids:
            if netuid not in self.dynamic_subnet_grids and netuid in held_fallback_grids:
                self.dynamic_subnet_grids[netuid] = held_fallback_grids[netuid]
        self.patches = {
            netuid: patch
            for netuid, patch in next_patches.items()
            if netuid in managed_netuids
        }

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
