"""Tests for dynamic runtime roster selection in the Brains integration."""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from Brains.integration import StrategyEngine
from Brains.state import PriceBarStore, StrategyStateStore


TEST_CFG = {
    'enabled': True,
    'dry_run': False,
    'risk_mode_default': 'aggressive',
    'bar_size_minutes': 15,
    'max_live_subnets': 1,
    'warmup_min_hours': 0,
    'warmup_full_hours': 0,
    'freeze_buys_if_confidence_lt': 0.01,
    'de_risk_only_if_confidence_lt': 0.02,
    'trade_only_if_confidence_gte': 0.02,
    'min_roundtrip_edge_pct': 0.02,
    'slippage_limit_pct': 0.005,
    'max_threshold_shift_pct_per_tick': 0.05,
    'max_daily_turnover_ratio': 0.50,
    'min_minutes_between_threshold_updates': 0,
    'min_minutes_between_trades_per_subnet': 0,
    'disable_buys_above_range_pos': 0.85,
    'disable_buys_if_6h_pump_pct': 0.06,
    'new_subnet_min_age_days': 7,
    'min_liquidity_tao': 150,
    'dynamic_max_alpha_tao': 25.0,
    'dynamic_buy_upper_discount_pct': 0.015,
    'dynamic_buy_lower_discount_pct': 0.045,
    'dynamic_sell_lower_premium_pct': 0.020,
    'dynamic_sell_upper_premium_pct': 0.080,
    'lookbacks': {
        'ema_hours': 72,
        'vol_hours': 24,
        'range_short_hours': 24,
        'range_medium_hours': 72,
        'momentum_short_hours': 6,
        'momentum_medium_hours': 24,
    },
}


class MockStake:
    def __init__(self, stake):
        self.stake = stake


class TestStrategyRuntimeRoster(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, 'bars.sqlite')
        self.state_path = os.path.join(self.tmpdir.name, 'state.json')
        self.bar_store = PriceBarStore(self.db_path)
        self.state_store = StrategyStateStore(self.state_path)
        self.settings = SimpleNamespace(
            SUBNET_SETTINGS={
                11: {
                    'buy_lower': 0.98,
                    'buy_upper': 0.99,
                    'sell_lower': 1.02,
                    'sell_upper': 1.08,
                    'max_alpha': 100.0,
                },
            },
            BRAINS_DRY_RUN=False,
        )

    def tearDown(self):
        self.bar_store.close()
        self.tmpdir.cleanup()

    def _make_engine(self):
        with patch('Brains.config.load_config', return_value=TEST_CFG), \
             patch('Brains.integration.PriceBarStore', return_value=self.bar_store), \
             patch('Brains.integration.StrategyStateStore', return_value=self.state_store), \
             patch('Brains.telegram_cmds.setup_telegram', return_value=None):
            return StrategyEngine(self.settings)

    def _seed_history(self, engine):
        now = time.time()
        prices_11 = [1.00] * 12
        prices_22 = [1.05, 1.04, 1.03, 1.02, 1.01, 1.00, 0.99, 0.98, 0.97, 0.95, 0.93, 0.90]
        for idx, (p11, p22) in enumerate(zip(prices_11, prices_22)):
            timestamp = now - ((len(prices_11) - idx) * 900)
            engine.bar_store.record_tick(11, p11, 5000.0, 5000.0, timestamp=timestamp, bar_minutes=15)
            engine.bar_store.record_tick(22, p22, 7000.0, 7000.0, timestamp=timestamp, bar_minutes=15)

    def _run_tick(self, engine, stats, stake_info, balance):
        with patch('Brains.config.load_config', return_value=TEST_CFG):
            engine.on_tick(stats, self.settings.SUBNET_SETTINGS, stake_info=stake_info, balance=balance)

    def test_dynamic_candidate_can_replace_seed_roster(self):
        engine = self._make_engine()
        self._seed_history(engine)

        stats = {
            11: {'price': 1.00, 'tao_in': 5000.0, 'alpha_in': 5000.0},
            22: {'price': 0.90, 'tao_in': 7000.0, 'alpha_in': 7000.0},
        }

        self._run_tick(engine, stats, stake_info={}, balance=10.0)
        runtime_grids = engine.get_runtime_subnet_grids(self.settings.SUBNET_SETTINGS)

        self.assertEqual(list(runtime_grids.keys()), [22])
        self.assertIn(22, engine.buy_roster_netuids)

    def test_held_subnet_stays_managed_even_when_not_top_pick(self):
        engine = self._make_engine()
        self._seed_history(engine)

        stats = {
            11: {'price': 1.00, 'tao_in': 5000.0, 'alpha_in': 5000.0},
            22: {'price': 0.90, 'tao_in': 7000.0, 'alpha_in': 7000.0},
        }
        stake_info = {
            'somehotkey': {
                11: MockStake(10.0),
            }
        }

        self._run_tick(engine, stats, stake_info=stake_info, balance=10.0)
        runtime_grids = engine.get_runtime_subnet_grids(self.settings.SUBNET_SETTINGS)

        self.assertEqual(list(runtime_grids.keys()), [11, 22])
        self.assertIn(11, engine.runtime_netuids)
        self.assertIn(22, engine.buy_roster_netuids)
        self.assertFalse(engine.get_patch(11).enable_buys)

    def test_runtime_roster_persists_across_threshold_cooldown_ticks(self):
        engine = self._make_engine()
        self._seed_history(engine)

        stats = {
            11: {'price': 1.00, 'tao_in': 5000.0, 'alpha_in': 5000.0},
            22: {'price': 0.90, 'tao_in': 7000.0, 'alpha_in': 7000.0},
        }

        self._run_tick(engine, stats, stake_info={}, balance=10.0)
        self.assertEqual(engine.runtime_ordered_netuids, [22])
        self.assertIn(22, engine.buy_roster_netuids)
        first_patch = engine.get_patch(22)
        self.assertIsNotNone(first_patch)

        cooldown_cfg = dict(TEST_CFG)
        cooldown_cfg['min_minutes_between_threshold_updates'] = 60
        with patch('Brains.config.load_config', return_value=cooldown_cfg):
            engine.on_tick(stats, self.settings.SUBNET_SETTINGS, stake_info={}, balance=10.0)

        runtime_grids = engine.get_runtime_subnet_grids(self.settings.SUBNET_SETTINGS)
        self.assertEqual(list(runtime_grids.keys()), [22])
        self.assertEqual(engine.runtime_ordered_netuids, [22])
        self.assertIn(22, engine.buy_roster_netuids)
        self.assertIs(engine.get_patch(22), first_patch)


if __name__ == '__main__':
    unittest.main()
