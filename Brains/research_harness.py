"""Offline replay harness for evaluating Brains configs on recorded subnet history.

This is a lightweight `autoresearch`-style evaluator:
- replay historical price bars from `Brains/price_history.db`
- run the live `StrategyEngine` offline against the bar stream
- simulate fee/slippage-aware buys and sells with a shared TAO bankroll
- score candidate configs on net TAO growth, drawdown, and turnover

It is intentionally conservative: no live API calls, no wallet access, no code mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Brains import config, signals
from Brains.integration import StrategyEngine
from Brains.state import PriceBarStore, StrategyStateStore


DEFAULT_DB_PATH = Path(__file__).with_name("price_history.db")
DEFAULT_CFG_PATH = Path(__file__).parent / "config" / "threshold_farm.yaml"


@dataclass
class ReplayResult:
    name: str
    config_path: str
    bars: int
    netuids: int
    trades: int
    buys: int
    sells: int
    turnover_tao: float
    final_value_tao: float
    pnl_tao: float
    return_pct: float
    max_drawdown_pct: float
    objective: float


class ReplayStake:
    def __init__(self, stake: float):
        self.stake = stake


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Brains configs on recorded price history.")
    parser.add_argument(
        "--config",
        action="append",
        dest="config_paths",
        help="Config YAML to evaluate. Repeat for multiple candidates. Defaults to the live config.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite price history path. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=168.0,
        help="Replay window in hours counting back from the latest recorded bar. Default: 168.",
    )
    parser.add_argument(
        "--start-cash",
        type=float,
        default=10.0,
        help="Starting TAO bankroll for the simulation. Default: 10.",
    )
    parser.add_argument(
        "--turnover-penalty-rate",
        type=float,
        default=0.001,
        help="Extra penalty per TAO of turnover applied to the objective. Default: 0.001.",
    )
    parser.add_argument(
        "--simulated-fee-rate",
        type=float,
        default=0.0005,
        help="Fallback fee rate when historical fee data is unavailable. Default: 0.0005.",
    )
    parser.add_argument(
        "--min-trade-tao",
        type=float,
        default=0.01,
        help="Ignore simulated trades smaller than this TAO amount. Default: 0.01.",
    )
    parser.add_argument(
        "--netuid",
        action="append",
        type=int,
        dest="netuids",
        help="Optional netuid filter. Repeat to restrict replay to specific subnets.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    return parser.parse_args()


def _group_rows(rows: Iterable[tuple]) -> List[tuple[int, Dict[int, Dict[str, float]]]]:
    timeline: List[tuple[int, Dict[int, Dict[str, float]]]] = []
    current_bar_time: Optional[int] = None
    current_stats: Dict[int, Dict[str, float]] = {}

    for row in rows:
        bar_time, netuid, _open, high, low, close, tao_in, alpha_in, tick_count = row
        if current_bar_time is None:
            current_bar_time = int(bar_time)
        if bar_time != current_bar_time:
            timeline.append((current_bar_time, current_stats))
            current_bar_time = int(bar_time)
            current_stats = {}
        current_stats[int(netuid)] = {
            "price": float(close),
            "tao_in": float(tao_in),
            "alpha_in": float(alpha_in),
            "high": float(high),
            "low": float(low),
            "tick_count": int(tick_count),
        }

    if current_bar_time is not None and current_stats:
        timeline.append((current_bar_time, current_stats))
    return timeline


def _determine_buy_threshold(grid: Dict, current_alpha: float) -> Optional[float]:
    buy_upper = grid.get("buy_upper")
    if buy_upper is None:
        return None
    buy_lower = grid.get("buy_lower")
    max_alpha = float(grid.get("max_alpha", 0.0) or 0.0)
    if buy_lower is None or current_alpha <= 0 or max_alpha <= 0:
        return float(buy_upper)
    progress = min(max(current_alpha / max_alpha, 0.0), 1.0)
    curve_value = progress ** float(grid.get("buy_zone_power", 1.0) or 1.0)
    return float(buy_upper) - (float(buy_upper) - float(buy_lower)) * curve_value


def _determine_sell_threshold(grid: Dict, current_alpha: float) -> Optional[float]:
    sell_lower = grid.get("sell_lower")
    if sell_lower is None:
        return None
    sell_upper = grid.get("sell_upper")
    max_alpha = float(grid.get("max_alpha", 0.0) or 0.0)
    if sell_upper is None or current_alpha <= 0 or max_alpha <= 0:
        return float(sell_lower)
    progress = min(max(current_alpha / max_alpha, 0.0), 1.0)
    curve_value = progress ** float(grid.get("sell_zone_power", 1.0) or 1.0)
    return float(sell_upper) - (float(sell_upper) - float(sell_lower)) * curve_value


def _stake_info_for_holdings(holdings_alpha: Dict[int, float]) -> Dict[str, Dict[int, ReplayStake]]:
    return {
        "research-hotkey": {
            netuid: ReplayStake(alpha)
            for netuid, alpha in holdings_alpha.items()
            if alpha > 0
        }
    }


def _last_prices(timeline: List[tuple[int, Dict[int, Dict[str, float]]]]) -> Dict[int, float]:
    prices: Dict[int, float] = {}
    for _bar_time, stats in timeline:
        for netuid, sdata in stats.items():
            prices[netuid] = float(sdata["price"])
    return prices


def evaluate_config(
    config_path: str,
    db_path: str,
    hours: float,
    start_cash: float,
    turnover_penalty_rate: float,
    simulated_fee_rate: float,
    min_trade_tao: float,
    netuids: Optional[List[int]] = None,
) -> ReplayResult:
    source_store = PriceBarStore(db_path)
    try:
        latest_bar_time = source_store.get_latest_bar_time()
        if latest_bar_time is None:
            raise RuntimeError(f"No price bars found in {db_path}")
        start_time = latest_bar_time - int(hours * 3600)
        rows = source_store.get_bars_between(start_time, latest_bar_time, netuids=netuids)
        if not rows:
            raise RuntimeError(f"No price bars found in the last {hours}h for the requested universe")
        timeline = _group_rows(rows)
    finally:
        source_store.close()

    cfg = dict(config.load_config(config_path))
    cfg["taostats_flow_enabled"] = False
    settings = SimpleNamespace(SUBNET_SETTINGS={}, BRAINS_DRY_RUN=False)

    with tempfile.TemporaryDirectory(prefix="bagbot-research-") as tmpdir, ExitStack() as stack:
        replay_bar_store = PriceBarStore(os.path.join(tmpdir, "replay.sqlite"))
        replay_state_store = StrategyStateStore(os.path.join(tmpdir, "state.json"))
        stack.callback(replay_bar_store.close)
        stack.enter_context(patch("Brains.config.load_config", return_value=cfg))
        stack.enter_context(patch("Brains.integration.PriceBarStore", return_value=replay_bar_store))
        stack.enter_context(patch("Brains.integration.StrategyStateStore", return_value=replay_state_store))
        stack.enter_context(patch("Brains.telegram_cmds.setup_telegram", return_value=None))

        engine = StrategyEngine(settings)
        cash_balance = float(start_cash)
        holdings_alpha: Dict[int, float] = {}
        equity_curve: List[float] = []
        buys = 0
        sells = 0
        turnover_tao = 0.0

        for bar_time, stats in timeline:
            stake_info = _stake_info_for_holdings(holdings_alpha)
            engine.on_tick(stats, settings.SUBNET_SETTINGS, stake_info=stake_info, balance=cash_balance)
            runtime_grids = engine.get_runtime_subnet_grids(settings.SUBNET_SETTINGS)

            # Exit weak or profitable positions before deploying fresh cash.
            for netuid in list(runtime_grids.keys()):
                if netuid not in stats:
                    continue
                current_alpha = holdings_alpha.get(netuid, 0.0)
                if current_alpha <= 0:
                    continue
                patch_obj = engine.get_patch(netuid)
                if patch_obj is None or not patch_obj.enable_sells:
                    continue
                live_grid = dict(runtime_grids[netuid])
                live_grid.update(
                    {
                        "buy_lower": patch_obj.buy_lower,
                        "buy_upper": patch_obj.buy_upper,
                        "sell_lower": patch_obj.sell_lower,
                        "sell_upper": patch_obj.sell_upper,
                    }
                )
                sell_threshold = _determine_sell_threshold(live_grid, current_alpha)
                price = float(stats[netuid]["price"])
                if sell_threshold is None or price < sell_threshold:
                    continue
                max_sell_tao = float(getattr(patch_obj, "max_tao_per_sell", 0.0) or 0.0)
                sell_notional_tao = current_alpha * price
                if max_sell_tao > 0:
                    sell_notional_tao = min(sell_notional_tao, max_sell_tao)
                if sell_notional_tao < min_trade_tao:
                    continue
                alpha_to_sell = min(current_alpha, sell_notional_tao / max(price, 1e-9))
                gross_tao = alpha_to_sell * price
                fee_rate = float(getattr(patch_obj, "fee_rate", 0.0) or 0.0) or simulated_fee_rate
                slippage_rate = signals.estimate_slippage_pct(gross_tao, float(stats[netuid]["tao_in"])) / 100.0
                net_tao = gross_tao * max(0.0, 1.0 - fee_rate - slippage_rate)
                cash_balance += net_tao
                holdings_alpha[netuid] = max(0.0, current_alpha - alpha_to_sell)
                turnover_tao += gross_tao
                sells += 1
                engine.on_fill(netuid, "sell", gross_tao, alpha_to_sell, price, tx_hash=f"sim-sell-{bar_time}")

            # Deploy cash across the active runtime roster using the same patched thresholds.
            for netuid in list(runtime_grids.keys()):
                if netuid not in stats or cash_balance < min_trade_tao:
                    continue
                patch_obj = engine.get_patch(netuid)
                if patch_obj is None or not patch_obj.enable_buys:
                    continue
                live_grid = dict(runtime_grids[netuid])
                live_grid.update(
                    {
                        "buy_lower": patch_obj.buy_lower,
                        "buy_upper": patch_obj.buy_upper,
                        "sell_lower": patch_obj.sell_lower,
                        "sell_upper": patch_obj.sell_upper,
                    }
                )
                current_alpha = holdings_alpha.get(netuid, 0.0)
                buy_threshold = _determine_buy_threshold(live_grid, current_alpha)
                price = float(stats[netuid]["price"])
                if buy_threshold is None or price > buy_threshold:
                    continue
                max_buy_tao = float(getattr(patch_obj, "max_tao_per_buy", 0.0) or 0.0)
                buy_tao = cash_balance if max_buy_tao <= 0 else min(cash_balance, max_buy_tao)
                if buy_tao < min_trade_tao:
                    continue
                fee_rate = float(getattr(patch_obj, "fee_rate", 0.0) or 0.0) or simulated_fee_rate
                slippage_rate = signals.estimate_slippage_pct(buy_tao, float(stats[netuid]["tao_in"])) / 100.0
                effective_tao = buy_tao * max(0.0, 1.0 - fee_rate - slippage_rate)
                alpha_bought = effective_tao / max(price, 1e-9)
                if alpha_bought <= 0:
                    continue
                cash_balance -= buy_tao
                holdings_alpha[netuid] = current_alpha + alpha_bought
                turnover_tao += buy_tao
                buys += 1
                engine.on_fill(netuid, "buy", buy_tao, alpha_bought, price, tx_hash=f"sim-buy-{bar_time}")

            mark_to_market = cash_balance
            for netuid, alpha in holdings_alpha.items():
                if alpha > 0 and netuid in stats:
                    mark_to_market += alpha * float(stats[netuid]["price"])
            equity_curve.append(mark_to_market)

        if not equity_curve:
            equity_curve = [start_cash]

        peak_equity = equity_curve[0]
        max_drawdown_pct = 0.0
        for equity in equity_curve:
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                max_drawdown_pct = max(max_drawdown_pct, (peak_equity - equity) / peak_equity)

        final_prices = _last_prices(timeline)
        final_value = cash_balance + sum(
            alpha * final_prices.get(netuid, 0.0)
            for netuid, alpha in holdings_alpha.items()
        )
        pnl_tao = final_value - start_cash
        objective = pnl_tao - (start_cash * max_drawdown_pct) - (turnover_tao * turnover_penalty_rate)

        return ReplayResult(
            name=Path(config_path).stem,
            config_path=config_path,
            bars=len(timeline),
            netuids=len(final_prices),
            trades=buys + sells,
            buys=buys,
            sells=sells,
            turnover_tao=turnover_tao,
            final_value_tao=final_value,
            pnl_tao=pnl_tao,
            return_pct=(pnl_tao / start_cash) * 100.0 if start_cash > 0 else 0.0,
            max_drawdown_pct=max_drawdown_pct * 100.0,
            objective=objective,
        )


def _render_table(results: List[ReplayResult]) -> str:
    headers = (
        "config",
        "bars",
        "netuids",
        "trades",
        "final_tao",
        "pnl_tao",
        "return_pct",
        "max_dd_pct",
        "objective",
    )
    rows = [headers]
    for result in results:
        rows.append(
            (
                result.name,
                str(result.bars),
                str(result.netuids),
                str(result.trades),
                f"{result.final_value_tao:.4f}",
                f"{result.pnl_tao:+.4f}",
                f"{result.return_pct:+.2f}",
                f"{result.max_drawdown_pct:.2f}",
                f"{result.objective:+.4f}",
            )
        )
    widths = [max(len(row[idx]) for row in rows) for idx in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))
        for row in rows
    )


def main() -> int:
    args = parse_args()
    config_paths = args.config_paths or [str(DEFAULT_CFG_PATH)]
    results = [
        evaluate_config(
            config_path=config_path,
            db_path=args.db_path,
            hours=args.hours,
            start_cash=args.start_cash,
            turnover_penalty_rate=args.turnover_penalty_rate,
            simulated_fee_rate=args.simulated_fee_rate,
            min_trade_tao=args.min_trade_tao,
            netuids=args.netuids,
        )
        for config_path in config_paths
    ]
    results.sort(key=lambda result: result.objective, reverse=True)

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2, sort_keys=True))
    else:
        print(_render_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
