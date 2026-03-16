"""Risk presets, guardrails, and threshold clamping."""

import time
import logging
from typing import Optional, Tuple
from dataclasses import dataclass

from Brains import config
from Brains.models import SubnetState, ThresholdPatch

logger = logging.getLogger(__name__)


@dataclass
class RiskPreset:
    name: str
    buy_offsets: Tuple[float, float]   # (low_offset, high_offset) from EMA
    sell_offsets: Tuple[float, float]   # (low_offset, high_offset) from EMA
    max_buy_tao: float
    max_sell_tao: float
    max_position_alpha: float          # multiplier on configured max_alpha


# Start at 50% of conservative for buys during canary period
RISK_PRESETS = {
    'conservative': RiskPreset(
        name='conservative',
        buy_offsets=(-0.060, -0.025),
        sell_offsets=(0.020, 0.050),
        max_buy_tao=1.5,       # 50% of the original 3 (canary sizing)
        max_sell_tao=5.0,
        max_position_alpha=20,
    ),
    'balanced': RiskPreset(
        name='balanced',
        buy_offsets=(-0.045, -0.015),
        sell_offsets=(0.015, 0.045),
        max_buy_tao=5.0,
        max_sell_tao=8.0,
        max_position_alpha=35,
    ),
    'aggressive': RiskPreset(
        name='aggressive',
        buy_offsets=(-0.030, -0.010),
        sell_offsets=(0.010, 0.035),
        max_buy_tao=8.0,
        max_sell_tao=12.0,
        max_position_alpha=50,
    ),
}


def get_preset(name: str) -> RiskPreset:
    return RISK_PRESETS.get(name, RISK_PRESETS['conservative'])


def dynamic_min_edge(min_roundtrip_edge_pct: float, est_slippage_pct: float) -> float:
    """Compute dynamic minimum roundtrip edge.

    required_edge = max(config_min, 2 * est_slippage + 0.003)
    Bittensor charges ~0.05% fees on stake/unstake plus slippage.
    """
    return max(min_roundtrip_edge_pct, 2 * est_slippage_pct + 0.003)


def apply_regime_adjustments(
    buy_low_off: float, buy_high_off: float,
    sell_low_off: float, sell_high_off: float,
    regime: str, inventory_pct: float,
    vol_mult: float, est_slippage_pct: float,
    confidence: float,
    freeze_buys_if_confidence_lt: float = 0.5,
    de_risk_only_if_confidence_lt: float = 0.7,
) -> Tuple[float, float, float, float, float, float, bool]:
    """Apply regime-based and condition-based offset adjustments.

    Returns:
        (buy_low_off, buy_high_off, sell_low_off, sell_high_off,
         buy_size_mult, sell_size_mult, enable_buys)
    """
    buy_size_mult = 1.0
    sell_size_mult = 1.0
    enable_buys = True

    # Scale all adjustments by confidence
    c = confidence

    if regime == 'pump':
        enable_buys = False
        buy_size_mult = 0.0
    elif regime == 'bull':
        buy_low_off += 0.005 * c
        buy_high_off += 0.005 * c
        sell_low_off += 0.005 * c
        sell_high_off += 0.010 * c
    elif regime == 'bear':
        buy_low_off -= 0.010 * c
        buy_high_off -= 0.010 * c
        sell_low_off -= 0.005 * c
        sell_high_off -= 0.005 * c

    # Inventory-aware: >80% filled, tighten sell, widen buy
    if inventory_pct > 0.80:
        buy_low_off -= 0.010 * c
        buy_high_off -= 0.010 * c
        sell_low_off -= 0.010 * c
        sell_high_off -= 0.010 * c

    # High volatility: widen both sides
    if vol_mult > 1.0:
        widen = (vol_mult - 1.0) * 0.01 * c
        buy_low_off -= widen
        buy_high_off -= widen
        sell_low_off += widen
        sell_high_off += widen

    # High slippage / low liquidity: reduce sizes, widen
    if est_slippage_pct > 0.5:
        buy_size_mult *= 0.5
        sell_size_mult *= 0.75
        buy_low_off -= 0.005 * c
        sell_high_off += 0.005 * c

    # Confidence gates: when confidence is low, only allow de-risking.
    de_risk_only_if_confidence_lt = max(
        de_risk_only_if_confidence_lt,
        freeze_buys_if_confidence_lt,
    )
    if confidence < freeze_buys_if_confidence_lt:
        enable_buys = False
    elif confidence < de_risk_only_if_confidence_lt:
        # Only allow shrinkage / de-risking adjustments
        # Don't widen buy zone (don't buy more aggressively)
        buy_size_mult = min(buy_size_mult, 0.5)

    return (buy_low_off, buy_high_off, sell_low_off, sell_high_off,
            buy_size_mult, sell_size_mult, enable_buys)


