
STAKE_ON_VALIDATOR = "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u" # Tao.Bot (Opentensor Foundation by default) Replace with the hotkey of the validator you want to stake with (copy from https://taostats.io/validators )
# Note: The bot can only see and operate alpha staked on the hotkey defined above. Alpha staked on any other validator hotkey are invisible & unusable to the bot.

WALLET_PW = 'CHANGE_ME' #Replace with your wallet's password that you entered into btcli
WALLET_PW_ENV = None #Optional: environment variable name containing the wallet password
WALLET_PW_FILE = None #Optional: path to a file containing only the wallet password
WALLET_NAME = 'bagbot' #The name of the wallet created in btcli

# Note: LOWER THAN 0.01 MAY CAUSE THE BUYS TO FAIL WHILE STILL TAKING THE GAS FEE
MAX_TAO_PER_BUY = 0.02 #May increase as desired, I wouldnt reduce it.
MAX_TAO_PER_SELL = 0.02 #May increase as desired, I wouldnt reduce it
MAX_SLIPPAGE_PERCENT_PER_BUY = 0.2 #If over this slippage %, buy trades won't execute.
MAX_SUBNET_ALLOCATION_RATIO = None #Optional cap on single-subnet exposure as a fraction of total portfolio value

# buy_lower is the lowest price that the bot will allocate your max_alpha amount to.  Will only purchase this low if you hold near the max_alpha amount.
# buy_upper is the highest price that the bot will allocate your max_alpha amount to.  Will only purchase this high if you hold no alpha in the subnet yet.
# sell_lower is the lowest price that the bot will sell your alpha.  Will only sell this low if you hold near the max_alpha amount.
# sell_upper is the highest price that the bot will sell your alpha.  Will only sell this high if you hold near almost no alpha in the subnet.
# max_alpha is the maximum amount of alpha to buy in the subnet, the bot will not purchase more.
# DELETE THE EXAMPLE SUBNETS BELOW AND ADD SUBNETS AS DESIRED
SUBNET_SETTINGS = {
# Subnet 36 web agents settings:
36: {'buy_lower':  0.003522,
     'buy_upper':  0.004866,
     'sell_lower': 0.005,
     'sell_upper': 0.015,
     'max_alpha':  1500},
# Subnet 45 talisman settings:
45: {'buy_lower':  0.007,
     'buy_upper':  0.008,
     'sell_lower': 0.01,
     'sell_upper': 0.02,
     'max_alpha':  1000},
# Subnet 87 checker settings:
87: {'buy_lower':  0.0001,
     'buy_upper':  0.0001,
     'sell_lower': 0.01,
     'sell_upper': 0.02,
     'max_alpha':  7500},   
}
