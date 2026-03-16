"""Tests for threshold_farm regime classification and threshold computation."""

import unittest
import time
import tempfile
import os
from unittest.mock import patch

from Brains import config
from Brains.models import SignalSnapshot, SubnetState
from Brains.threshold_farm import classify_regime, compute_thresholds
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
        self.assertGreater(patch.buy_upper, 0)
        self.assertGreater(patch.sell_lower, 0)
        self.assertGreater(patch.sell_upper, 0)
        # Buy band should be below sell band
        self.assertLess(patch.buy_upper, patch.sell_lower)

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


if __name__ == '__main__':
    unittest.main()
