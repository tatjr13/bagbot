"""Main threshold-farming strategy: regime classification + threshold computation."""

import time
import logging
from typing import Dict, Optional, Tuple

from Brains import config, signals
from Brains.models import SignalSnapshot, ThresholdPatch, SubnetState
from Brains.risk import (
    get_preset, dynamic_min_edge, apply_regime_adjustments,
    clamp_threshold_shift, check_cooldowns, check_daily_turnover,
    passes_subnet_universe_filter, RiskPreset,
)
from Brains.state import PriceBarStore, StrategyStateStore

logger = logging.getLogger(__name__)


def classify_regime(snap: SignalSnapshot) -> str:
    """Classify market regime from signal snapshot.

    Returns one of: 'pump', 'bull', 'bear', 'chop'.
    """
    if snap.range_pos_24h > 0.85 and snap.momentum_6h > 0.06:
        return 'pump'
    if (
        snap.ema_fast_slow_spread > 0.008
        and snap.ema_fast_slope_6h > 0.003
        and snap.spot_price > snap.ema_fast
    ):
        return 'bull'
    if (
        snap.ema_fast_slow_spread < -0.008
        and snap.ema_fast_slope_6h < -0.003
        and snap.spot_price < snap.ema_fast
    ):
        return 'bear'
    if snap.ema_slope_24h > 0.01 and snap.spot_price > snap.ema_72h:
        return 'bull'
    if snap.ema_slope_24h < -0.01 and snap.spot_price < snap.ema_72h:
        return 'bear'
    return 'chop'


def compute_signals(
    netuid: int,
    spot_price: float,
    tao_in: float,
    alpha_in: float,
    current_alpha: float,
    max_alpha: float,
    max_buy_tao: float,
    bar_store: PriceBarStore,
    now: float = None,
    cfg: Dict = None,
) -> SignalSnapshot:
    """Compute all signals for a subnet from stored price bars."""
    cfg = cfg or config.load_config()
    lookbacks = cfg.get('lookbacks', {})
    now = now or time.time()

    ema_hours = lookbacks.get('ema_hours', 72)
    ema_fast_hours = lookbacks.get('ema_fast_hours', 12)
    ema_fast_slope_hours = lookbacks.get('ema_fast_slope_hours', 6)
    vol_hours = lookbacks.get('vol_hours', 24)
    range_short_h = lookbacks.get('range_short_hours', 24)
    range_med_h = lookbacks.get('range_medium_hours', 72)
    mom_short_h = lookbacks.get('momentum_short_hours', 6)

    bar_minutes = cfg.get('bar_size_minutes', 15)
    bars_per_hour = 60 / bar_minutes

    # Ideal bar counts
    ideal_ema_bars = int(ema_hours * bars_per_hour)
    ideal_vol_bars = int(vol_hours * bars_per_hour)
    ideal_range_short_bars = int(range_short_h * bars_per_hour)
    ideal_range_med_bars = int(range_med_h * bars_per_hour)
    ideal_mom_bars = int(mom_short_h * bars_per_hour)

    # Get price data
    ema_prices = bar_store.get_close_prices(netuid, ema_hours, now)
    vol_prices = bar_store.get_close_prices(netuid, vol_hours, now)
    range_short_prices = bar_store.get_close_prices(netuid, range_short_h, now)
    range_med_prices = bar_store.get_close_prices(netuid, range_med_h, now)
    mom_history_hours = mom_short_h + (bar_minutes / 60.0)
    mom_prices = bar_store.get_close_prices(netuid, mom_history_hours, now)

    # Get bars for tao_in data
    bars_all = bar_store.get_bars(netuid, ema_hours, now)
    tao_in_values = [b[5] for b in bars_all]  # index 5 = tao_in

    # Confidence based on available vs ideal bars
    available_bars = bar_store.get_bar_count(netuid, ema_hours, now)
    conf = signals.compute_confidence(available_bars, ideal_ema_bars)

    # Compute EMA - span is in bars
    ema_span = int(ema_hours * bars_per_hour)
    ema_val = signals.ema(ema_prices, ema_span)
    if ema_val is None:
        ema_val = spot_price  # absolute fallback
    ema_fast_span = max(1, int(ema_fast_hours * bars_per_hour))
    ema_fast_val = signals.ema(ema_prices, ema_fast_span)
    if ema_fast_val is None:
        ema_fast_val = spot_price

    # EMA slope lookback in bars (24h)
    slope_lookback = int(24 * bars_per_hour)
    fast_slope_lookback = max(1, int(ema_fast_slope_hours * bars_per_hour))

    return SignalSnapshot(
        netuid=netuid,
        spot_price=spot_price,
        ema_72h=ema_val,
        ema_distance=signals.ema_distance(spot_price, ema_val),
        ema_slope_24h=signals.ema_slope(ema_prices, ema_span, slope_lookback),
        range_pos_24h=signals.range_position(range_short_prices),
        range_pos_72h=signals.range_position(range_med_prices),
        momentum_6h=signals.momentum(mom_prices, ideal_mom_bars),
        volatility_24h=signals.volatility(vol_prices),
        volume_score=signals.volume_score(tao_in_values),
        inventory_ratio=signals.inventory_ratio(current_alpha, max_alpha),
        est_slippage_pct=signals.estimate_slippage_pct(max_buy_tao, tao_in),
        confidence=conf,
        tao_in_pool=tao_in,
        alpha_in_pool=alpha_in,
        ema_fast=ema_fast_val,
        ema_fast_distance=signals.ema_distance(spot_price, ema_fast_val),
        ema_fast_slope_6h=signals.ema_slope(ema_prices, ema_fast_span, fast_slope_lookback),
        ema_fast_slow_spread=signals.ema_distance(ema_fast_val, ema_val),
    )


