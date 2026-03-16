"""Tests for Brains signal computation functions."""

import unittest
import math
from Brains.signals import (
    ema, ema_distance, ema_slope, range_position,
    momentum, volatility, volume_score, estimate_slippage_pct,
    compute_confidence, inventory_ratio,
)


class TestEMA(unittest.TestCase):

    def test_single_value(self):
        self.assertEqual(ema([5.0], 10), 5.0)

    def test_empty_list(self):
        self.assertIsNone(ema([], 10))

    def test_known_series(self):
        # EMA of [1, 2, 3, 4, 5] with span=3 -> k=0.5
        # ema[0]=1, ema[1]=1.5, ema[2]=2.25, ema[3]=3.125, ema[4]=4.0625
        result = ema([1, 2, 3, 4, 5], 3)
        self.assertAlmostEqual(result, 4.0625, places=4)

    def test_constant_series(self):
        result = ema([3.0] * 20, 10)
        self.assertAlmostEqual(result, 3.0, places=6)

    def test_short_series_returns_best_effort(self):
        # With only 3 bars for a 72-bar EMA, still computes (just less smoothed)
        result = ema([1.0, 1.1, 1.2], 288)
        self.assertIsNotNone(result)
        self.assertGreater(result, 1.0)


class TestEMADistance(unittest.TestCase):

    def test_above_ema(self):
        d = ema_distance(1.1, 1.0)
        self.assertAlmostEqual(d, 0.1, places=6)

    def test_below_ema(self):
        d = ema_distance(0.9, 1.0)
        self.assertAlmostEqual(d, -0.1, places=6)

    def test_at_ema(self):
        self.assertAlmostEqual(ema_distance(1.0, 1.0), 0.0)

    def test_zero_ema(self):
        self.assertEqual(ema_distance(1.0, 0.0), 0.0)


class TestEMASlope(unittest.TestCase):

    def test_uptrend(self):
        prices = list(range(1, 101))  # 1..100 rising
        slope = ema_slope([float(p) for p in prices], span_bars=20, lookback_bars=30)
        self.assertGreater(slope, 0)

    def test_downtrend(self):
        prices = list(range(100, 0, -1))  # 100..1 falling
        slope = ema_slope([float(p) for p in prices], span_bars=20, lookback_bars=30)
        self.assertLess(slope, 0)

    def test_insufficient_data(self):
        slope = ema_slope([1.0, 2.0], span_bars=10, lookback_bars=5)
        self.assertEqual(slope, 0.0)

    def test_flat(self):
        slope = ema_slope([1.0] * 100, span_bars=20, lookback_bars=30)
        self.assertAlmostEqual(slope, 0.0, places=6)


class TestRangePosition(unittest.TestCase):

    def test_at_high(self):
        prices = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(range_position(prices), 1.0)

    def test_at_low(self):
        prices = [5.0, 4.0, 3.0, 2.0, 1.0]
        self.assertAlmostEqual(range_position(prices), 0.0)

    def test_at_midpoint(self):
        prices = [1.0, 5.0, 3.0]
        self.assertAlmostEqual(range_position(prices), 0.5)

    def test_flat_returns_half(self):
        self.assertEqual(range_position([3.0, 3.0, 3.0]), 0.5)

    def test_single_value_returns_half(self):
        self.assertEqual(range_position([3.0]), 0.5)

    def test_empty_returns_half(self):
        self.assertEqual(range_position([]), 0.5)


class TestMomentum(unittest.TestCase):

    def test_positive_momentum(self):
        prices = [1.0, 1.1, 1.2, 1.3, 1.4]
        mom = momentum(prices, lookback_bars=4)
        self.assertAlmostEqual(mom, 0.4, places=4)

    def test_zero_momentum(self):
        mom = momentum([1.0, 1.0, 1.0], lookback_bars=2)
        self.assertAlmostEqual(mom, 0.0)

    def test_insufficient_data(self):
        self.assertEqual(momentum([1.0], lookback_bars=5), 0.0)


class TestVolatility(unittest.TestCase):

    def test_zero_volatility(self):
        self.assertAlmostEqual(volatility([1.0, 1.0, 1.0]), 0.0)

    def test_known_volatility(self):
        # [1, 2, 3]: mean=2, variance=2/3, std=0.8165, cv=0.4082
        vol = volatility([1.0, 2.0, 3.0])
        self.assertAlmostEqual(vol, math.sqrt(2.0 / 3.0) / 2.0, places=4)

    def test_single_value(self):
        self.assertEqual(volatility([1.0]), 0.0)


class TestVolumeScore(unittest.TestCase):

    def test_normal_volume(self):
        values = [100.0] * 96
        self.assertAlmostEqual(volume_score(values), 1.0)

    def test_high_recent_volume(self):
        values = [100.0] * 92 + [200.0] * 4
        score = volume_score(values)
        self.assertGreater(score, 1.0)

    def test_insufficient_data(self):
        self.assertEqual(volume_score([1.0, 2.0]), 1.0)


class TestEstimateSlippage(unittest.TestCase):

    def test_small_trade(self):
        slippage = estimate_slippage_pct(1.0, 10000.0)
        self.assertAlmostEqual(slippage, 0.01, places=2)

    def test_large_trade(self):
        slippage = estimate_slippage_pct(100.0, 100.0)
        self.assertAlmostEqual(slippage, 50.0, places=1)

    def test_empty_pool(self):
        self.assertEqual(estimate_slippage_pct(1.0, 0.0), 100.0)


class TestConfidence(unittest.TestCase):

    def test_full_confidence(self):
        self.assertEqual(compute_confidence(100, 100), 1.0)

    def test_partial_confidence(self):
        self.assertAlmostEqual(compute_confidence(50, 100), 0.5)

    def test_over_ideal(self):
        self.assertEqual(compute_confidence(200, 100), 1.0)


class TestInventoryRatio(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(inventory_ratio(0, 1000), 0.0)

    def test_full(self):
        self.assertEqual(inventory_ratio(1000, 1000), 1.0)

    def test_over_max(self):
        self.assertEqual(inventory_ratio(2000, 1000), 1.0)

    def test_zero_max(self):
        self.assertEqual(inventory_ratio(100, 0), 0.0)


if __name__ == '__main__':
    unittest.main()
