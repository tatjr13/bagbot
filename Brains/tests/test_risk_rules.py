"""Tests for risk guardrails, clamping, and universe filters."""

import unittest
import time
import tempfile
import os
from unittest.mock import patch

from Brains import config
from Brains.risk import (
    get_preset, dynamic_min_edge, apply_regime_adjustments,
    clamp_threshold_shift, check_daily_turnover,
    passes_subnet_universe_filter,
)
from Brains.models import SubnetState


class TestDynamicMinEdge(unittest.TestCase):

    def test_low_slippage_uses_config(self):
        # est_slippage 0.001 -> 2*0.001+0.003 = 0.005 < 0.02 -> use 0.02
        self.assertAlmostEqual(dynamic_min_edge(0.02, 0.001), 0.02)

    def test_high_slippage_raises_edge(self):
        # est_slippage 0.015 -> 2*0.015+0.003 = 0.033 > 0.02 -> use 0.033
        self.assertAlmostEqual(dynamic_min_edge(0.02, 0.015), 0.033)

    def test_zero_slippage(self):
        self.assertAlmostEqual(dynamic_min_edge(0.02, 0.0), 0.02)


class TestRegimeAdjustments(unittest.TestCase):

    def test_pump_disables_buys(self):
        result = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='pump', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=1.0,
        )
        buy_size_mult = result[4]
        enable_buys = result[6]
        self.assertEqual(buy_size_mult, 0.0)
        self.assertFalse(enable_buys)

    def test_high_inventory_widens_buy(self):
        # At 85% inventory
        result_high = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='chop', inventory_pct=0.85,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=1.0,
        )
        result_low = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='chop', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=1.0,
        )
        # High inventory should make buy offsets more negative (buy cheaper)
        self.assertLess(result_high[0], result_low[0])

    def test_high_slippage_reduces_buy_size(self):
        result = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='chop', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.6,
            confidence=1.0,
        )
        self.assertAlmostEqual(result[4], 0.5)  # buy_size_mult halved
        self.assertAlmostEqual(result[5], 0.75)  # sell_size_mult * 0.75

    def test_low_confidence_disables_buys(self):
        result = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='chop', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=0.4,
        )
        self.assertFalse(result[6])  # enable_buys = False

    def test_medium_confidence_caps_buy_size(self):
        result = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='chop', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=0.6,
        )
        self.assertLessEqual(result[4], 0.5)  # buy_size_mult capped

    def test_adjustments_scaled_by_confidence(self):
        # Bull at 50% confidence should adjust less than at 100%
        result_full = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='bull', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=1.0,
        )
        result_half = apply_regime_adjustments(
            -0.06, -0.025, 0.02, 0.05,
            regime='bull', inventory_pct=0.3,
            vol_mult=1.0, est_slippage_pct=0.1,
            confidence=0.8,  # > 0.7 so buys still enabled
        )
        # Full confidence bull shifts sell_high more
        self.assertGreater(result_full[3], result_half[3])


class TestClampThresholdShift(unittest.TestCase):

    def test_no_previous(self):
        self.assertEqual(clamp_threshold_shift(1.0, None, 0.005), 1.0)

    def test_within_range(self):
        # 0.5% of 1.0 = 0.005
        self.assertAlmostEqual(clamp_threshold_shift(1.003, 1.0, 0.005), 1.003)

    def test_clamped_up(self):
        # Trying to move from 1.0 to 1.01 (1%), but max is 0.5%
        result = clamp_threshold_shift(1.01, 1.0, 0.005)
        self.assertAlmostEqual(result, 1.005)

    def test_clamped_down(self):
        result = clamp_threshold_shift(0.99, 1.0, 0.005)
        self.assertAlmostEqual(result, 0.995)

    def test_zero_old_val(self):
        self.assertEqual(clamp_threshold_shift(1.0, 0.0, 0.005), 1.0)


