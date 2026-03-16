"""Smoke tests for the offline research harness."""

import os
import tempfile
import time
import unittest

import yaml

from Brains.research_harness import evaluate_config
from Brains.state import PriceBarStore


def _test_cfg():
    return {
        'enabled': True,
        'dry_run': False,
        'risk_mode_default': 'aggressive',
        'bar_size_minutes': 15,
        'max_live_subnets': 1,
        'taostats_flow_enabled': False,
        'warmup_min_hours': 0,
        'warmup_full_hours': 0,
        'freeze_buys_if_confidence_lt': 0.0,
        'de_risk_only_if_confidence_lt': 0.0,
        'trade_only_if_confidence_gte': 0.0,
        'min_roundtrip_edge_pct': 0.0,
        'slippage_limit_pct': 0.5,
        'max_threshold_shift_pct_per_tick': 1.0,
        'max_daily_turnover_ratio': 10.0,
        'min_minutes_between_threshold_updates': 0,
        'min_minutes_between_trades_per_subnet': 0,
        'disable_buys_above_range_pos': 0.95,
        'disable_buys_if_6h_pump_pct': 0.20,
        'new_subnet_min_age_days': 0,
        'min_liquidity_tao': 150,
        'dynamic_max_alpha_tao': 25.0,
        'dynamic_buy_upper_discount_pct': 0.005,
        'dynamic_buy_lower_discount_pct': 0.020,
        'dynamic_sell_lower_premium_pct': 0.005,
        'dynamic_sell_upper_premium_pct': 0.030,
        'lookbacks': {
            'ema_hours': 12,
            'vol_hours': 6,
            'range_short_hours': 6,
            'range_medium_hours': 12,
            'momentum_short_hours': 6,
        },
    }


class TestResearchHarness(unittest.TestCase):
    def test_evaluate_config_replays_history_and_emits_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'bars.sqlite')
            cfg_path = os.path.join(tmpdir, 'candidate.yaml')
            with open(cfg_path, 'w', encoding='utf-8') as handle:
                yaml.safe_dump(_test_cfg(), handle)

            bar_store = PriceBarStore(db_path)
            try:
                now = time.time()
                prices = [
                    1.10, 1.08, 1.06, 1.04, 1.02, 1.00, 0.98, 0.96,
                    0.95, 0.96, 0.98, 1.00, 1.03, 1.06, 1.09, 1.12,
                ]
                for idx, price in enumerate(prices):
                    timestamp = now - ((len(prices) - idx) * 900)
                    bar_store.record_tick(
                        11, price, 7000.0, 7000.0,
                        timestamp=timestamp, bar_minutes=15,
                    )
            finally:
                bar_store.close()

            result = evaluate_config(
                config_path=cfg_path,
                db_path=db_path,
                hours=8.0,
                start_cash=10.0,
                turnover_penalty_rate=0.001,
                simulated_fee_rate=0.0005,
                min_trade_tao=0.01,
                netuids=None,
            )

        self.assertGreater(result.bars, 0)
        self.assertEqual(result.netuids, 1)
        self.assertGreaterEqual(result.final_value_tao, 0.0)
        self.assertGreaterEqual(result.trades, 0)


if __name__ == '__main__':
    unittest.main()
