"""Dataclasses for the Brains strategy plugin."""

from dataclasses import dataclass, field
from typing import Optional, Dict


@dataclass
class SignalSnapshot:
    """All computed signals for a single subnet at a point in time."""
    netuid: int
    spot_price: float
    ema_72h: float
    ema_distance: float          # (spot - ema) / ema
    ema_slope_24h: float         # rate of change of EMA over 24h
    range_pos_24h: float         # 0..1 position within 24h high-low range
    range_pos_72h: float         # 0..1 position within 72h high-low range
    momentum_6h: float           # (price_now - price_6h_ago) / price_6h_ago
    volatility_24h: float        # std_dev / mean of 24h prices
    volume_score: float          # relative volume metric
    inventory_ratio: float       # current_alpha / max_alpha (0..1)
    est_slippage_pct: float      # estimated slippage for max_buy size
    confidence: float            # min(available_bars / ideal_bars, 1.0)
    tao_in_pool: float           # pool liquidity in TAO
    alpha_in_pool: float         # pool liquidity in alpha


@dataclass
class ThresholdPatch:
    """Computed threshold overrides for a single subnet."""
    netuid: int
    buy_lower: float
    buy_upper: float
    sell_lower: float
    sell_upper: float
    max_tao_per_buy: float
    max_tao_per_sell: float
    enable_buys: bool
    enable_sells: bool
    regime: str                  # 'pump', 'bull', 'bear', 'chop'
    reason: str                  # human-readable explanation
    confidence: float
    dry_run: bool                # if True, computed but not applied


@dataclass
class SubnetState:
    """Persisted per-subnet strategy state."""
    netuid: int
    last_patch_at: float = 0.0           # timestamp of last threshold update
    last_trade_at: float = 0.0           # timestamp of last confirmed trade
    last_buy_lower: Optional[float] = None
    last_buy_upper: Optional[float] = None
    last_sell_lower: Optional[float] = None
    last_sell_upper: Optional[float] = None
    daily_buy_tao: float = 0.0           # rolling 24h buy volume in TAO
    daily_sell_tao: float = 0.0          # rolling 24h sell volume in TAO
    daily_turnover_reset_at: float = 0.0 # when daily counters were last reset
    avg_entry_price: Optional[float] = None
    total_cost_basis_tao: float = 0.0    # total TAO spent on buys
    total_alpha_bought: float = 0.0      # total alpha acquired from buys
    regime: str = 'chop'


@dataclass
class FillRecord:
    """A confirmed trade execution."""
    netuid: int
    side: str           # 'buy' or 'sell'
    tao_amount: float
    alpha_amount: float
    price: float        # effective price
    timestamp: float
    tx_hash: str = ''
