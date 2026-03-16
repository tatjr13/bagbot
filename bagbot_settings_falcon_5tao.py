STAKE_ON_VALIDATOR = "5FtBncJvGhxjBs4aFn2pid6aur9tBUuo9QR7sHe5DkoRizzo"

# Set this to the password you choose when restoring the Falcon coldkey.
# Prefer WALLET_PW_FILE on the live box so the password is not stored directly in this file.
WALLET_PW = "CHANGE_ME"
WALLET_PW_ENV = None
WALLET_PW_FILE = None
WALLET_NAME = "Falcon"
MAX_PORTFOLIO_TAO = None
MIN_TAO_RESERVE = 0.0
EXECUTION_FEE_BUFFER_TAO = 0.001
MAX_SUBNET_ALLOCATION_RATIO = 0.35
ENABLE_POSITION_ROTATION = True
ENABLE_ATOMIC_ROTATION = True
ENABLE_MEV_PROTECTION = True
ROTATION_REQUIRE_CONSTRAINTS = False
ROTATION_TARGET_DISCOUNT_PCT = 0.02
ROTATION_SOURCE_WEAKNESS_PCT = 0.02
ROTATION_MIN_NET_EDGE_PCT = 0.01
ROTATION_EXTRINSIC_FEE_BUFFER_TAO = 0.0002

# Use the full bankroll when the signal is strong enough; slippage rules still cap trade size.
MAX_TAO_PER_BUY = None
MAX_TAO_PER_SELL = None
MAX_SLIPPAGE_PERCENT_PER_BUY = 0.5

BUY_ZONE_POWER = 1.2
SELL_ZONE_POWER = 0.2

# Enable Brains so threshold-farm can take over once it has enough history.
BRAINS_ENABLED = True
BRAINS_DRY_RUN = False

SUBNET_SETTINGS = {
    22: {
        "buy_lower": 0.004,
        "buy_upper": 0.0055,
        "sell_lower": 0.006,
        "sell_upper": 0.01,
        "max_alpha": 2000,
    },
    15: {
        "buy_lower": 0.008,
        "buy_upper": 0.01,
        "sell_lower": 0.016,
        "sell_upper": 0.03,
        "max_alpha": 500,
    },
    107: {
        "buy_lower": 0.01,
        "buy_upper": 0.0165,
        "sell_lower": 0.02,
        "sell_upper": 0.03,
        "max_alpha": 1000,
    },
    100: {
        "buy_lower": 0.01,
        "buy_upper": 0.0155,
        "sell_lower": 0.018,
        "sell_upper": 0.02,
        "max_alpha": 1000,
    },
    11: {
        "buy_lower": 0.012,
        "buy_upper": 0.013,
        "sell_lower": 0.014,
        "sell_upper": 0.03,
        "max_alpha": 2000,
    },
    93: {
        "buy_zone_power": 1.2,
        "buy_lower": 0.01,
        "buy_upper": 0.014,
        "sell": 0.04,
        "max_alpha": 2000,
    },
}