def clamp_threshold_shift(
    new_val: float,
    old_val: Optional[float],
    max_shift_pct: float
) -> float:
    """Clamp a threshold so it doesn't jump more than max_shift_pct from previous value."""
    if old_val is None or old_val == 0:
        return new_val
    max_delta = abs(old_val) * max_shift_pct
    delta = new_val - old_val
    if abs(delta) > max_delta:
        return old_val + (max_delta if delta > 0 else -max_delta)
    return new_val


def check_cooldowns(state: SubnetState, now: float = None) -> Tuple[bool, bool]:
    """Check if threshold update and trade cooldowns have elapsed.

    Returns:
        (can_update_thresholds, can_trade)
    """
    cfg = config.load_config()
    now = now or time.time()

    update_cooldown = cfg.get('min_minutes_between_threshold_updates', 15) * 60
    trade_cooldown = cfg.get('min_minutes_between_trades_per_subnet', 60) * 60

    can_update = (now - state.last_patch_at) >= update_cooldown
    can_trade = (now - state.last_trade_at) >= trade_cooldown

    return (can_update, can_trade)


def check_daily_turnover(
    daily_buy_tao: float, daily_sell_tao: float,
    portfolio_value_tao: float, max_ratio: float = None
) -> Tuple[bool, bool]:
    """Check if daily turnover limits allow more trades.

    Returns:
        (can_buy, can_sell)
    """
    if max_ratio is None:
        max_ratio = config.get('max_daily_turnover_ratio', 0.15)

    if portfolio_value_tao <= 0:
        return (True, True)

    max_turnover = portfolio_value_tao * max_ratio
    can_buy = daily_buy_tao < max_turnover
    can_sell = daily_sell_tao < max_turnover
    return (can_buy, can_sell)


def passes_subnet_universe_filter(
    netuid: int,
    tao_in_pool: float,
    history_hours: float,
    max_buy_tao: float,
    allowed_netuids: set = None,
) -> Tuple[bool, str]:
    """Check if a subnet passes the tradable universe filters.

    Returns:
        (passes, reason)
    """
    cfg = config.load_config()
    min_liquidity = cfg.get('min_liquidity_tao', 150)
    min_age_days = cfg.get('new_subnet_min_age_days', 7)
    warmup_min_hours = cfg.get('warmup_min_hours', 24)

    # Manual allowlist check
    if allowed_netuids is not None and netuid not in allowed_netuids:
        return (False, f'sn{netuid} not in tradable allowlist')

    # Minimum liquidity
    if tao_in_pool < min_liquidity:
        return (False, f'sn{netuid} pool too thin: {tao_in_pool:.0f} TAO < {min_liquidity}')

    # Pool must be at least 100x max buy
    if tao_in_pool < max_buy_tao * 100:
        return (False, f'sn{netuid} pool {tao_in_pool:.0f} < 100x max_buy {max_buy_tao}')

    # Need minimum history
    if history_hours < warmup_min_hours:
        return (False, f'sn{netuid} only {history_hours:.1f}h history < {warmup_min_hours}h warmup')

    # Simulated slippage check
    if tao_in_pool > 0:
        sim_slippage = (max_buy_tao / (tao_in_pool + max_buy_tao)) * 100
        if sim_slippage > 0.5:
            return (False, f'sn{netuid} simulated slippage {sim_slippage:.2f}% > 0.5%')

    return (True, 'passes')
