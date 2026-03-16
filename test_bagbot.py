import unittest
import bagbot
import math
import os
from unittest.mock import patch



"""
class MockExchange(Exchange):
    def fetchTickerMap(self, *args):
        pass
    def fetchCurrentSnapshotData(self, *args):
        pass
"""

class TestBAGBot(unittest.TestCase):

    def setUp(self):
        # Pin globals to known defaults so tests pass regardless of overrides file
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = 0.02
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 0.02
        bagbot.bagbot_settings.MAX_SLIPPAGE_PERCENT_PER_BUY = 0.2
        bagbot.bagbot_settings.BUY_ZONE_POWER = 1.0
        bagbot.bagbot_settings.SELL_ZONE_POWER = 1.0
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = None
        bagbot.bagbot_settings.MIN_TAO_RESERVE = 0.0
        bagbot.bagbot_settings.EXECUTION_FEE_BUFFER_TAO = 0.0
        bagbot.bagbot_settings.MAX_SUBNET_ALLOCATION_RATIO = None
        bagbot.bagbot_settings.WALLET_PW_ENV = None
        bagbot.bagbot_settings.WALLET_PW_FILE = None
        bagbot.bagbot_settings.STAKE_ON_VALIDATOR = 'somehotkey'
        bagbot.bagbot_settings.ENABLE_POSITION_ROTATION = False
        bagbot.bagbot_settings.ENABLE_ATOMIC_ROTATION = True
        bagbot.bagbot_settings.ENABLE_MEV_PROTECTION = False
        bagbot.bagbot_settings.ROTATION_REQUIRE_CONSTRAINTS = False
        bagbot.bagbot_settings.ROTATION_TARGET_DISCOUNT_PCT = 0.02
        bagbot.bagbot_settings.ROTATION_SOURCE_WEAKNESS_PCT = 0.02
        bagbot.bagbot_settings.ROTATION_MIN_NET_EDGE_PCT = 0.01
        bagbot.bagbot_settings.ROTATION_EXTRINSIC_FEE_BUFFER_TAO = 0.0002

    def testNoSellPriceException(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01}
        bu.subnet_grids = {90:{'buy_upper':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)

    def testNoBuyPriceException(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01}
        bu.subnet_grids = {90:{'sell_lower':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)

    def testBadSettings1(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01}
        bu.subnet_grids = {90:{'sell_lower':0.005, 'buy_upper':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)

    def testBadSettings2(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01}
        bu.subnet_grids = {'90':{'sell_lower':0.05, 'buy_upper':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)

    def testNoMaxAlpha(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01}
        bu.subnet_grids = {90:{'sell_lower':0.05, 'buy_upper':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)


    def testNoSupportFor0(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[0] = {'price' : 0.01}
        bu.subnet_grids = {0:{'sell_lower':0.05, 'buy_upper':0.01}}
        self.assertRaises(bagbot.InvalidSettings, bu.validateGrid)






    def testBuyBaseCase(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01, 'tao_in':10000}
        bu.balance = 1
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'sell_lower':0.03,
                               'max_alpha':3000,
                          }}
        buyDict = bu.constructBuy(90)
        self.assertEqual(buyDict['netuid'], 90)
        self.assertEqual(float(buyDict['tao_amount']), 0.02)


    def testBuyLineBaseCase(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.01, 'tao_in':10000}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'buy_lower':0.01,
                               'sell_lower':0.03,
                               'max_alpha':1000,
                          }}
        buyDict = bu.constructBuy(90)
        self.assertEqual(buyDict['netuid'], 90)
        self.assertEqual(float(buyDict['tao_amount']), 0.02)
        #Have 100, want 1000, so we're 10% of the way there, 10% in between the buy_upper and buy_lower is 0.019
        self.assertTrue(math.isclose(buyDict['buy_threshold'], 0.019))



    def testSellBaseCase(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 0.02
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.04, 'alpha_in':10000}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'stake_on_validator':'somehotkey',
                               'sell_lower':0.03,
                               'max_alpha':1000,
                          }}
        sellDict = bu.constructSell(90)
        self.assertEqual(sellDict['netuid'], 90)
        #0.02 TAO per sell, price of 0.04,
        #0.02 / 0.04 = 0.5 alpha to sell
        self.assertEqual(float(sellDict['alpha_amount']), 0.5)
        self.assertEqual(float(sellDict['sell_threshold']), 0.03)


    def testSellLineBaseCase(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 0.02
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.02, 'alpha_in':10000}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90:{'buy_upper':0.001,
                               'stake_on_validator':'somehotkey',
                               'sell_lower':0.01,
                               'sell_upper':0.02,
                               'max_alpha':1000,
                          }}
        sellDict = bu.constructSell(90)
        self.assertEqual(sellDict['netuid'], 90)
        #0.02 TAO per sell, price of 0.02,
        #0.02 / 0.02 = 1 alpha to sell
        self.assertEqual(float(sellDict['alpha_amount']), 1)
        self.assertTrue(math.isclose(float(sellDict['sell_threshold']), 0.019))


    def testSellLineOverMaxAlpha(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 0.02
        bu = bagbot.BittensorUtility(args)
        bu.stats = {}
        bu.stats[90] = {'price' : 0.02, 'alpha_in':10000}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(2000)}}
        bu.subnet_grids = {90:{'buy_upper':0.001,
                               'stake_on_validator':'somehotkey',
                               'sell_lower':0.01,
                               'sell_upper':0.02,
                               'max_alpha':1000,
                          }}
        sellDict = bu.constructSell(90)
        self.assertEqual(sellDict['netuid'], 90)
        #0.02 TAO per sell, price of 0.02,
        #0.02 / 0.02 = 1 alpha to sell
        self.assertEqual(float(sellDict['alpha_amount']), 1)
        self.assertTrue(math.isclose(float(sellDict['sell_threshold']), 0.01))


    def testDetermineSlippagePythonBS(self):
        args = {}
        bu = bagbot.BittensorUtility(args)

        ratio = (0.1 + 0.2) * 0.1
        token_amount = 1.0
        token_in_pool = token_amount / ratio - token_amount
        # Without fixes this returns 3.0000000000000004 instead of 3

        # Compute slippage
        slippage = bu.determineSlippage(token_amount, token_in_pool)
        self.assertEqual(slippage, 3)


    def testBuyPowerCurveLinear(self):
        """Test that power curve = 1.0 gives linear behavior"""
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'buy_lower':0.01,
                               'sell_lower':0.03,
                               'max_alpha':1000,
                               'buy_zone_power': 1.0,
                          }}
        # At 50% progress (500 alpha), should be at midpoint
        buy_threshold = bu.determine_buy_at_for_amount(bu.subnet_grids[90], 500)
        self.assertTrue(math.isclose(buy_threshold, 0.015))


    def testBuyPowerCurveAggressive(self):
        """Test that power curve > 1.0 keeps price higher for longer"""
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'buy_lower':0.01,
                               'sell_lower':0.03,
                               'max_alpha':1000,
                               'buy_zone_power': 2.0,
                          }}
        # At 50% progress (500 alpha), with power=2.0, curve_value = 0.5^2 = 0.25
        # buy_at = 0.02 - (0.02 - 0.01) * 0.25 = 0.02 - 0.0025 = 0.0175
        buy_threshold = bu.determine_buy_at_for_amount(bu.subnet_grids[90], 500)
        self.assertTrue(math.isclose(buy_threshold, 0.0175))


    def testBuyPowerCurveConservative(self):
        """Test that power curve < 1.0 drops price faster"""
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'buy_lower':0.01,
                               'sell_lower':0.03,
                               'max_alpha':1000,
                               'buy_zone_power': 0.5,
                          }}
        # At 50% progress (500 alpha), with power=0.5, curve_value = 0.5^0.5 = ~0.707
        # buy_at = 0.02 - (0.02 - 0.01) * 0.707 = 0.02 - 0.00707 = ~0.01293
        buy_threshold = bu.determine_buy_at_for_amount(bu.subnet_grids[90], 500)
        self.assertTrue(math.isclose(buy_threshold, 0.01293, rel_tol=0.001))

    def testBuyBlockedByPortfolioCap(self):
        args = {}
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = 1.0
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'sell_lower':0.03,
                               'max_alpha':3000,
                          }}
        buyDict = bu.constructBuy(90)
        self.assertIsNone(buyDict)

    def testBuySizedDownByPortfolioCap(self):
        args = {}
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = 1.015
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90:{'buy_upper':0.02,
                               'sell_lower':0.03,
                               'max_alpha':3000,
                          }}
        buyDict = bu.constructBuy(90)
        self.assertTrue(math.isclose(float(buyDict['tao_amount']), 0.015, rel_tol=1e-6))

    def testBuyBlockedByFeeReserve(self):
        args = {}
        bagbot.bagbot_settings.MIN_TAO_RESERVE = 0.995
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90)
        self.assertIsNone(buyDict)

    def testBuySizedDownByFeeReserve(self):
        args = {}
        bagbot.bagbot_settings.MIN_TAO_RESERVE = 0.985
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                          }}
        buyDict = bu.constructBuy(90)
        self.assertTrue(math.isclose(float(buyDict['tao_amount']), 0.015, rel_tol=1e-6))

    def testBuySizedDownByExecutionFeeBuffer(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = None
        bagbot.bagbot_settings.EXECUTION_FEE_BUFFER_TAO = 0.015
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90)
        self.assertTrue(math.isclose(float(buyDict['tao_amount']), 0.985, rel_tol=1e-6))

    def testBuyUsesAvailableBalanceWhenMaxBuyIsUnbounded(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = None
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90)
        self.assertTrue(math.isclose(float(buyDict['tao_amount']), 1.0, rel_tol=1e-6))

    def testBuyBlockedBySubnetAllocationCap(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = None
        bagbot.bagbot_settings.MAX_SUBNET_ALLOCATION_RATIO = 0.60
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 0.50
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90)
        self.assertIsNone(buyDict)

    def testBuySizedDownBySubnetAllocationCap(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = None
        bagbot.bagbot_settings.MAX_SUBNET_ALLOCATION_RATIO = 0.75
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 0.50
        bu.current_stake_info = {'somehotkey': {90: MockStake(100)}}
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90)
        self.assertTrue(math.isclose(float(buyDict['tao_amount']), 0.125, rel_tol=1e-6))

    def testResolveWalletPasswordFromEnv(self):
        settings = bagbot.SimpleNamespace(
            WALLET_PW='ignored',
            WALLET_PW_ENV='BAGBOT_TEST_WALLET_PW',
            WALLET_PW_FILE=None,
        )
        with patch.dict(os.environ, {'BAGBOT_TEST_WALLET_PW': 'secret-from-env'}, clear=False):
            self.assertEqual(bagbot.resolve_wallet_password(settings), 'secret-from-env')

    def testPreviewBuyDoesNotCrashAtSlippageBoundary(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = None
        bagbot.bagbot_settings.MAX_SLIPPAGE_PERCENT_PER_BUY = 0.5
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.balance = 1
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        buyDict = bu.constructBuy(90, ignore_balance_limits=True, preview_only=True)
        self.assertIsNotNone(buyDict)
        self.assertLessEqual(float(buyDict['calculated_slippage']), 0.500000001)

    def testSellUsesHeldPositionWhenMaxSellIsUnbounded(self):
        args = {}
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = None
        bu = bagbot.BittensorUtility(args)
        bu.stats = {90: {'price': 0.04, 'alpha_in': 10000}}
        bu.balance = 1
        bu.current_stake_info = {'somehotkey': {90: MockStake(2)}}
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'stake_on_validator': 'somehotkey',
                                'sell_lower': 0.03,
                                'max_alpha': 1000,
                           }}
        sellDict = bu.constructSell(90)
        self.assertTrue(math.isclose(float(sellDict['alpha_amount']), 2.0, rel_tol=1e-6))

    def testRotationTradeChoosesWeakestHeldSubnetForBetterOpportunity(self):
        args = {}
        bagbot.bagbot_settings.ENABLE_POSITION_ROTATION = True
        bagbot.bagbot_settings.ENABLE_ATOMIC_ROTATION = True
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = None
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = 0.05
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 1.0

        bu = bagbot.BittensorUtility(args)
        bu.balance = 1.0
        bu.stats = {
            90: {'price': 0.009, 'tao_in': 10000, 'alpha_in': 10000},
            91: {'price': 0.010, 'tao_in': 10000, 'alpha_in': 10000},
        }
        bu.current_stake_info = {
            bagbot.bagbot_settings.STAKE_ON_VALIDATOR: {
                91: MockStake(100),
            }
        }
        bu.subnet_grids = {
            90: {
                'buy_lower': 0.0105,
                'buy_upper': 0.011,
                'sell_lower': 0.013,
                'sell_upper': 0.014,
                'max_alpha': 1000,
            },
            91: {
                'buy_lower': 0.008,
                'buy_upper': 0.009,
                'sell_lower': 0.012,
                'sell_upper': 0.013,
                'max_alpha': 1000,
            },
        }
        bu.sub = StubSub(MockSimSwapResult(dest_netuid=90))

        rotationTrade = bagbot.asyncio.run(bu.constructRotationTrade())
        self.assertIsNotNone(rotationTrade)
        self.assertEqual(rotationTrade['origin_netuid'], 91)
        self.assertEqual(rotationTrade['destination_netuid'], 90)
        self.assertIn('rotation_to_sn90', rotationTrade['rotation_reason'])

    def testRotationTradeBlockedWhenFeesKillNetEdge(self):
        args = {}
        bagbot.bagbot_settings.ENABLE_POSITION_ROTATION = True
        bagbot.bagbot_settings.ENABLE_ATOMIC_ROTATION = True
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = None
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = 0.05
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 1.0

        bu = bagbot.BittensorUtility(args)
        bu.balance = 1.0
        bu.stats = {
            90: {'price': 0.009, 'tao_in': 10000, 'alpha_in': 10000},
            91: {'price': 0.010, 'tao_in': 10000, 'alpha_in': 10000},
        }
        bu.current_stake_info = {
            bagbot.bagbot_settings.STAKE_ON_VALIDATOR: {
                91: MockStake(100),
            }
        }
        bu.subnet_grids = {
            90: {
                'buy_lower': 0.0105,
                'buy_upper': 0.011,
                'sell_lower': 0.013,
                'sell_upper': 0.014,
                'max_alpha': 1000,
            },
            91: {
                'buy_lower': 0.008,
                'buy_upper': 0.009,
                'sell_lower': 0.012,
                'sell_upper': 0.013,
                'max_alpha': 1000,
            },
        }
        bu.sub = StubSub(MockSimSwapResult(tao_fee=0.05, dest_netuid=90))

        rotationTrade = bagbot.asyncio.run(bu.constructRotationTrade())
        self.assertIsNone(rotationTrade)

    def testRotationTradeSkipsBlockedTarget(self):
        args = {}
        bagbot.bagbot_settings.ENABLE_POSITION_ROTATION = True
        bagbot.bagbot_settings.ENABLE_ATOMIC_ROTATION = True
        bagbot.bagbot_settings.MAX_PORTFOLIO_TAO = None
        bagbot.bagbot_settings.MAX_TAO_PER_BUY = 0.05
        bagbot.bagbot_settings.MAX_TAO_PER_SELL = 1.0

        bu = bagbot.BittensorUtility(args)
        bu.balance = 1.0
        bu.stats = {
            90: {'price': 0.009, 'tao_in': 10000, 'alpha_in': 10000},
            91: {'price': 0.010, 'tao_in': 10000, 'alpha_in': 10000},
        }
        bu.current_stake_info = {
            bagbot.bagbot_settings.STAKE_ON_VALIDATOR: {
                91: MockStake(100),
            }
        }
        bu.subnet_grids = {
            90: {
                'buy_lower': 0.0105,
                'buy_upper': 0.011,
                'sell_lower': 0.013,
                'sell_upper': 0.014,
                'max_alpha': 1000,
            },
            91: {
                'buy_lower': 0.008,
                'buy_upper': 0.009,
                'sell_lower': 0.012,
                'sell_upper': 0.013,
                'max_alpha': 1000,
            },
        }
        bu.sub = StubSub(MockSimSwapResult(dest_netuid=90))
        bu._block_subnet_execution(90, 'ZeroMaxStakeAmount', ttl_seconds=3600)

        rotationTrade = bagbot.asyncio.run(bu.constructRotationTrade())
        self.assertIsNone(rotationTrade)

    def testDoAvailableTradesSkipsBlockedSubnet(self):
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.balance = 1.0
        bu.wallet = object()
        bu.stats = {90: {'price': 0.01, 'tao_in': 10000}}
        bu.subnet_grids = {90: {'buy_upper': 0.02,
                                'sell_lower': 0.03,
                                'max_alpha': 3000,
                           }}
        bu.sub = CaptureAddStakeSub()
        bu._block_subnet_execution(90, 'ZeroMaxStakeAmount', ttl_seconds=3600)

        bagbot.asyncio.run(bu.do_available_trades(90))
        self.assertEqual(bu.sub.calls, [])

    def testRefreshSubnetGridHotReloadsUpdatedAllowlist(self):
        args = {}
        bu = bagbot.BittensorUtility(args)

        initial_settings = bagbot.SimpleNamespace(
            SUBNET_SETTINGS={
                11: {'buy_upper': 0.01, 'sell_lower': 0.02, 'max_alpha': 100},
            },
            BRAINS_DRY_RUN=True,
            BUY_ZONE_POWER=1.0,
            SELL_ZONE_POWER=1.0,
        )
        updated_settings = bagbot.SimpleNamespace(
            SUBNET_SETTINGS={
                11: {'buy_upper': 0.01, 'sell_lower': 0.02, 'max_alpha': 100},
                22: {'buy_upper': 0.02, 'sell_lower': 0.03, 'max_alpha': 100},
            },
            BRAINS_DRY_RUN=False,
            BUY_ZONE_POWER=1.0,
            SELL_ZONE_POWER=1.0,
        )

        class StubStrategyEngine:
            def __init__(self):
                self.calls = []

            def refresh_runtime_settings(self, settings):
                self.calls.append(sorted(settings.SUBNET_SETTINGS.keys()))

        stub_engine = StubStrategyEngine()

        original_settings = bagbot.bagbot_settings

        with patch.object(bagbot, 'bagbot_settings', original_settings), \
             patch.object(bagbot, '_strategy_engine', stub_engine), \
             patch.object(bagbot, 'load_safe_python_settings', side_effect=[initial_settings, updated_settings]), \
             patch.object(bagbot, '_settings_signature', side_effect=['sig-a', 'sig-a', 'sig-b']):
            bagbot.asyncio.run(bu.refresh_subnet_grid())
            self.assertEqual(sorted(bu.subnet_grids.keys()), [11])
            self.assertEqual(stub_engine.calls, [[11]])

            bagbot.asyncio.run(bu.refresh_subnet_grid())
            self.assertEqual(sorted(bu.subnet_grids.keys()), [11])
            self.assertEqual(stub_engine.calls, [[11]])

            bagbot.asyncio.run(bu.refresh_subnet_grid())
            self.assertEqual(sorted(bu.subnet_grids.keys()), [11, 22])
            self.assertEqual(stub_engine.calls, [[11], [11, 22]])

    def testExecuteBuyTradeUsesInclusionWhenMevProtectionEnabled(self):
        args = {}
        bagbot.bagbot_settings.ENABLE_MEV_PROTECTION = True
        bu = bagbot.BittensorUtility(args)
        bu.sub = CaptureAddStakeSub()
        bu.wallet = object()
        bu.stats = {90: {'price': 0.01}}

        buyTrade = {
            'hotkey': 'somehotkey',
            'netuid': 90,
            'tao_amount': bagbot.bt.utils.balance.tao(0.1),
            'max_slippage': 0.005,
        }

        bagbot.asyncio.run(bu.execute_buy_trade(buyTrade))
        self.assertTrue(bu.sub.calls)
        self.assertTrue(bu.sub.calls[0]['wait_for_inclusion'])
        self.assertTrue(bu.sub.calls[0]['wait_for_revealed_execution'])


    def testSellPowerCurveLinear(self):
        """Test that power curve = 1.0 gives linear behavior for sells"""
        args = {}
        bu = bagbot.BittensorUtility(args)
        bu.subnet_grids = {90:{'buy_upper':0.01,
                               'sell_lower':0.01,
                               'sell_upper':0.02,
                               'max_alpha':1000,
                               'sell_zone_power': 1.0,
                          }}
        # At 50% progress (500 alpha), should be at midpoint
        sell_threshold = bu.determine_sell_at_for_amount(bu.subnet_grids[90], 500)
        self.assertTrue(math.isclose(sell_threshold, 0.015))



class MockStake:
    def __init__(self, stake):
        self.stake = stake


class MockSimSwapResult:
    def __init__(self, tao_fee=0.0001, alpha_fee=0.0, alpha_amount=10.0, dest_netuid=0):
        self.tao_fee = bagbot.bt.utils.balance.tao(tao_fee)
        self.alpha_fee = bagbot.bt.utils.balance.tao(alpha_fee, dest_netuid)
        self.alpha_amount = bagbot.bt.utils.balance.tao(alpha_amount, dest_netuid)


class StubSub:
    def __init__(self, sim_result):
        self.sim_result = sim_result
        self.calls = []

    async def sim_swap(self, origin_netuid, destination_netuid, amount):
        self.calls.append((origin_netuid, destination_netuid, float(amount)))
        return self.sim_result


class CaptureAddStakeSub:
    def __init__(self):
        self.calls = []

    async def add_stake(self, **kwargs):
        self.calls.append(kwargs)
        return MockExtrinsicResponse(True)


class MockExtrinsicResponse:
    def __init__(self, success):
        self.success = success

if __name__ == '__main__':
    unittest.main()