class TestDailyTurnover(unittest.TestCase):

    def test_under_limit(self):
        can_buy, can_sell = check_daily_turnover(
            daily_buy_tao=1.0, daily_sell_tao=1.0,
            portfolio_value_tao=100.0, max_ratio=0.15,
        )
        self.assertTrue(can_buy)
        self.assertTrue(can_sell)

    def test_over_limit(self):
        can_buy, can_sell = check_daily_turnover(
            daily_buy_tao=20.0, daily_sell_tao=20.0,
            portfolio_value_tao=100.0, max_ratio=0.15,
        )
        self.assertFalse(can_buy)
        self.assertFalse(can_sell)

    def test_zero_portfolio(self):
        can_buy, can_sell = check_daily_turnover(
            daily_buy_tao=1.0, daily_sell_tao=1.0,
            portfolio_value_tao=0.0, max_ratio=0.15,
        )
        self.assertTrue(can_buy)
        self.assertTrue(can_sell)


class TestSubnetUniverseFilter(unittest.TestCase):

    def test_passes_good_subnet(self):
        ok, reason = passes_subnet_universe_filter(
            netuid=11, tao_in_pool=5000.0,
            history_hours=48, max_buy_tao=0.1,
            allowed_netuids={11},
        )
        self.assertTrue(ok)

    def test_fails_low_liquidity(self):
        ok, reason = passes_subnet_universe_filter(
            netuid=11, tao_in_pool=50.0,
            history_hours=48, max_buy_tao=0.1,
            allowed_netuids={11},
        )
        self.assertFalse(ok)
        self.assertIn('pool too thin', reason)

    def test_fails_not_in_allowlist(self):
        ok, reason = passes_subnet_universe_filter(
            netuid=99, tao_in_pool=5000.0,
            history_hours=48, max_buy_tao=0.1,
            allowed_netuids={11, 22},
        )
        self.assertFalse(ok)
        self.assertIn('allowlist', reason)

    def test_fails_insufficient_history(self):
        test_cfg = {'min_liquidity_tao': 150, 'warmup_min_hours': 24, 'new_subnet_min_age_days': 7}
        with patch.object(config, 'load_config', return_value=test_cfg):
            ok, reason = passes_subnet_universe_filter(
                netuid=11, tao_in_pool=5000.0,
                history_hours=12, max_buy_tao=0.1,
                allowed_netuids={11},
            )
        self.assertFalse(ok)
        self.assertIn('warmup', reason)

    def test_fails_high_simulated_slippage(self):
        ok, reason = passes_subnet_universe_filter(
            netuid=11, tao_in_pool=5.0,  # tiny pool
            history_hours=48, max_buy_tao=0.1,
        )
        self.assertFalse(ok)

    def test_no_allowlist_means_all_pass(self):
        ok, reason = passes_subnet_universe_filter(
            netuid=999, tao_in_pool=5000.0,
            history_hours=48, max_buy_tao=0.1,
            allowed_netuids=None,
        )
        self.assertTrue(ok)

    def test_fails_when_true_subnet_age_is_below_minimum(self):
        test_cfg = {'min_liquidity_tao': 150, 'warmup_min_hours': 0, 'new_subnet_min_age_days': 7}
        ok, reason = passes_subnet_universe_filter(
            netuid=11, tao_in_pool=5000.0,
            history_hours=72, max_buy_tao=0.1,
            subnet_age_days=3.0,
            cfg=test_cfg,
        )
        self.assertFalse(ok)
        self.assertIn('age', reason)


class TestRiskPresets(unittest.TestCase):

    def test_conservative_exists(self):
        p = get_preset('conservative')
        self.assertEqual(p.name, 'conservative')
        self.assertLess(p.max_buy_tao, get_preset('balanced').max_buy_tao)

    def test_unknown_falls_back(self):
        p = get_preset('yolo')
        self.assertEqual(p.name, 'conservative')

    def test_canary_buy_sizing(self):
        """Conservative buy sizing should be 50% of original plan (canary period)."""
        p = get_preset('conservative')
        self.assertEqual(p.max_buy_tao, 1.5)


if __name__ == '__main__':
    unittest.main()
