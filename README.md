# Bagbot
A bot for accumulating alpha in the Bittensor Alpha Group.

> **⚠️ Warning: Use at your own risk!** There are no guarantees! Try with small amounts first!!

> [!CAUTION]
**HIGHLY RECOMMENDED TO JOIN THE [BITTENSOR ALPHA GROUP](https://taotemplar.com/bag) FOR HELP WITH USE AND STRATEGY**

> [!NOTE]
> See [TARGON_SETUP_NOTES.md](TARGON_SETUP_NOTES.md) for the current Targon runtime layout, important bot files, PM2 process names, and live deployment notes.

## Setup Instructions

Follow these steps to set up and run Bagbot:

1. **Clone the Repository**  
   ```bash
   git clone https://github.com/taotemplar/bagbot.git
   ```

2. **Navigate to the Bagbot Directory**  
   ```bash
   cd bagbot
   ```

3. **Set Up a Python Virtual Environment**

   Install, create, and activate a Python virtual environment on **Windows**:
   ```bash
   pip3 install virtualenv
   virtualenv ~/.bagbotvirtualenv/
   source ~/.bagbotvirtualenv/bin/activate
   ```
   on **MacOS**:
   ```bash
   pip3 install virtualenv
   python3.10 venv .bagbotvirtualenv
   source .bagbotvirtualenv/bin/activate
   ```

   ***Note: At the time of writing, Bittensor CLI is compatible with python versions from 3.9.0 to 3.12.0.***

   Check the python version:
   ```bash
   python3 --version
   ```
   Install compatible python version and pip3 on **windows wsl**:
   ```bash
   sudo apt install python3.10
   ```
   on **MacOS**:
   ```bash
   brew install python3.10
   echo 'export PATH="/opt/homebrew/opt/python@3.10/bin:$PATH"' >> ~/.zshrc
   ```

4. **Install Requirements**  
   ```bash
   pip3 install -r requirements.txt
   ```

5. **Create a New Wallet**  
   ```bash
   btcli w create --wallet.name bagbot
   ```

6. **Fund the Wallet**  
   Send a small amount to the wallet address. To find the address, run the following command and look for the `ss58_address` (e.g., `5Dso...xAi3`):
   ```bash
   btcli w list
   ```

7. **Configure Buy/Sell Settings**  
   Copy the top part of the `bagbot_settings.py` file to a new file named `bagbot_settings_overrides.py`.  
   **Note:** Do **not** copy the bottom 4 lines.

8. **Edit the Settings File**  
   In `bagbot_settings_overrides.py`:
   - Either update `WALLET_PW` with your wallet password, or point `WALLET_PW_FILE` at a local file containing only the password.
   - Modify other settings as desired. The file includes notes explaining each variable.

## Running the Bot

To start the bot, activate the virtual environment and run the script:
```bash
source ~/.bagbotvirtualenv/bin/activate
python3 bagbot.py
```

## Offline Research

Bagbot now ships with an offline replay harness for Brains config experiments. It replays recorded subnet bars from `Brains/price_history.db`, applies the live `StrategyEngine`, and scores candidate YAML configs on net TAO, drawdown, and turnover.

Example:

```bash
python3 Brains/research_harness.py \
  --hours 168 \
  --config Brains/config/threshold_farm.yaml \
  --config /tmp/candidate.yaml
```

Use this before promoting nontrivial `threshold_farm.yaml` changes into a live Targon runtime.
