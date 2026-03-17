STAKE_ON_VALIDATOR = "5FtBncJvGhxjBs4aFn2pid6aur9tBUuo9QR7sHe5DkoRizzo"

# Set this to the password you choose when restoring the Falcon coldkey.
# Prefer WALLET_PW_FILE on the live box so the password is not stored directly in this file.
WALLET_PW = "CHANGE_ME"
WALLET_PW_ENV = None
WALLET_PW_FILE = None
WALLET_NAME = "Falcon"
MAX_PORTFOLIO_TAO = None
MIN_TAO_RESERVE = 0.0
EXECUTION_FEE_BUFFER_TAO = 0.005
MAX_SUBNET_ALLOCATION_RATIO = 0.35
IGNORE_NON_CONFIGURED_STAKE_BELOW_TAO = 0.25
ENABLE_POSITION_ROTATION = True
ENABLE_ATOMIC_ROTATION = True
ENABLE_MEV_PROTECTION = True
ROTATION_REQUIRE_CONSTRAINTS = False
ROTATION_TARGET_DISCOUNT_PCT = 0.02
ROTATION_SOURCE_WEAKNESS_PCT = 0.02
ROTATION_MIN_NET_EDGE_PCT = 0.01
ROTATION_EXTRINSIC_FEE_BUFFER_TAO = 0.002

# Use the full bankroll when the signal is strong enough; slippage rules still cap trade size.
MAX_TAO_PER_BUY = None
MAX_TAO_PER_SELL = None
MAX_SLIPPAGE_PERCENT_PER_BUY = 0.5

BUY_ZONE_POWER = 1.2
SELL_ZONE_POWER = 0.2

# Enable Brains so threshold-farm can take over once it has enough history.
BRAINS_ENABLED = True
BRAINS_DRY_RUN = False

# Keep the static grid empty so Brains can score the full observed subnet universe
# and build the live roster dynamically from market conditions.
SUBNET_SETTINGS = {}
