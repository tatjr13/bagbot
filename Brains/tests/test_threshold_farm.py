"""Tests for threshold_farm regime classification and threshold computation."""

import unittest
import time
import tempfile
import os
from unittest.mock import patch

from Brains import config
from Brains.models import SignalSnapshot, SubnetState
from Brains.state import PriceBarStore
from Brains.threshold_farm import classify_regime, compute_signals, compute_thresholds
from Brains.risk import get_preset


def make_snap(**overrides):
    """Helper to create a SignalSnapshot with defaults."""
    defaults = dict(
        netuid=11,
        spot_price=1.0,
        ema_72h=1.0,
        ema_distance=0.0,
        ema_slope_24h=0.0,
        range_pos_24h=0.5,
        range_pos_72h=0.5,
        momentum_6h=0.0,
        volatility_24h=0.02,
        volume_score=1.0,
        inventory_ratio=0.3,
        est_slippage_pct=0.1,
        confidence=1.0,
        tao_in_pool=5000.0,
        alpha_in_pool=5000.0,
        ema_fast=1.0,
        ema_fast_distance=0.0,
        ema_fast_slope_6h=0.0,
        ema_fast_slow_spread=0.0,
    )
    defaults.update(overrides)
    return SignalSnapshot(**defaults)


class TestRegimeClassification(unittest.TestCase):

    def test_pump(self):
        snap = make_snap(range_pos_24h=0.90, momentum_6h=0.08)
        self.assertEqual(classify_regime(snap), 'pump')

    def test_bull(self):
        snap = make_snap(ema_slope_24h=0.02, spot_price=1.05, ema_72h=1.0)
        self.assertEqual(classify_regime(snap), 'bull')

    def test_fast_crossover_can_signal_bull(self):
        snap = make_snap(
            spot_price=1.02,
            ema_fast=1.01,
            ema_fast_slow_spread=0.01,
            ema_fast_slope_6h=0.005,
        )
        self.assertEqual(classify_regime(snap), 'bull')

    def test_bear(self):
        snap = make_snap(ema_slope_24h=-0.02, spot_price=0.95, ema_72h=1.0)
        self.assertEqual(classify_regime(snap), 'bear')

    def test_chop(self):
        snap = make_snap(ema_slope_24h=0.005, spot_price=1.0, ema_72h=1.0,
                         range_pos_24h=0.5, momentum_6h=0.01)
        self.assertEqual(classify_regime(snap), 'chop')

    def test_pump_takes_priority_over_bull(self):
        # Both pump and bull conditions met - pump should win (checked first)
        snap = make_snap(
            range_pos_24h=0.90, momentum_6h=0.08,
            ema_slope_24h=0.02, spot_price=1.05, ema_72h=1.0
        )
        self.assertEqual(classify_regime(snap), 'pump')

    def test_near_boundary_not_pump(self):
        snap = make_snap(range_pos_24h=0.84, momentum_6h=0.07)
        self.assertNotEqual(classify_regime(snap), 'pump')


