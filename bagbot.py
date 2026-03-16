import asyncio
import logging
import argparse
import time
import os
import websockets
import traceback
import json
from typing import List, Dict, Tuple

import bittensor as bt
from bittensor.core.async_subtensor import get_async_subtensor
import async_substrate_interface

import printHelpers
from decimal import Decimal, getcontext
getcontext().prec = 14 #Precision for price stuff

from rich.console import Console
console = Console()

import ast
from pathlib import Path
import sys
from types import SimpleNamespace

class InvalidSettings(Exception): pass
class InternetIssueException(Exception): pass

# Configure logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler('staking.log')#,
#        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def print_link(url: str, text: str | None = None) -> None:
    if text is None:
        text = url
    # \x1b = ESC
    print(f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\")

def load_safe_python_settings():
    settings = {}

    # Determine where to look for the files (works in dev and when frozen with PyInstaller/Nuitka)
    exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(".")

    default_path = exe_dir / "bagbot_settings.py"
    overrides_path = exe_dir / "bagbot_settings_overrides.py"

    for path in [default_path, overrides_path]:
        is_default = (path == default_path)

        if not path.exists():
            if is_default:
                raise FileNotFoundError(f"CRITICAL: {path} is missing! Cannot continue.")
            else:
                print(f"Info: Optional overrides file not found (this is fine): {path}")
                continue  # overrides are optional

        source = path.read_text(encoding="utf-8")

        try:
            tree = ast.parse(source)  # mode='exec' by default → accepts real Python files
        except SyntaxError as e:
            raise SyntaxError(f"Invalid Python syntax in {path.name}: {e}") from e

        for node in tree.body:
            # Simple assignment: VAR = value
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id.isidentifier():
                    name = target.id
                    try:
                        value = ast.literal_eval(node.value)
                        settings[name] = value
                    except (ValueError, SyntaxError):
                        print(f"Warning: Skipping unsafe or invalid value for '{name}' in {path.name}")

            # Allow top-level comments / docstrings / pass etc. → just ignore them
            # (optional) you can also support AnnAssign (typed vars) if you want:
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if node.value:  # only if there's actually a value
                    try:
                        value = ast.literal_eval(node.value)
                        settings[name] = value
                    except (ValueError, SyntaxError):
                        print(f"Warning: Skipping unsafe annotated assignment '{name}' in {path.name}")

    return SimpleNamespace(**settings)


def _settings_signature():
    """Return a cheap signature for the settings files so we can hot-reload safely."""
    exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(".")
    signature = []
    for filename in ("bagbot_settings.py", "bagbot_settings_overrides.py"):
        path = exe_dir / filename
        try:
            signature.append((filename, path.stat().st_mtime_ns))
        except FileNotFoundError:
            signature.append((filename, None))
    return tuple(signature)


def resolve_wallet_password(settings):
    """Resolve the wallet password from env/file settings before unlocking keys."""
    env_name = getattr(settings, 'WALLET_PW_ENV', None)
    if env_name:
        env_value = os.environ.get(env_name, '').strip()
        if not env_value:
            raise InvalidSettings(f'Environment variable {env_name} is required but not set')
        return env_value

    pw_file = getattr(settings, 'WALLET_PW_FILE', None)
    if pw_file:
        try:
            file_value = Path(pw_file).read_text(encoding='utf-8').strip()
        except FileNotFoundError as exc:
            raise InvalidSettings(f'Wallet password file is missing: {pw_file}') from exc
        if not file_value:
            raise InvalidSettings(f'Wallet password file is empty: {pw_file}')
        return file_value

    wallet_pw = getattr(settings, 'WALLET_PW', None)
    if wallet_pw is None or str(wallet_pw).strip() == '':
        raise InvalidSettings('WALLET_PW is missing; set WALLET_PW, WALLET_PW_ENV, or WALLET_PW_FILE')
    return wallet_pw

bagbot_settings = load_safe_python_settings()

# Brains strategy engine (optional, enabled via BRAINS_ENABLED in settings)
_strategy_engine = None
if getattr(bagbot_settings, 'BRAINS_ENABLED', False):
    try:
        from Brains.integration import StrategyEngine
        _strategy_engine = StrategyEngine(bagbot_settings)
        logger.info('Brains strategy engine initialized')
    except Exception as e:
        logger.error(f'Failed to initialize Brains: {e}')
        _strategy_engine = None

def parseArgs():
    parser = argparse.ArgumentParser(description="A basic bittensor alpha bot")
    parser.add_argument( "--nocheck", action="store_true", help="Don't check settings before starting the bot (boolean flag)"
    )

    # Parse arguments
    args = parser.parse_args()
    return args


rao_to_tao = lambda rao : int(rao)/1000000000.0


async def my_async_subtensor(*args, **kwargs):
    attempts = 0
    while attempts < 20:
        try:
            return await get_async_subtensor(*args, **kwargs)
        except (websockets.exceptions.InvalidStatus, AttributeError, asyncio.exceptions.TimeoutError) as e:
            logger.error(f'Invalid status err {str(e)}, retrying')
            attempts += 1
            if attempts >= 19:
                raise
            await asyncio.sleep(attempts*2)

class BittensorUtility():


    def __init__(self, args):
        self.args = args
        self.current_stake_info = {}
        self.tick = 0
        self.gridLoaded = False
        self.settings_signature = None
        self.static_subnet_grids = {}
        self.subnet_grids = {}
        self.execution_blocked_subnets = {}


    def get_subnet_setting(self, subnet_netuid, setting_name, default_value):
        """
        Get a setting for a subnet, allowing per-subnet overrides of global settings.

        Args:
            subnet_netuid: The subnet ID
            setting_name: The name of the setting to get
            default_value: The default (global) value if no override exists

        Returns:
            The subnet-specific override if it exists, otherwise the default value
        """
        if subnet_netuid in self.subnet_grids:
            return self.subnet_grids[subnet_netuid].get(setting_name, default_value)
        return default_value


    async def discover_all_validators_with_stake(self):
        """
        Query the blockchain to find ALL validators where this coldkey has stake.

        Returns:
            List of validator hotkeys that have stake from this coldkey, or None if discovery fails
        """
        try:
            # Try to get comprehensive stake info
            stake_info_list = await asyncio.wait_for(
                self.sub.get_stake_info_for_coldkey(
                    coldkey_ss58=self.wallet.coldkey.ss58_address
                ),
                timeout=30.0
            )

            validators = set()

            # Handle different return types
            if stake_info_list is None:
                logger.warning('get_stake_info_for_coldkey returned None')
                return None

            # If it's a list, iterate and extract hotkeys
            if isinstance(stake_info_list, list):
                for stake_info in stake_info_list:
                    hotkey = None
                    # Try different attribute names
                    if hasattr(stake_info, 'hotkey_ss58'):
                        hotkey = stake_info.hotkey_ss58
                    elif hasattr(stake_info, 'hotkey'):
                        hotkey = stake_info.hotkey

                    if hotkey:
                        validators.add(hotkey)
                        logger.debug(f'Found stake on validator {hotkey}')
            else:
                logger.warning(f'Unexpected return type from get_stake_info_for_coldkey: {type(stake_info_list)}')
                return None

            if len(validators) > 0:
                logger.info(f'Discovered {len(validators)} validators with stake from blockchain: {list(validators)}')
                return list(validators)
            else:
                logger.warning('No validators found with stake, falling back to configured validators')
                return None

        except (AttributeError, TypeError) as e:
            logger.warning(f'Error parsing stake info structure: {e}')
            logger.warning('Falling back to configured validators only')
            return None
        except asyncio.TimeoutError:
            logger.warning('Timeout discovering validators from blockchain')
            logger.warning('Falling back to configured validators only')
            return None
        except Exception as e:
            logger.warning(f'Could not discover validators from blockchain: {e}')
            logger.warning(traceback.format_exc())
            logger.warning('Falling back to configured validators only')
            return None

    def get_all_validators(self):
        """
        Collect all unique validator hotkeys from global setting and per-subnet overrides.

        Returns:
            List of unique validator hotkeys to query for stake info
        """
        validators = {bagbot_settings.STAKE_ON_VALIDATOR}
        grid_source = self.static_subnet_grids or self.subnet_grids

        # Check each subnet for validator overrides
        for subnet_config in grid_source.values():
            if 'stake_on_validator' in subnet_config:
                validators.add(subnet_config['stake_on_validator'])

        return list(validators)


    async def setupWallet(self):
        wallet_pw = resolve_wallet_password(bagbot_settings)

        self.wallet = bt.Wallet(name=bagbot_settings.WALLET_NAME)
        self.wallet.create_if_non_existent()
        self.wallet.coldkey_file.save_password_to_env(wallet_pw)
        self.wallet.unlock_coldkey()


    async def setupSubtensor(self):
        while True:
            try:
                self.sub = await get_async_subtensor("finney")

                break
            except (asyncio.exceptions.TimeoutError, ConnectionResetError) as e:
                logger.error(e)
                logger.error(f'{str(e)}having trouble starting up... try again')
                await asyncio.sleep(3)


    async def setup(self):
        await self.setupWallet()
        await self.setupSubtensor()
        logger.info('Started')


    async def refresh_subnet_grid(self):
        global bagbot_settings

        current_signature = _settings_signature()
        if self.gridLoaded and current_signature == self.settings_signature:
            return

        bagbot_settings = load_safe_python_settings()
        self.settings_signature = current_signature
        self.static_subnet_grids = {
            netuid: dict(config)
            for netuid, config in getattr(bagbot_settings, 'SUBNET_SETTINGS', {}).items()
        }
        self.subnet_grids = dict(self.static_subnet_grids)
        self.validateGrid()
        self.gridLoaded = True

        if _strategy_engine is not None:
            try:
                _strategy_engine.refresh_runtime_settings(bagbot_settings)
            except Exception as e:
                logger.error(f'Brains settings refresh error: {e}')

        logger.info(f'Loaded subnet settings for netuids: {sorted(self.static_subnet_grids.keys())}')

    def validateGrid(self):
        for subnet_id in self.subnet_grids:
            if (self.subnet_grids[subnet_id].get('sell_lower') or self.subnet_grids[subnet_id].get('sell_upper')) and self.subnet_grids[subnet_id].get('sell'):
                raise InvalidSettings(f'Do not mix and match [sell_lower + sell_upper] with [sell], pick one or the other.  Subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS: {self.subnet_grids[subnet_id]}')
            if (self.subnet_grids[subnet_id].get('buy_lower') or self.subnet_grids[subnet_id].get('buy_upper')) and self.subnet_grids[subnet_id].get('buy'):
                raise InvalidSettings(f'Do not mix and match [buy_lower + buy_upper] with [buy], pick one or the other.  Subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if not self.subnet_grids[subnet_id].get('sell_lower'):
                if self.subnet_grids[subnet_id].get('sell'):
                    self.subnet_grids[subnet_id]['sell_lower'] = self.subnet_grids[subnet_id]['sell']
                else:
                    raise InvalidSettings(f'"sell_lower" missing for subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if self.subnet_grids[subnet_id].get('buy_upper') is None:
                if self.subnet_grids[subnet_id].get('buy') is not None:
                    self.subnet_grids[subnet_id]['buy_upper'] = self.subnet_grids[subnet_id]['buy']
                else:
                    raise InvalidSettings(f'"buy_upper" missing for subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if self.subnet_grids[subnet_id].get('sell_upper') is not None and self.subnet_grids[subnet_id].get('sell_lower') is not None and \
               self.subnet_grids[subnet_id].get('sell_upper') < self.subnet_grids[subnet_id].get('sell_lower'):
                raise InvalidSettings(f'"sell_upper" is lower than "sell_lower" for subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if not self.subnet_grids[subnet_id].get('max_alpha'):
                raise InvalidSettings(f'"max_alpha" missing for subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if self.subnet_grids[subnet_id]['buy_upper'] > self.subnet_grids[subnet_id]['sell_lower']:
                raise InvalidSettings(f'"buy_upper" is higher than "sell_lower" for subnet {subnet_id} in bagbot_settings.SUBNET_SETTINGS')
            if not isinstance(subnet_id, int):
                raise InvalidSettings(f'subnet {subnet_id} must be an integer in bagbot_settings.SUBNET_SETTINGS.  Strings or other objects are not allowed')
            if subnet_id == 0:
                raise InvalidSettings(f'No support for {subnet_id} in bagbot_settings.SUBNET_SETTINGS.')

            # Validate power curve settings if present
            buy_zone_power = self.subnet_grids[subnet_id].get('buy_zone_power', bagbot_settings.BUY_ZONE_POWER)
            if buy_zone_power <= 0:
                raise InvalidSettings(f'"buy_zone_power" must be positive for subnet {subnet_id} (got {buy_zone_power})')

            sell_zone_power = self.subnet_grids[subnet_id].get('sell_zone_power', bagbot_settings.SELL_ZONE_POWER)
            if sell_zone_power <= 0:
                raise InvalidSettings(f'"sell_zone_power" must be positive for subnet {subnet_id} (got {sell_zone_power})')




    def sendNotification(self, msg):
        logger.info(msg)
        if _strategy_engine and _strategy_engine.telegram:
            _strategy_engine.telegram.send_async(msg)


    async def get_subnet_stats(self) -> Tuple[Dict[int, Dict], Dict[int, int]]:
        all_subnets = None
        attempts = 0
        while all_subnets is None:
            try:
                all_subnets = await self.sub.all_subnets()
            except (AttributeError, websockets.exceptions.InvalidStatus):
                if attempts > 5:
                    self.sendNotification(errMsg)
                    logger.error(traceback.format_exc())
                errMsg = 'Fetching subnets data from substrate had a problem... retrying'
                logger.error(errMsg)

                await asyncio.sleep(3)

                try:
                    await self.sub.close()
                except asyncio.exceptions.TimeoutError:
                    logger.error('Closing subtensor timeout')
                self.sub = await my_async_subtensor("finney")
                attempts += 1

        stats = {}
        for subnet in all_subnets:
            netuid = subnet.netuid

            price = float(subnet.price)
            if price <= 0:
                continue
            name = str(subnet.subnet_name) if hasattr(subnet, "subnet_name") else ""
            stats[netuid] = {
                "name": name,
                "price": price,
                "tao_in": subnet.tao_in.tao,
                "alpha_in": subnet.alpha_in.tao,
            }
        return stats



    async def get_stake_for_hotkey(self, hotkey):
        attempts = 10
        for i in range(attempts):
            try:
                retval = await asyncio.wait_for(
                            self.sub.get_stake_for_coldkey_and_hotkey(
                                hotkey_ss58=hotkey,
                                coldkey_ss58=self.wallet.coldkey.ss58_address
                            ),
                            timeout=20.0
                        )
                return retval
            except asyncio.exceptions.TimeoutError:
                logger.info('Timeout fetching hotkey stake')
                await asyncio.sleep(10)
        raise InternetIssueException("Too many attempts to refresh stats")


    async def refresh_stats(self, hotkeys):

        try:
            logger.info('Fetching subnet stats')
            self.stats = await asyncio.wait_for(self.get_subnet_stats(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error('Timeout fetching subnet stats after 30s')
            raise
        except Exception as e:
            logger.error(traceback.format_exc())
            raise

        for hotkey in hotkeys:
            logger.info(f'Fetching stake info for {hotkey}')
            self.current_stake_info[hotkey] = await self.get_stake_for_hotkey(hotkey)

        logger.info('Fetching wallet balance')
        self.balance = float(await asyncio.wait_for(
            self.sub.get_balance(address=self.wallet.coldkey.ss58_address),
            timeout=20.0
        ))

        sumStakedValue = 0
        tickLog = []

        for hotkey in hotkeys:
            for subnet_netuid in self.current_stake_info[hotkey]:
                if subnet_netuid in self.current_stake_info[hotkey] and self.current_stake_info[hotkey][subnet_netuid].stake.rao == 0: continue
                sumStakedValue += rao_to_tao(self.current_stake_info[hotkey][subnet_netuid].stake.rao) * self.stats[subnet_netuid]['price']
                tickLog.append( f'sn{subnet_netuid}: {rao_to_tao(self.current_stake_info[hotkey][subnet_netuid].stake.rao):.1f}' )

        logger.info('{' + f'wallet_value:"{sumStakedValue:.2f} + {self.balance:.2f}", ' + ', '.join(tickLog) + '}')



    async def run(self):
        await self.setup()
        await self.refresh_subnet_grid()  # Load subnet settings before first tick

        while True:
            self.tick += 1
            start = time.time()
            try:
                logger.info(f'Starting tick {self.tick}')
                await self.refresh_subnet_grid()
                # Try to discover ALL validators with stake from blockchain
                discovered_validators = await self.discover_all_validators_with_stake()

                if discovered_validators:
                    # Use discovered validators for comprehensive stake info
                    all_validators = discovered_validators
                    logger.info(f'Using discovered validators for stake queries: {all_validators}')
                else:
                    # Fall back to configured validators only
                    all_validators = self.get_all_validators()
                    logger.info(f'Using configured validators for stake queries: {all_validators}')

                await self.refresh_stats(all_validators)

                # Brains strategy tick: record bars, compute patches
                if _strategy_engine is not None:
                    try:
                        _strategy_engine.on_tick(
                            self.stats, self.static_subnet_grids,
                            self.current_stake_info, self.balance
                        )
                        self.subnet_grids = _strategy_engine.get_runtime_subnet_grids(self.static_subnet_grids)
                    except Exception as e:
                        logger.error(f'Brains on_tick error: {e}')
                        self.subnet_grids = dict(self.static_subnet_grids)
                else:
                    self.subnet_grids = dict(self.static_subnet_grids)

                logger.info(f'Tick {self.tick}: Printing table')
                printHelpers.print_table_rich(self, console, self.current_stake_info, list(self.subnet_grids.keys()), self.stats, self.balance, self.subnet_grids)
                allSubnetParams = '&var-target_subnets='.join([str(k) for k in self.subnet_grids])
                if self.tick == 1 and not self.args.nocheck:
                    loop = asyncio.get_event_loop()
                    print(f"Link to portfolio on taoflute: https://taoflute.com/d/5c216965-b99b-4d82-8b31-931bb3d71567/subnets-overview?orgId=1&var-target_subnets={allSubnetParams}\n")
                    user_input = await loop.run_in_executor(None, input, "Should the bot proceed? (Y/N): ")
                    if user_input.lower() != 'y':
                        print('Exiting...')
                        return

                print_link(f"https://taoflute.com/d/5c216965-b99b-4d82-8b31-931bb3d71567/subnets-overview?orgId=1&var-target_subnets={allSubnetParams}", 'Taoflute Portfolio link')
                logger.info(f'Tick {self.tick}: Checking trades')
                rotationTrade = await self.constructRotationTrade()
                if rotationTrade:
                    await self.execute_rotation_trade(rotationTrade)
                else:
                    for subnet_netuid in self.subnet_grids:
                        await self.do_available_trades(subnet_netuid)

                logging.info(f'Finished tick {self.tick} in {time.time() - start:.2f} seconds')
                #return
                try:
                    logger.info(f'Tick {self.tick}: Waiting for next block')
                    await asyncio.wait_for(self.sub.wait_for_block(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning(f'Tick {self.tick}: wait_for_block timed out after 30s, reconnecting...')
                    await self.sub.close()
                    self.sub = await my_async_subtensor("finney")
                except (OSError, KeyError):
                    await asyncio.sleep(12) #if error with waiting for block, just wait approx 1 block and try again

            except InternetIssueException:
                logger.warning(f'Some internet issue must be happening, pausing for 1 minute...')
                await asyncio.sleep(60)
            except asyncio.exceptions.CancelledError:
                logger.info(f'Asyncio exception, retrying...')
                await asyncio.sleep(3)
            except async_substrate_interface.errors.SubstrateRequestException:
                logger.info(f'substrate request exception, retrying...')
                await asyncio.sleep(3)
            except ConnectionResetError:
                logger.info(f'connection reset, retrying...')
                await asyncio.sleep(3)
            except (websockets.exceptions.InvalidStatus, async_substrate_interface.errors.SubstrateRequestException, websockets.exceptions.ConnectionClosedError) as e:
                logger.info(f'potential server error: {e}, reconnecting...')
                try:
                    await self.sub.close()
                except:
                    pass
                self.sub = await my_async_subtensor("finney")
            except asyncio.exceptions.TimeoutError:
                logger.warning(f'Timeout error in tick {self.tick}, reconnecting subtensor...')
                try:
                    await self.sub.close()
                except:
                    pass
                self.sub = await my_async_subtensor("finney")
                await asyncio.sleep(3)
            finally:
                try:
                    await self.sub.close()
                except asyncio.exceptions.TimeoutError:
                    logger.error('Closing subtensor timeout')


    def determine_buy_at_for_amount(self, subnet_settings, alpha_amount):
        if 'buy_upper' not in subnet_settings:
            return None
        buy_upper = subnet_settings['buy_upper']
        if 'buy_lower' not in subnet_settings or alpha_amount == 0:
            return buy_upper
        buy_lower = subnet_settings['buy_lower']
        max_alpha = subnet_settings['max_alpha']

        # Get power curve setting (default to global setting)
        buy_zone_power = subnet_settings.get('buy_zone_power', bagbot_settings.BUY_ZONE_POWER)

        # Calculate position in the range (0 to 1)
        progress = min(alpha_amount / max_alpha, 1.0)

        # Apply power curve
        curve_value = progress ** buy_zone_power

        # Interpolate between buy_upper and buy_lower using the curve
        buy_at = buy_upper - (buy_upper - buy_lower) * curve_value

        return buy_at

    def determine_sell_at_for_amount(self, subnet_settings, alpha_amount):
        if 'sell_lower' not in subnet_settings:
            return None
        sell_lower = subnet_settings['sell_lower']
        if 'sell_upper' not in subnet_settings or alpha_amount == 0:
            return sell_lower
        sell_upper = subnet_settings['sell_upper']
        max_alpha = subnet_settings['max_alpha']

        # Get power curve setting (default to global setting)
        sell_zone_power = subnet_settings.get('sell_zone_power', bagbot_settings.SELL_ZONE_POWER)

        # Calculate position in the range (0 to 1)
        progress = min(alpha_amount / max_alpha, 1.0)

        # Apply power curve
        curve_value = progress ** sell_zone_power

        # Interpolate between sell_upper and sell_lower using the curve
        sell_at = sell_upper - (sell_upper - sell_lower) * curve_value

        return max(sell_lower, sell_at)



    def get_subnet_buy_threshold(self, subnet_netuid):
        current_stake_amt = self.my_current_stake(subnet_netuid)
        if self.subnet_grids.get(subnet_netuid,{}).get('buy_upper') is not None:
            return self.determine_buy_at_for_amount(self.subnet_grids.get(subnet_netuid,{}), current_stake_amt)
        return None


    def get_subnet_sell_threshold(self, subnet_netuid):
        current_stake_amt = self.my_current_stake(subnet_netuid)
        if self.subnet_grids.get(subnet_netuid,{}).get('sell_lower') is not None:
            return self.determine_sell_at_for_amount(self.subnet_grids.get(subnet_netuid,{}), current_stake_amt)
        """
        baseline = self.subnet_grids.get(subnet_netuid,{}).get('sell_lower')
        return baseline
        """


    def my_current_stake(self, subnet_netuid):
        total_stake = 0
        for hotkey in self.current_stake_info:
            stake_obj = self.current_stake_info[hotkey].get(subnet_netuid)
            total_stake += (float(stake_obj.stake) if stake_obj is not None else 0.0)
        return total_stake


    def my_total_staked_value(self):
        total_value = 0.0
        for hotkey in self.current_stake_info:
            for subnet_netuid, stake_obj in self.current_stake_info[hotkey].items():
                if stake_obj is None or subnet_netuid not in self.stats:
                    continue
                total_value += float(stake_obj.stake) * self.stats[subnet_netuid]['price']
        return total_value


    def my_subnet_staked_value(self, subnet_netuid):
        if subnet_netuid not in self.stats:
            return 0.0
        return self.my_current_stake(subnet_netuid) * self.stats[subnet_netuid]['price']


    def _stake_for_hotkey_subnet(self, hotkey, subnet_netuid):
        stake_obj = self.current_stake_info.get(hotkey, {}).get(subnet_netuid)
        return float(stake_obj.stake) if stake_obj is not None else 0.0


    def determineHotKey(self, unstake_amt, subnet_netuid):
        # Prioritize the configured validator for this subnet
        configured_validator = self.get_subnet_setting(subnet_netuid, 'stake_on_validator', bagbot_settings.STAKE_ON_VALIDATOR)

        # First check if configured validator has stake
        if configured_validator in self.current_stake_info:
            stake_obj = self.current_stake_info[configured_validator].get(subnet_netuid)
            stake = (float(stake_obj.stake) if stake_obj is not None else 0.0)
            if stake > 0:
                return configured_validator

        # If configured validator has no stake, don't sell from other validators
        # (This prevents accidentally selling stake from validators the user doesn't want to trade on)
        logger.warning(f'Configured validator {configured_validator} has no stake for subnet {subnet_netuid}, cannot sell')
        return None


    def determineSlippage(self, token_amount, token_in_pool):
        slippage = (Decimal(token_amount)/(Decimal(token_in_pool)+Decimal(token_amount))) * Decimal('100.0')
        return slippage


    def determineTokenBuyAmount(self, max_token_per_buy, token_in_pool, max_slippage_percent):
        max_amount_with_max_slippage = (token_in_pool*(max_slippage_percent/100.0)) / (1 - (max_slippage_percent/100.0))
        return min(max_token_per_buy, max_amount_with_max_slippage)


    def _apply_live_patch(self, subnet_netuid):
        if _strategy_engine is None:
            return None
        patch = _strategy_engine.get_patch(subnet_netuid)
        if patch is None or patch.dry_run:
            return None

        self.subnet_grids[subnet_netuid]['buy_lower'] = patch.buy_lower
        self.subnet_grids[subnet_netuid]['buy_upper'] = patch.buy_upper
        self.subnet_grids[subnet_netuid]['sell_lower'] = patch.sell_lower
        self.subnet_grids[subnet_netuid]['sell_upper'] = patch.sell_upper
        return patch


    def _available_remaining_budget(self):
        portfolio_cap = getattr(bagbot_settings, 'MAX_PORTFOLIO_TAO', None)
        if portfolio_cap is None:
            return None
        return max(0.0, float(portfolio_cap) - self.my_total_staked_value())


    def _execution_fee_buffer_tao(self):
        configured = getattr(bagbot_settings, 'EXECUTION_FEE_BUFFER_TAO', None)
        if configured is not None:
            return max(0.0, float(configured))
        return 0.001 if self._mev_enabled() else 0.0002


    def _available_spendable_balance(self):
        tao_reserve = float(getattr(bagbot_settings, 'MIN_TAO_RESERVE', 0.0) or 0.0)
        execution_fee_buffer = self._execution_fee_buffer_tao()
        return max(0.0, float(self.balance) - tao_reserve - execution_fee_buffer)


    def _max_additional_subnet_allocation(self, subnet_netuid):
        allocation_ratio = getattr(bagbot_settings, 'MAX_SUBNET_ALLOCATION_RATIO', None)
        if allocation_ratio is None:
            return None

        total_portfolio_value = self.my_total_staked_value() + float(self.balance)
        if total_portfolio_value <= 0:
            return None

        max_subnet_value = total_portfolio_value * float(allocation_ratio)
        current_subnet_value = self.my_subnet_staked_value(subnet_netuid)
        return max(0.0, max_subnet_value - current_subnet_value)


    def _rotation_constraints_active(self):
        remaining_budget = self._available_remaining_budget()
        spendable_balance = self._available_spendable_balance()
        return (
            (remaining_budget is not None and remaining_budget < 0.01) or
            spendable_balance < 0.01
        )


    def _mev_enabled(self):
        return bool(getattr(bagbot_settings, 'ENABLE_MEV_PROTECTION', False))


    def _block_subnet_execution(self, subnet_netuid, reason, ttl_seconds=3600):
        self.execution_blocked_subnets[subnet_netuid] = {
            'reason': reason,
            'blocked_until': time.time() + ttl_seconds,
        }
        logger.warning(f'Blocking sn{subnet_netuid} from new allocations for {ttl_seconds}s: {reason}')


    def _subnet_execution_block_reason(self, subnet_netuid):
        now = time.time()
        blocked = self.execution_blocked_subnets.get(subnet_netuid)
        if not blocked:
            return None
        if now >= blocked['blocked_until']:
            self.execution_blocked_subnets.pop(subnet_netuid, None)
            return None
        return blocked['reason']


    def _extract_execution_block_reason(self, result_text):
        if 'ZeroMaxStakeAmount' in result_text:
            return 'ZeroMaxStakeAmount'
        if 'HotKeyNotRegisteredInSubNet' in result_text:
            return 'HotKeyNotRegisteredInSubNet'
        return None


    def _mev_rotation_outcome_failed(self, result_text):
        lowered = result_text.lower()
        return (
            'failed to find outcome of the shield extrinsic' in lowered
            or "protected extrinsic wasn't decrypted" in lowered
        )


    def _rotation_extrinsic_fee_buffer_tao(self):
        return float(getattr(bagbot_settings, 'ROTATION_EXTRINSIC_FEE_BUFFER_TAO', 0.0002) or 0.0002)


    def _limit_or_unbounded(self, raw_limit):
        if raw_limit is None:
            return float('inf')
        limit = float(raw_limit)
        if limit <= 0:
            return float('inf')
        return limit


    def _slippage_exceeds_limit(self, slippage, max_slippage, epsilon=Decimal('1e-9')):
        return Decimal(str(slippage)) - Decimal(str(max_slippage)) > epsilon


    def _build_sell_trade(self, subnet_netuid, max_tao_per_sell, sell_threshold, hotkey, force_reason=None, preview_only=False):
        if subnet_netuid not in self.stats or self.my_current_stake(subnet_netuid) <= 0:
            return None

        max_slippage = self.get_subnet_setting(subnet_netuid, 'max_slippage_percent_per_buy', bagbot_settings.MAX_SLIPPAGE_PERCENT_PER_BUY)
        unstake_target = max_tao_per_sell / self.stats[subnet_netuid]['price']
        available_hotkey_alpha = self._stake_for_hotkey_subnet(hotkey, subnet_netuid)
        if available_hotkey_alpha <= 0:
            return None
        max_alpha_possible_to_sell = min(available_hotkey_alpha, unstake_target)
        alpha_to_sell = self.determineTokenBuyAmount(max_alpha_possible_to_sell, self.stats[subnet_netuid]['alpha_in'], max_slippage)
        alpha_amount = bt.utils.balance.tao(alpha_to_sell, subnet_netuid)
        approx_tao = float(Decimal(self.stats[subnet_netuid]['price']) * Decimal(alpha_to_sell))

        if approx_tao > max_tao_per_sell:
            raise Exception(f'Stopping before selling too much. approx_tao: {approx_tao}, max tao per sell: {max_tao_per_sell}, price x alpha: {self.stats[subnet_netuid]["price"]} x {alpha_to_sell} \nTO FIX: increase the max_tao_per_sell variable or increase max_slippage_percent_per_buy')

        slippage = self.determineSlippage(alpha_to_sell, self.stats[subnet_netuid]['alpha_in'])
        if self._slippage_exceeds_limit(slippage, max_slippage):
            raise Exception(f'Stopping before selling too much, slippage: {Decimal(slippage)}, max slippage per buy/sell: {Decimal(max_slippage)}  \nTO FIX: increase the max_tao_per_sell variable or increase max_slippage_percent_per_buy')

        if not preview_only:
            if force_reason:
                logger.info(
                    f"About to rotate out of sn{subnet_netuid}: unstake {alpha_to_sell} alpha "
                    f"(~{approx_tao} TAO) on hotkey {hotkey} with expected slippage of {slippage:.4f}% | {force_reason}"
                )
            else:
                logger.info(f"About to unstake {alpha_to_sell} alpha (~{approx_tao} TAO) in sn{subnet_netuid} on hotkey {hotkey} with expected slippage of {slippage:.4f}%")

        trade = {
            'hotkey': hotkey,
            'netuid': subnet_netuid,
            'alpha_amount': alpha_amount,
            'max_slippage': max_slippage / 100.0,
            'sell_threshold': sell_threshold,
            'calculated_slippage': slippage,
            'approx_tao': approx_tao,
        }
        if force_reason:
            trade['rotation_reason'] = force_reason
        return trade



    def constructBuy(self, subnet_netuid, ignore_balance_limits=False, preview_only=False):
        patch = self._apply_live_patch(subnet_netuid)
        if patch is not None and not patch.enable_buys:
            return None

        current_stake_amt = self.my_current_stake(subnet_netuid)
        buy_threshold = self.get_subnet_buy_threshold(subnet_netuid)

        max_tao_per_buy = self._limit_or_unbounded(
            self.get_subnet_setting(subnet_netuid, 'max_tao_per_buy', bagbot_settings.MAX_TAO_PER_BUY)
        )
        max_slippage = self.get_subnet_setting(subnet_netuid, 'max_slippage_percent_per_buy', bagbot_settings.MAX_SLIPPAGE_PERCENT_PER_BUY)
        hotkey = self.get_subnet_setting(subnet_netuid, 'stake_on_validator', bagbot_settings.STAKE_ON_VALIDATOR)

        portfolio_cap = getattr(bagbot_settings, 'MAX_PORTFOLIO_TAO', None)
        if not ignore_balance_limits and portfolio_cap is not None:
            remaining_budget = self._available_remaining_budget()
            max_tao_per_buy = min(max_tao_per_buy, remaining_budget)
            if max_tao_per_buy < 0.01:
                if not preview_only:
                    logger.info(
                        f'Portfolio cap reached or remaining budget too small to trade: '
                        f'cap={float(portfolio_cap):.4f}, remaining={remaining_budget:.4f}'
                    )
                return None

        tao_reserve = float(getattr(bagbot_settings, 'MIN_TAO_RESERVE', 0.0) or 0.0)
        execution_fee_buffer = self._execution_fee_buffer_tao()
        if not ignore_balance_limits:
            spendable_balance = self._available_spendable_balance()
            max_tao_per_buy = min(max_tao_per_buy, spendable_balance)
            if max_tao_per_buy < 0.01:
                if not preview_only:
                    logger.info(
                        f'Fee buffer reached or remaining spendable balance too small to trade: '
                        f'reserve={tao_reserve:.4f}, fee_buffer={execution_fee_buffer:.4f}, balance={float(self.balance):.4f}, '
                        f'spendable={spendable_balance:.4f}'
                    )
                return None

        if patch is not None:
            buy_threshold = self.get_subnet_buy_threshold(subnet_netuid)
            patch_limit = getattr(patch, 'max_tao_per_buy', None)
            if patch_limit is not None:
                max_tao_per_buy = min(max_tao_per_buy, float(patch_limit))

        if not ignore_balance_limits:
            max_tao_per_buy = min(max_tao_per_buy, float(self.balance))
            if max_tao_per_buy < 0.01:
                if not preview_only:
                    logger.info(f'Not enough balance to stake: {self.balance:.2f}')
                return None

        allocation_headroom = self._max_additional_subnet_allocation(subnet_netuid)
        if not ignore_balance_limits and allocation_headroom is not None:
            max_tao_per_buy = min(max_tao_per_buy, allocation_headroom)
            if max_tao_per_buy < 0.01:
                if not preview_only:
                    logger.info(
                        f'Subnet allocation cap reached for sn{subnet_netuid}: '
                        f'ratio={float(getattr(bagbot_settings, "MAX_SUBNET_ALLOCATION_RATIO", 0.0)):.2f}, '
                        f'current_value={self.my_subnet_staked_value(subnet_netuid):.4f}, '
                        f'portfolio_value={(self.my_total_staked_value() + float(self.balance)):.4f}'
                    )
                return None

        if subnet_netuid in self.stats and self.stats[subnet_netuid]['price'] < buy_threshold and current_stake_amt < self.subnet_grids[subnet_netuid]['max_alpha']:
            if not preview_only:
                logger.info(f'''Want to buy sn{subnet_netuid} at price {self.stats[subnet_netuid]['price']} because it's lower than my threshold: {buy_threshold}, currently have {current_stake_amt} alpha in it''')

            tao_amount = self.determineTokenBuyAmount(max_tao_per_buy, self.stats[subnet_netuid]['tao_in'], max_slippage)
            slippage = self.determineSlippage(tao_amount, self.stats[subnet_netuid]['tao_in'])
            if self._slippage_exceeds_limit(slippage, max_slippage):
                raise Exception(f'Stopping before purchasing too much slippage: {Decimal(slippage)}, max slippage per buy/sell: {Decimal(max_slippage)}.  \nTO FIX: increase the max_tao_per_buy variable or increase max_slippage_percent_per_buy')
            tao_amount = bt.utils.balance.tao(tao_amount)
            trade = {
                'hotkey':hotkey,
                'netuid':subnet_netuid,
                'tao_amount':tao_amount,
                'buy_threshold':buy_threshold,
                'calculated_slippage':slippage,
                'max_slippage':max_slippage / 100.0
            }
            if not preview_only:
                logger.info(f"About to stake {tao_amount} to {subnet_netuid} with expected slippage of {slippage:.4f}%")
            return trade
        if not ignore_balance_limits and not preview_only and max_tao_per_buy < float('inf') and self.balance < max_tao_per_buy:
            logger.info(f'Not enough balance to stake: {self.balance:.2f}')
        return None

    def constructSell(self, subnet_netuid, force_sell=False, desired_tao=None, force_reason=None, preview_only=False):
        patch = self._apply_live_patch(subnet_netuid)
        if patch is not None and not patch.enable_sells and not force_sell:
            return None

        sell_threshold = self.get_subnet_sell_threshold(subnet_netuid)
        max_tao_per_sell = self._limit_or_unbounded(
            self.get_subnet_setting(subnet_netuid, 'max_tao_per_sell', bagbot_settings.MAX_TAO_PER_SELL)
        )

        if patch is not None:
            sell_threshold = self.get_subnet_sell_threshold(subnet_netuid)
            patch_limit = getattr(patch, 'max_tao_per_sell', None)
            if patch_limit is not None:
                max_tao_per_sell = min(max_tao_per_sell, float(patch_limit))

        if desired_tao is not None:
            max_tao_per_sell = min(max_tao_per_sell, desired_tao)

        if subnet_netuid in self.stats and self.my_current_stake(subnet_netuid) > 0:
            hotkey = self.determineHotKey(max_tao_per_sell / self.stats[subnet_netuid]['price'], subnet_netuid)
            if hotkey is None:
                return None

            if force_sell or self.stats[subnet_netuid]['price'] > sell_threshold:
                return self._build_sell_trade(
                    subnet_netuid=subnet_netuid,
                    max_tao_per_sell=max_tao_per_sell,
                    sell_threshold=sell_threshold,
                    hotkey=hotkey,
                    force_reason=force_reason,
                    preview_only=preview_only,
                )

        return None


    async def constructRotationTrade(self):
        if not getattr(bagbot_settings, 'ENABLE_POSITION_ROTATION', False):
            return None
        if not getattr(bagbot_settings, 'ENABLE_ATOMIC_ROTATION', True):
            return None
        if getattr(bagbot_settings, 'ROTATION_REQUIRE_CONSTRAINTS', False) and not self._rotation_constraints_active():
            return None

        target_discount_floor = float(getattr(bagbot_settings, 'ROTATION_TARGET_DISCOUNT_PCT', 0.02) or 0.02)
        source_weakness_floor = float(getattr(bagbot_settings, 'ROTATION_SOURCE_WEAKNESS_PCT', 0.02) or 0.02)
        min_net_edge_pct = float(getattr(bagbot_settings, 'ROTATION_MIN_NET_EDGE_PCT', 0.01) or 0.01)
        extrinsic_fee_buffer_tao = self._rotation_extrinsic_fee_buffer_tao()

        best_trade = None
        for target_netuid in self.subnet_grids:
            if self._subnet_execution_block_reason(target_netuid):
                continue
            buy_trade = self.constructBuy(target_netuid, ignore_balance_limits=True, preview_only=True)
            if buy_trade is None:
                continue

            buy_threshold = float(buy_trade['buy_threshold'])
            if buy_threshold <= 0:
                continue

            discount_pct = max(0.0, (buy_threshold - self.stats[target_netuid]['price']) / buy_threshold)
            if discount_pct < target_discount_floor:
                continue

            desired_tao = float(buy_trade['tao_amount'])
            for source_netuid in self.subnet_grids:
                if source_netuid == target_netuid:
                    continue
                if self.my_current_stake(source_netuid) <= 0 or source_netuid not in self.stats:
                    continue

                sell_trade = self.constructSell(
                    source_netuid,
                    force_sell=True,
                    desired_tao=desired_tao,
                    preview_only=True,
                )
                if sell_trade is None:
                    continue

                sell_threshold = float(sell_trade['sell_threshold'])
                if sell_threshold <= 0:
                    continue

                weakness_pct = max(0.0, (sell_threshold - self.stats[source_netuid]['price']) / sell_threshold)
                if weakness_pct < source_weakness_floor:
                    continue

                if sell_trade['hotkey'] != buy_trade['hotkey']:
                    continue

                sim_result = await self.sub.sim_swap(
                    origin_netuid=source_netuid,
                    destination_netuid=target_netuid,
                    amount=sell_trade['alpha_amount'],
                )
                movement_fee_tao = float(sim_result.tao_fee)
                alpha_fee_tao = float(sim_result.alpha_fee) * self.stats[target_netuid]['price']
                estimated_total_fee_tao = movement_fee_tao + alpha_fee_tao + extrinsic_fee_buffer_tao
                gross_edge_pct = discount_pct + weakness_pct
                fee_drag_pct = estimated_total_fee_tao / sell_trade['approx_tao'] if sell_trade['approx_tao'] > 0 else 1.0
                net_edge_pct = gross_edge_pct - fee_drag_pct
                if net_edge_pct < min_net_edge_pct:
                    continue

                if best_trade is None or net_edge_pct > best_trade['net_edge_pct']:
                    best_trade = {
                        'type': 'rotation_swap',
                        'hotkey': sell_trade['hotkey'],
                        'origin_netuid': source_netuid,
                        'destination_netuid': target_netuid,
                        'alpha_amount': sell_trade['alpha_amount'],
                        'approx_tao': sell_trade['approx_tao'],
                        'max_slippage': min(sell_trade['max_slippage'], buy_trade['max_slippage']),
                        'source_sell_threshold': sell_trade['sell_threshold'],
                        'target_buy_threshold': buy_trade['buy_threshold'],
                        'rotation_reason': (
                            f'rotation_to_sn{target_netuid}: target is {discount_pct:.2%} below its buy threshold '
                            f'while sn{source_netuid} is {weakness_pct:.2%} below its sell threshold; '
                            f'fee_drag={fee_drag_pct:.2%}; net_edge={net_edge_pct:.2%}'
                        ),
                        'discount_pct': discount_pct,
                        'weakness_pct': weakness_pct,
                        'gross_edge_pct': gross_edge_pct,
                        'net_edge_pct': net_edge_pct,
                        'estimated_fee_tao': estimated_total_fee_tao,
                        'simulated_destination_alpha': float(sim_result.alpha_amount),
                        'mev_protection': self._mev_enabled(),
                    }

        return best_trade


    async def execute_buy_trade(self, buyTrade):
        try:
            logger.info(f"Attempting to stake {float(buyTrade['tao_amount'])} TAO to subnet {buyTrade['netuid']}")
            mev_protection = self._mev_enabled()
            stake_result = await asyncio.wait_for(
                self.sub.add_stake(
                    wallet=self.wallet,
                    hotkey_ss58=buyTrade['hotkey'],
                    netuid=buyTrade['netuid'],
                    amount=buyTrade['tao_amount'],
                    rate_tolerance=buyTrade['max_slippage'],
                    wait_for_inclusion=mev_protection,
                    wait_for_finalization=False,
                    safe_staking=True,
                    allow_partial_stake=False,
                    mev_protection=mev_protection,
                    wait_for_revealed_execution=mev_protection,
                ),
                timeout=45.0
            )
            print(f'after buy {str(buyTrade)}')
            if stake_result is True or stake_result.__dict__.get('success') is True:
                logger.info(f"Staked {float(buyTrade['tao_amount'])} TAO to subnet {buyTrade['netuid']} ({str(stake_result)})")
                if _strategy_engine is not None:
                    try:
                        est_alpha = float(buyTrade['tao_amount']) / self.stats[buyTrade['netuid']]['price'] if self.stats[buyTrade['netuid']]['price'] > 0 else 0
                        _strategy_engine.on_fill(
                            netuid=buyTrade['netuid'], side='buy',
                            tao_amount=float(buyTrade['tao_amount']),
                            alpha_amount=est_alpha,
                            price=self.stats[buyTrade['netuid']]['price'],
                        )
                    except Exception as e:
                        logger.error(f'Brains on_fill error (buy): {e}')
            else:
                block_reason = self._extract_execution_block_reason(str(stake_result))
                if block_reason:
                    self._block_subnet_execution(buyTrade['netuid'], block_reason)
                logger.info(f"Failed to stake {float(buyTrade['tao_amount'])} TAO to subnet {buyTrade['netuid']} ({str(stake_result)})")
        except asyncio.TimeoutError:
            logger.error(f"Timeout staking on subnet {buyTrade['netuid']} after 45s")
        except Exception as e:
            print(f'ERROR staking')
            logger.error(traceback.format_exc())
            logger.error(f"Failed to stake on subnet {buyTrade['netuid']}: {e}")


    async def execute_sell_trade(self, sellTrade):
        try:
            logger.info(f"Attempting to unstake {float(sellTrade['alpha_amount'])} alpha from subnet {sellTrade['netuid']}")
            mev_protection = self._mev_enabled()
            unstake_result = await asyncio.wait_for(
                self.sub.unstake(
                    wallet=self.wallet,
                    hotkey_ss58=sellTrade['hotkey'] ,
                    netuid=sellTrade['netuid'],
                    amount=sellTrade['alpha_amount'],
                    rate_tolerance=sellTrade['max_slippage'],
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                    safe_unstaking=True,
                    allow_partial_stake=False,
                    mev_protection=mev_protection,
                    wait_for_revealed_execution=mev_protection,
                ),
                timeout=60.0
            )
            print(f'after sell {str(sellTrade)}')
            if unstake_result is True or unstake_result.__dict__.get('success') is True:
                if sellTrade.get('rotation_reason'):
                    logger.info(f"Rotation exit executed on sn{sellTrade['netuid']}: {sellTrade['rotation_reason']}")
                logger.info(f"Unstaked {float(sellTrade['alpha_amount'])} stake units from sn{sellTrade['netuid']} (approx. {sellTrade['approx_tao']:.4f} TAO value) at price: {self.stats[sellTrade['netuid']]['price']}.  my threshold = {sellTrade['sell_threshold']}")
                if _strategy_engine is not None:
                    try:
                        _strategy_engine.on_fill(
                            netuid=sellTrade['netuid'], side='sell',
                            tao_amount=sellTrade['approx_tao'],
                            alpha_amount=float(sellTrade['alpha_amount']),
                            price=self.stats[sellTrade['netuid']]['price'],
                        )
                    except Exception as e:
                        logger.error(f'Brains on_fill error (sell): {e}')
            else:
                logger.info(f"Failed to unstake {str(sellTrade)}  sn{sellTrade['netuid']} ({str(unstake_result)})")
        except asyncio.TimeoutError:
            msg = f"Timeout unstaking from subnet {sellTrade['netuid']} after 60s"
            print(msg)
            logger.error(msg)
            self.sub = await my_async_subtensor("finney")
        except (asyncio.exceptions.CancelledError, asyncio.exceptions.InvalidStateError) as e:
            print(f'ERROR unstaking - {e}... continuing')
            logger.error(traceback.format_exc())
            logger.error(f"Failed to unstake from subnet {sellTrade['netuid']}: {e}")
            self.sub = await my_async_subtensor("finney")
        except Exception as e:
            print(f'ERROR unstaking')
            logger.error(traceback.format_exc())
            logger.error(f"Failed to unstake from subnet {sellTrade['netuid']}: {e}")
            raise


    async def execute_rotation_trade(self, rotationTrade):
        async def submit_rotation(mev_protection):
            return await asyncio.wait_for(
                self.sub.swap_stake(
                    wallet=self.wallet,
                    hotkey_ss58=rotationTrade['hotkey'],
                    origin_netuid=rotationTrade['origin_netuid'],
                    destination_netuid=rotationTrade['destination_netuid'],
                    amount=rotationTrade['alpha_amount'],
                    safe_swapping=True,
                    allow_partial_stake=False,
                    rate_tolerance=rotationTrade['max_slippage'],
                    mev_protection=mev_protection,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                    wait_for_revealed_execution=mev_protection,
                ),
                timeout=60.0
            )

        def record_rotation_fill():
            if _strategy_engine is not None:
                try:
                    _strategy_engine.on_fill(
                        netuid=rotationTrade['origin_netuid'], side='sell',
                        tao_amount=rotationTrade['approx_tao'],
                        alpha_amount=float(rotationTrade['alpha_amount']),
                        price=self.stats[rotationTrade['origin_netuid']]['price'],
                    )
                    _strategy_engine.on_fill(
                        netuid=rotationTrade['destination_netuid'], side='buy',
                        tao_amount=max(0.0, rotationTrade['approx_tao'] - rotationTrade['estimated_fee_tao']),
                        alpha_amount=rotationTrade['simulated_destination_alpha'],
                        price=self.stats[rotationTrade['destination_netuid']]['price'],
                    )
                except Exception as e:
                    logger.error(f'Brains on_fill error (rotation): {e}')

        try:
            logger.info(
                f"Attempting atomic swap from sn{rotationTrade['origin_netuid']} to sn{rotationTrade['destination_netuid']} "
                f"for {float(rotationTrade['alpha_amount'])} alpha with expected net edge {rotationTrade['net_edge_pct']:.2%}"
            )
            swap_result = await submit_rotation(rotationTrade['mev_protection'])
            print(f'after rotation {str(rotationTrade)}')
            if swap_result is True or swap_result.__dict__.get('success') is True:
                logger.info(
                    f"Rotation swap executed: sn{rotationTrade['origin_netuid']} -> sn{rotationTrade['destination_netuid']} | "
                    f"{rotationTrade['rotation_reason']}"
                )
                record_rotation_fill()
                return True
            swap_result_text = str(swap_result)
            if rotationTrade['mev_protection'] and self._mev_rotation_outcome_failed(swap_result_text):
                logger.warning(
                    f"Shielded rotation outcome unavailable for sn{rotationTrade['origin_netuid']} -> "
                    f"sn{rotationTrade['destination_netuid']}; retrying without MEV protection"
                )
                retry_result = await submit_rotation(False)
                if retry_result is True or retry_result.__dict__.get('success') is True:
                    logger.info(
                        f"Rotation swap executed without MEV fallback: sn{rotationTrade['origin_netuid']} -> "
                        f"sn{rotationTrade['destination_netuid']} | {rotationTrade['rotation_reason']}"
                    )
                    record_rotation_fill()
                    return True
                swap_result = retry_result
                swap_result_text = str(retry_result)
            block_reason = self._extract_execution_block_reason(swap_result_text)
            if block_reason:
                self._block_subnet_execution(rotationTrade['destination_netuid'], block_reason)
            logger.info(f"Failed rotation swap {rotationTrade} ({str(swap_result)})")
            return False
        except asyncio.TimeoutError:
            msg = (
                f"Timeout rotating stake from sn{rotationTrade['origin_netuid']} "
                f"to sn{rotationTrade['destination_netuid']} after 60s"
            )
            print(msg)
            logger.error(msg)
            self.sub = await my_async_subtensor("finney")
            return False
        except Exception as e:
            print('ERROR rotating')
            logger.error(traceback.format_exc())
            logger.error(
                f"Failed to rotate from sn{rotationTrade['origin_netuid']} "
                f"to sn{rotationTrade['destination_netuid']}: {e}"
            )
            raise


    async def do_available_trades(self, subnet_netuid):
        buyTrade = None
        if not self._subnet_execution_block_reason(subnet_netuid):
            buyTrade = self.constructBuy(subnet_netuid)
        if buyTrade:
            await self.execute_buy_trade(buyTrade)

        sellTrade = self.constructSell(subnet_netuid)
        if sellTrade:
            await self.execute_sell_trade(sellTrade)


if __name__ == "__main__":
    args = parseArgs()
    binterface = BittensorUtility(args)
    try:
        asyncio.run(binterface.run())
    except KeyboardInterrupt:
        logger.info("Service stopped by user.")
    except Exception as e:
        logger.error(traceback.format_exc())
        logger.critical(f"Critical error: {e}")
        print(traceback.format_exc())
        binterface.sendNotification(f"Bittensor interface Broke: {e}")
