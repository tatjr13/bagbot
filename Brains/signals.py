"""Pure signal computation functions. No side effects, no state mutation."""

import math
from typing import List, Optional


def ema(prices: List[float], span_bars: int) -> Optional[float]:
    """Compute exponential moving average over a list of close prices.

    Args:
        prices: List of close prices, oldest first.
        span_bars: EMA span in number of bars.

    Returns:
        EMA value, or None if prices is empty.
    """
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]

    k = 2.0 / (span_bars + 1)
    ema_val = prices[0]
    for p in prices[1:]:
        ema_val = p * k + ema_val * (1.0 - k)
    return ema_val


def ema_distance(spot: float, ema_val: float) -> float:
    """Fractional distance of spot from EMA: (spot - ema) / ema."""
    if ema_val == 0:
        return 0.0
    return (spot - ema_val) / ema_val


def ema_slope(prices: List[float], span_bars: int, lookback_bars: int) -> float:
    """Rate of change of EMA over lookback_bars.

    Returns fractional change: (ema_now - ema_then) / ema_then.
    Returns 0.0 if insufficient data.
    """
    if len(prices) < lookback_bars + 1:
        return 0.0

    ema_now = ema(prices, span_bars)
    ema_then = ema(prices[:-lookback_bars], span_bars)
    if ema_now is None or ema_then is None or ema_then == 0:
        return 0.0
    return (ema_now - ema_then) / ema_then


def range_position(prices: List[float]) -> float:
    """Where does the last price sit within the high-low range? 0.0 = at low, 1.0 = at high.

    Returns 0.5 if insufficient data or flat range.
    """
    if len(prices) < 2:
        return 0.5

    high = max(prices)
    low = min(prices)
    if high == low:
        return 0.5
    return (prices[-1] - low) / (high - low)


def momentum(prices: List[float], lookback_bars: int) -> float:
    """Price momentum: (price_now - price_N_bars_ago) / price_N_bars_ago.

    Returns 0.0 if insufficient data.
    """
    if len(prices) < lookback_bars + 1:
        return 0.0
    old_price = prices[-(lookback_bars + 1)]
    if old_price == 0:
        return 0.0
    return (prices[-1] - old_price) / old_price


def volatility(prices: List[float]) -> float:
    """Coefficient of variation: std_dev / mean of prices.

    Returns 0.0 if insufficient data.
    """
    if len(prices) < 2:
        return 0.0
    mean = sum(prices) / len(prices)
    if mean == 0:
        return 0.0
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    return math.sqrt(variance) / mean


def volume_score(tao_in_values: List[float], lookback_bars: int = 96) -> float:
    """Relative volume: recent avg tao_in vs longer-term avg.

    Returns 1.0 if insufficient data.
    """
    if len(tao_in_values) < 4:
        return 1.0
    recent = tao_in_values[-min(4, len(tao_in_values)):]
    avg_recent = sum(recent) / len(recent)

    full = tao_in_values[-min(lookback_bars, len(tao_in_values)):]
    avg_full = sum(full) / len(full)
    if avg_full == 0:
        return 1.0
    return avg_recent / avg_full


def estimate_slippage_pct(trade_amount_tao: float, tao_in_pool: float) -> float:
    """Estimate slippage percentage for a given trade size vs pool.

    Uses constant-product AMM formula: slippage = amount / (pool + amount).
    """
    if tao_in_pool <= 0:
        return 100.0
    return (trade_amount_tao / (tao_in_pool + trade_amount_tao)) * 100.0


def compute_confidence(available_bars: int, ideal_bars: int) -> float:
    """Confidence metric: how much of ideal history is available."""
    if ideal_bars <= 0:
        return 1.0
    return min(available_bars / ideal_bars, 1.0)


def inventory_ratio(current_alpha: float, max_alpha: float) -> float:
    """Current inventory as fraction of max allowed."""
    if max_alpha <= 0:
        return 0.0
    return min(current_alpha / max_alpha, 1.0)