class TestComputeThresholds(unittest.TestCase):

    def setUp(self):
        self.preset = get_preset('conservative')
        self.state = SubnetState(netuid=11)
        self.now = time.time()
        # Set last_patch_at far enough back to pass cooldown
        self.state.last_patch_at = self.now - 3600
        self.default_cfg = {
            'warmup_min_hours': 0,
            'warmup_full_hours': 0,
            'freeze_buys_if_confidence_lt': 0.50,
            'de_risk_only_if_confidence_lt': 0.70,
            'trade_only_if_confidence_gte': 0.70,
            'min_roundtrip_edge_pct': 0.02,
            'max_threshold_shift_pct_per_tick': 0.005,
            'max_daily_turnover_ratio': 0.15,
            'min_minutes_between_threshold_updates': 15,
            'min_minutes_between_trades_per_subnet': 60,
        }

    def test_buy_band_below_sell_band(self):
        snap = make_snap(confidence=1.0)
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(patch)
        self.assertLess(patch.buy_upper, patch.sell_lower)

    def test_sell_lower_never_below_cost_floor(self):
        snap = make_snap(confidence=1.0, ema_72h=1.0)
        self.state.avg_entry_price = 1.05  # bought high
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(patch)
        # cost_floor = 1.05 * (1 + 0.02) = 1.071
        # sell_lower should be >= cost_floor
        expected_floor = 1.05 * (1 + 0.02)
        self.assertGreaterEqual(patch.sell_lower, expected_floor - 0.001)

    def test_pump_disables_buys(self):
        snap = make_snap(range_pos_24h=0.90, momentum_6h=0.08, confidence=1.0)
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(patch)
        self.assertFalse(patch.enable_buys)
        self.assertEqual(patch.regime, 'pump')

    def test_low_confidence_disables_buys(self):
        snap = make_snap(confidence=0.4)
        with patch.object(config, 'load_config', return_value=self.default_cfg):
            patch_result = compute_thresholds(
                snap, self.state, self.preset,
                original_grid={'max_alpha': 2000},
                daily_buy_tao=0, daily_sell_tao=0,
                portfolio_value_tao=100,
                dry_run=True, now=self.now,
            )
        self.assertIsNotNone(patch_result)
        self.assertFalse(patch_result.enable_buys)

    def test_medium_confidence_disables_buys(self):
        snap = make_snap(confidence=0.65)
        with patch.object(config, 'load_config', return_value=self.default_cfg):
            patch_result = compute_thresholds(
                snap, self.state, self.preset,
                original_grid={'max_alpha': 2000},
                daily_buy_tao=0, daily_sell_tao=0,
                portfolio_value_tao=100,
                dry_run=True, now=self.now,
            )
        self.assertIsNotNone(patch_result)
        self.assertFalse(patch_result.enable_buys)

    def test_custom_confidence_gates_can_allow_buys_earlier(self):
        snap = make_snap(confidence=0.20)
        test_cfg = {
            'warmup_min_hours': 0,
            'warmup_full_hours': 0,
            'freeze_buys_if_confidence_lt': 0.10,
            'de_risk_only_if_confidence_lt': 0.15,
            'trade_only_if_confidence_gte': 0.70,
            'min_roundtrip_edge_pct': 0.02,
            'max_threshold_shift_pct_per_tick': 0.005,
            'max_daily_turnover_ratio': 0.15,
            'min_minutes_between_threshold_updates': 15,
            'min_minutes_between_trades_per_subnet': 60,
        }
        with patch.object(config, 'load_config', return_value=test_cfg):
            patch_result = compute_thresholds(
                snap, self.state, self.preset,
                original_grid={'max_alpha': 2000},
                daily_buy_tao=0, daily_sell_tao=0,
                portfolio_value_tao=100,
                dry_run=True, now=self.now,
            )
        self.assertIsNotNone(patch_result)
        self.assertTrue(patch_result.enable_buys)

    def test_cooldown_blocks_update(self):
        """If last patch was too recent, returns None."""
        self.state.last_patch_at = self.now - 60  # only 1 minute ago
        snap = make_snap(confidence=1.0)
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNone(patch)

    def test_dry_run_flag(self):
        snap = make_snap(confidence=1.0)
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(patch)
        self.assertTrue(patch.dry_run)

    def test_threshold_values_are_reasonable(self):
        snap = make_snap(spot_price=0.015, ema_72h=0.015, confidence=1.0)
        patch = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(patch)
        # All thresholds should be positive
        self.assertGreater(patch.buy_lower, 0)

    def test_fast_flip_signal_nudges_buy_thresholds_higher(self):
        neutral_snap = make_snap(
            spot_price=1.0,
            ema_72h=1.0,
            ema_distance=-0.02,
            ema_fast=0.99,
            ema_fast_distance=0.01,
            ema_fast_slope_6h=0.0,
            ema_fast_slow_spread=0.0,
            confidence=1.0,
        )
        flip_snap = make_snap(
            spot_price=1.0,
            ema_72h=1.0,
            ema_distance=-0.02,
            ema_fast=0.995,
            ema_fast_distance=0.005,
            ema_fast_slope_6h=0.01,
            ema_fast_slow_spread=0.01,
            confidence=1.0,
        )
        neutral_patch = compute_thresholds(
            neutral_snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        fresh_state = SubnetState(netuid=11, last_patch_at=self.now - 3600)
        flip_patch = compute_thresholds(
            flip_snap, fresh_state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
        )
        self.assertIsNotNone(neutral_patch)
        self.assertIsNotNone(flip_patch)
        self.assertGreater(flip_patch.buy_upper, neutral_patch.buy_upper)
        self.assertIn('fast_flip=', flip_patch.reason)
        self.assertGreater(flip_patch.buy_upper, 0)
        self.assertGreater(flip_patch.sell_lower, 0)
        self.assertGreater(flip_patch.sell_upper, 0)
        self.assertLess(flip_patch.buy_upper, flip_patch.sell_lower)

    def test_zero_warmup_config_does_not_block_or_crash(self):
        snap = make_snap(confidence=0.2)
        test_cfg = {
            'warmup_min_hours': 0,
            'warmup_full_hours': 0,
            'trade_only_if_confidence_gte': 0.70,
            'min_roundtrip_edge_pct': 0.02,
            'max_threshold_shift_pct_per_tick': 0.005,
            'max_daily_turnover_ratio': 0.15,
        }
        with patch.object(config, 'load_config', return_value=test_cfg):
            patch_result = compute_thresholds(
                snap, self.state, self.preset,
                original_grid={'max_alpha': 2000},
                daily_buy_tao=0, daily_sell_tao=0,
                portfolio_value_tao=100,
                dry_run=True, now=self.now,
            )
        self.assertIsNotNone(patch_result)

    def test_warmup_gate_requires_full_configured_ratio(self):
        snap = make_snap(confidence=0.20)
        test_cfg = {
            'warmup_min_hours': 24,
            'warmup_full_hours': 72,
            'freeze_buys_if_confidence_lt': 0.0,
            'de_risk_only_if_confidence_lt': 0.0,
            'trade_only_if_confidence_gte': 0.0,
            'min_roundtrip_edge_pct': 0.02,
            'max_threshold_shift_pct_per_tick': 0.005,
            'max_daily_turnover_ratio': 0.15,
            'min_minutes_between_threshold_updates': 15,
            'min_minutes_between_trades_per_subnet': 60,
        }
        patch_result = compute_thresholds(
            snap, self.state, self.preset,
            original_grid={'max_alpha': 2000},
            daily_buy_tao=0, daily_sell_tao=0,
            portfolio_value_tao=100,
            dry_run=True, now=self.now,
            cfg=test_cfg,
        )
        self.assertIsNone(patch_result)


class TestComputeSignals(unittest.TestCase):

    def test_momentum_fetches_one_extra_bar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'bars.sqlite')
            bar_store = PriceBarStore(db_path)
            try:
                now = time.time()
                prices = [1.0 + (idx * 0.01) for idx in range(25)]
                for idx, price in enumerate(prices):
                    timestamp = now - (((len(prices) - 1) - idx) * 900)
                    bar_store.record_tick(
                        11, price, 5000.0, 5000.0,
                        timestamp=timestamp, bar_minutes=15,
                    )
                test_cfg = {
                    'bar_size_minutes': 15,
                    'lookbacks': {
                        'ema_hours': 72,
                        'vol_hours': 24,
                        'range_short_hours': 24,
                        'range_medium_hours': 72,
                        'momentum_short_hours': 6,
                    },
                }
                snap = compute_signals(
                    netuid=11,
                    spot_price=prices[-1],
                    tao_in=5000.0,
                    alpha_in=5000.0,
                    current_alpha=0.0,
                    max_alpha=100.0,
                    max_buy_tao=1.0,
                    bar_store=bar_store,
                    now=now,
                    cfg=test_cfg,
                )
            finally:
                bar_store.close()

        expected_momentum = (prices[-1] - prices[0]) / prices[0]
        self.assertAlmostEqual(snap.momentum_6h, expected_momentum)


if __name__ == '__main__':
    unittest.main()
