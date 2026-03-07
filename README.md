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

5. Edit `.env` and set your real values.

## Example .env

Use this as a starting point for local testing.

```env
# Required secrets
PK=0xyour_private_key
WALLET_ADDRESS=0xyour_wallet_address
POLY_PROXY=0xyour_proxy_address
CLOB_FUNDER=0xyour_funder_address

# Core
CLOB_SIGNATURE_TYPE=2
DRY_RUN=1
WEB_HOST=127.0.0.1
WEB_PORT=8787

# Trading guards
MAX_ENTRY_CENT=99
MIN_MARKET_TIME_LEFT=45
ENTRY_SLIPPAGE_BPS=50
EXIT_SLIPPAGE_BPS=80
MIN_ORDER_SHARES=0.01
MIN_ORDER_USD=0.01
STRICT_EXECUTION=1
BUY_CMD_GUARD_SEC=1.2

# Position sync
POSITION_SYNC_GRACE=20
POSITION_DUST_SHARES=0.005
POSITION_DUST_USD=0.02

# Optional
CENT_DECIMALS=0
GTC_FALLBACK_TIMEOUT=0
TERM_STATUS_INTERVAL=10
NEXT_PREFETCH_SEC=120
SWITCH_MIN_REMAINING_SEC=10
PTB_MAX_DRIFT_SEC=2
PTB_WEB_FALLBACK=0
PTB_WEB_RETRY_SEC=30
```

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

## Safe First Run Checklist

1. Start with `DRY_RUN=1`.
2. Confirm market switch logs are healthy.
3. Confirm PTB and NOW are updating.
4. Test UI commands in dry mode.
5. Switch to real mode only with small order size.

## Development Workflow

Recommended flow for contributors:

1. Pull latest changes:

```bash
git pull
```

2. Create a feature branch:

```bash
git checkout -b feature/your-change
```

3. Run in dry mode while developing.
4. Keep changes focused and small.
5. Open a pull request with logs/screenshots for behavior changes.

## Troubleshooting

### Auth failed: `PolyApiException[status_code=None, error_message=Request exception!]`

- Verify internet access.
- Re-check `PK`, `WALLET_ADDRESS`, `POLY_PROXY`, `CLOB_FUNDER`.
- Confirm signature type (`CLOB_SIGNATURE_TYPE`) is correct for your setup.

### `Address already in use`

Another process is already using the same `WEB_PORT`.

- Stop the old process, or
- Change `WEB_PORT` in `.env`.

### PTB shows `-` for a while

This can happen briefly after switch while waiting for Chainlink samples in allowed drift.

- Keep `PTB_WEB_FALLBACK=0` for stricter safety.
- Confirm RTDS is connected in logs.

### WebSocket reconnect loops

- Check network stability.
- Confirm host can reach:
  - `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  - `wss://ws-live-data.polymarket.com`

## Security

- Never share your private key.
- Never commit `.env`.
- Use small order sizes when testing live mode.

## Notes

- `.env.example` is the source of truth for config keys.
- Keep sensitive values only in local `.env`.