def compute_thresholds(
    snap: SignalSnapshot,
    state: SubnetState,
    preset: RiskPreset,
    original_grid: Dict,
    daily_buy_tao: float,
    daily_sell_tao: float,
    portfolio_value_tao: float,
    dry_run: bool = True,
    now: float = None,
    cfg: Dict = None,
) -> Optional[ThresholdPatch]:
    """Compute threshold patch for a subnet.

    Returns None if cooldowns prevent update or warmup is insufficient.
    """
    cfg = cfg or config.load_config()
    now = now or time.time()

    warmup_min_h = cfg.get('warmup_min_hours', 24)
    warmup_full_h = cfg.get('warmup_full_hours', 72)
    min_confidence = cfg.get('trade_only_if_confidence_gte', 0.70)
    freeze_buys_confidence = cfg.get('freeze_buys_if_confidence_lt', 0.50)
    derisk_only_confidence = cfg.get('de_risk_only_if_confidence_lt', min_confidence)
    derisk_only_confidence = max(derisk_only_confidence, freeze_buys_confidence)

    # Check threshold update cooldown
    can_update, can_trade = check_cooldowns(state, now)
    if not can_update:
        return None

    # Warmup gate: no adaptive thresholds until warmup_min_hours
    warmup_ratio_gate = 0.0
    if warmup_min_h > 0:
        if warmup_full_h > 0:
            warmup_ratio_gate = min(max(warmup_min_h / warmup_full_h, 0.0), 1.0)
        else:
            warmup_ratio_gate = 1.0

    # We use confidence as a proxy for history availability.
    if snap.confidence < warmup_ratio_gate:
        # Very early: not enough data, keep static thresholds
        logger.info(f'Brains sn{snap.netuid}: insufficient warmup '
                    f'(confidence={snap.confidence:.2f}), keeping static thresholds')
        return None

    # Classify regime
    regime = classify_regime(snap)

    # Start from preset offsets
    buy_low_off, buy_high_off = preset.buy_offsets
    sell_low_off, sell_high_off = preset.sell_offsets

    # Apply regime and condition adjustments
    (buy_low_off, buy_high_off, sell_low_off, sell_high_off,
     buy_size_mult, sell_size_mult, enable_buys) = apply_regime_adjustments(
        buy_low_off, buy_high_off,
        sell_low_off, sell_high_off,
        regime, snap.inventory_ratio,
        snap.volatility_24h / max(0.01, signals.volatility([snap.ema_72h] * 2)),  # vol_mult approx
        snap.est_slippage_pct,
        snap.confidence,
        freeze_buys_if_confidence_lt=freeze_buys_confidence,
        de_risk_only_if_confidence_lt=derisk_only_confidence,
    )

    # Dual-timeframe overlay: keep the slow EMA anchor, but react faster when
    # the short EMA turns up or crosses the slow EMA on discounted names.
    fast_turn_strength = min(
        max(snap.ema_fast_slope_6h * 120.0, 0.0)
        + max(snap.ema_fast_slow_spread * 50.0, 0.0),
        1.0,
    )
    fast_headwind_strength = min(
        max(-snap.ema_fast_slope_6h * 120.0, 0.0)
        + max(-snap.ema_fast_slow_spread * 50.0, 0.0),
        1.0,
    )
    discounted_slow_anchor = max(0.0, min(-snap.ema_distance, 0.08))
    flip_signal = min(fast_turn_strength * (0.5 + discounted_slow_anchor * 6.0), 1.0)

    buy_low_off += min(0.0035, 0.0035 * flip_signal)
    buy_high_off += min(0.0025, 0.0025 * flip_signal)
    sell_low_off += min(0.0030, 0.0020 * flip_signal)
    sell_high_off += min(0.0040, 0.0030 * flip_signal)

    if snap.inventory_ratio > 0.0 and fast_headwind_strength > 0.0:
        sell_low_off -= min(0.0020, 0.0020 * fast_headwind_strength)
        sell_high_off -= min(0.0030, 0.0030 * fast_headwind_strength)

    # Compute dynamic edge
    min_edge_cfg = cfg.get('min_roundtrip_edge_pct', 0.02)
    required_edge = dynamic_min_edge(min_edge_cfg, snap.est_slippage_pct / 100.0)

    # Compute thresholds from EMA + offsets
    buy_lower = snap.ema_72h * (1 + buy_low_off)
    buy_upper = snap.ema_72h * (1 + buy_high_off)

    # Cost floor based on avg entry price (only if known)
    cost_floor = 0.0
    if state.avg_entry_price is not None and state.avg_entry_price > 0:
        cost_floor = state.avg_entry_price * (1 + required_edge)

    sell_lower = max(cost_floor, snap.ema_72h * (1 + sell_low_off))
    sell_upper = max(sell_lower * 1.005, snap.ema_72h * (1 + sell_high_off))

    # Ensure buy band is below sell band
    if buy_upper >= sell_lower:
        # Push buy_upper down with a gap
        buy_upper = sell_lower * 0.99
        buy_lower = min(buy_lower, buy_upper * 0.95)

    # Size caps from preset, scaled by adjustments
    max_buy = preset.max_buy_tao * buy_size_mult
    max_sell = preset.max_sell_tao * sell_size_mult

    # Apply per-subnet configured caps as ceiling
    configured_max_buy = original_grid.get('max_tao_per_buy', max_buy)
    configured_max_sell = original_grid.get('max_tao_per_sell', max_sell)
    max_buy = min(max_buy, configured_max_buy)
    max_sell = min(max_sell, configured_max_sell)

    # Daily turnover check
    max_turnover = cfg.get('max_daily_turnover_ratio', 0.15)
    turnover_can_buy, turnover_can_sell = check_daily_turnover(
        daily_buy_tao, daily_sell_tao, portfolio_value_tao, max_turnover
    )
    if not turnover_can_buy:
        enable_buys = False
    enable_sells = turnover_can_sell

    # Trade cooldown
    if not can_trade:
        enable_buys = False
        enable_sells = False

    # Very low confidence: disable buys and freeze prior thresholds.
    if snap.confidence < freeze_buys_confidence:
        enable_buys = False
        if state.last_buy_lower is not None:
            buy_lower = state.last_buy_lower
            buy_upper = state.last_buy_upper
        if state.last_sell_lower is not None:
            sell_lower = state.last_sell_lower
            sell_upper = state.last_sell_upper

    # Moderate confidence: only allow de-risking (no widening of buy zone)
    elif snap.confidence < derisk_only_confidence:
        enable_buys = False

    # Clamp threshold shifts
    max_shift = cfg.get('max_threshold_shift_pct_per_tick', 0.005)
    buy_lower = clamp_threshold_shift(buy_lower, state.last_buy_lower, max_shift)
    buy_upper = clamp_threshold_shift(buy_upper, state.last_buy_upper, max_shift)
    sell_lower = clamp_threshold_shift(sell_lower, state.last_sell_lower, max_shift)
    sell_upper = clamp_threshold_shift(sell_upper, state.last_sell_upper, max_shift)

    # Build reason string
    reasons = [f'regime={regime}']
    if not enable_buys:
        reasons.append('buys_disabled')
    if snap.confidence < derisk_only_confidence:
        reasons.append(f'low_conf={snap.confidence:.2f}')
    if snap.inventory_ratio > 0.8:
        reasons.append(f'high_inv={snap.inventory_ratio:.0%}')
    if snap.est_slippage_pct > 0.5:
        reasons.append(f'high_slip={snap.est_slippage_pct:.2f}%')
    if cost_floor > 0:
        reasons.append(f'cost_floor={cost_floor:.6f}')
    if flip_signal > 0.10:
        reasons.append(f'fast_flip={flip_signal:.2f}')
    if fast_headwind_strength > 0.20:
        reasons.append(f'fast_headwind={fast_headwind_strength:.2f}')

    patch = ThresholdPatch(
        netuid=snap.netuid,
        buy_lower=buy_lower,
        buy_upper=buy_upper,
        sell_lower=sell_lower,
        sell_upper=sell_upper,
        max_tao_per_buy=max_buy,
        max_tao_per_sell=max_sell,
        enable_buys=enable_buys,
        enable_sells=enable_sells,
        regime=regime,
        reason='; '.join(reasons),
        confidence=snap.confidence,
        dry_run=dry_run,
    )

    logger.info(
        f'Brains sn{snap.netuid}: {patch.reason} | '
        f'buy=[{buy_lower:.6f},{buy_upper:.6f}] sell=[{sell_lower:.6f},{sell_upper:.6f}] '
        f'max_buy={max_buy:.3f} max_sell={max_sell:.3f} '
        f'{"DRY_RUN" if dry_run else "LIVE"}'
    )

    return patch
