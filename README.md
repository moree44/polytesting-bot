# Polytesting Bot (poly-bot-web)

A local web interface for running a Polymarket BTC 5-minute up/down bot.

## Requirements

- Linux or WSL
- Python 3.10+
- Access to a funded wallet configured for Polymarket CLOB
- Network access to Polymarket APIs and WebSocket endpoints

## Installation

1. Clone the repository:

```bash
git clone https://github.com/moree44/polytesting-bot.git
cd polytesting-bot
```

2. Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

3. Install dependencies:

```bash
pip install --upgrade pip
pip install requests websocket-client python-dotenv py-clob-client
```

4. Create your environment file:

```bash
cp .env.example .env
```

5. Edit `.env` and set required secrets:

- `PK`
- `WALLET_ADDRESS`
- `POLY_PROXY`
- `CLOB_FUNDER`

Keep `.env` private and never commit it.

## Run

Run the bot:

```bash
./run_web.sh
```

If your local script points to a different Python path, run directly:

```bash
python3 polybot_web.py
```

Open the UI:

- `http://127.0.0.1:8787`

## Recommended First Run Settings

In `.env`, start in dry mode:

```env
DRY_RUN=1
STRICT_EXECUTION=1
PTB_WEB_FALLBACK=0
```

After validating behavior, switch to real mode carefully.

## Configuration Notes

Key options in `.env`:

- `DRY_RUN`: `1` for simulation, `0` for live orders
- `WEB_HOST`, `WEB_PORT`: web server bind address/port
- `MAX_ENTRY_CENT`: max allowed entry ask price in cents
- `MIN_MARKET_TIME_LEFT`: blocks new entries near expiry
- `ENTRY_SLIPPAGE_BPS`, `EXIT_SLIPPAGE_BPS`: order slippage controls
- `BUY_CMD_GUARD_SEC`: duplicate buy command protection window
- `PTB_MAX_DRIFT_SEC`: max allowed timestamp drift for PTB lock

## Security

- Do not share your private key.
- Do not commit `.env`.
- Use small order sizes when testing live mode.

## Updating

Pull latest changes:

```bash
git pull
```

If dependencies changed, reinstall:

```bash
pip install -r requirements.txt
```

If `requirements.txt` is not present in your local copy, install packages manually as shown above.
