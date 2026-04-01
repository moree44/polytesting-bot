# Polytesting Bot (poly-bot-web)

Local Polymarket BTC 5 minute bot with a web UI. The Python backend reads Polymarket CLOB markets, uses Chainlink BTC via Polymarket RTDS for live price, and serves a browser dashboard and trading controls. Historical BTC candles are loaded from Binance for chart bootstrap.

**Summary**
- Local web UI with charts, positions, PnL, and logs.
- Market buy, limit buy, limit sell, cashout, cancel order.
- Execution guardrails such as min size, slippage, and anti double click.
- Auto switch to the next 5 minute market and prefetch tokens.
- Optional UI authentication with username and password.

**Folder Structure**
- `polybot_web.py` main backend and HTTP server.
- `webui/` static UI (HTML, CSS, JS) served by the backend.
- `run_web.sh` launcher using the local `venv`.
- `.env.example` configuration template.

## Requirements
- Linux / WSL
- Python 3.10+
- A funded Polymarket CLOB wallet
- Network access to Polymarket CLOB HTTP and WebSocket
- Network access to Binance HTTP for candles
- Network access to the `lightweight-charts` CDN for UI charts

## Quick Start
1. Clone the repo and enter the folder.
2. Create a virtualenv and install deps.
3. Copy `.env.example` to `.env` and fill in secrets.
4. Run `./run_web.sh`.
5. Open `http://127.0.0.1:8787`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
./run_web.sh
```

## `.env` Configuration
Fill all **Required secrets** before running. Use `.env.example` as the starting reference.

**Required secrets**
- `PK` wallet private key
- `WALLET_ADDRESS` wallet address
- `POLY_PROXY` proxy address for Polymarket
- `CLOB_FUNDER` funder address, falls back to `POLY_PROXY` or `WALLET_ADDRESS` if empty

**Core**
| Key | Default | Purpose |
| --- | --- | --- |
| `CLOB_SIGNATURE_TYPE` | `2` | Signature type for the CLOB client. |
| `DRY_RUN` | `1` | Simulation mode, no orders sent. |
| `WEB_HOST` | `127.0.0.1` | Web server bind host. |
| `WEB_PORT` | `8787` | Web server port. |

**Trading guards**
| Key | Default | Purpose |
| --- | --- | --- |
| `MAX_ENTRY_CENT` | `99` | Max entry price in cents. |
| `MIN_MARKET_TIME_LEFT` | `45` | Minimum time left for entry. |
| `ENTRY_SLIPPAGE_BPS` | `50` | Entry slippage in bps. |
| `EXIT_SLIPPAGE_BPS` | `80` | Exit slippage in bps. |
| `MIN_ORDER_SHARES` | `0.01` | Minimum order size in shares. |
| `MIN_ORDER_USD` | `0.01` | Minimum order size in USD. |
| `STRICT_EXECUTION` | `1` | Conservative execution flow. |
| `BUY_CMD_GUARD_SEC` | `1.2` | Cooldown to avoid double buy. |
| `MARKET_BUY_ORDER_TYPE` | forced `FOK` | Market buys are forced to FOK in code. |
| `MARKET_BUY_MAX_ATTEMPTS` | forced `1` | Market buys do not retry in code. |

**Position sync**
| Key | Default | Purpose |
| --- | --- | --- |
| `POSITION_SYNC_GRACE` | `8` | Grace period for position sync. |
| `POSITION_DUST_SHARES` | `0.005` | Small position threshold in shares. |
| `POSITION_DUST_USD` | `0.02` | Small position threshold in USD. |

**Chart and probability**
| Key | Default | Purpose |
| --- | --- | --- |
| `CHART_SAMPLE_SEC` | `0.3` | Chart sampling interval in seconds. |
| `CHART_MAX_CANDLES_1M` | `360` | Max number of 1m candles kept. |
| `PROB_VOL_WINDOW_SEC` | `240` | Volatility window in seconds. |
| `PROB_DEFAULT_VOL_ANNUAL` | `0.75` | Default annualized volatility if data is limited. |
| `PROB_W_MARKET` | `0.50` | Probability weight from market. |
| `PROB_W_PTB` | `0.40` | Probability weight from PTB. |
| `PROB_W_MICRO` | `0.10` | Probability weight from micro signal. |
| `PROB_SCORE_DRIFT_SEC` | `15` | Drift tolerance for probability score. |

**PTB and switching**
| Key | Default | Purpose |
| --- | --- | --- |
| `PTB_MAX_DRIFT_SEC` | `1` | Max PTB timestamp drift before stale. |
| `PTB_WEB_FALLBACK` | `0` | PTB web fallback. |
| `PTB_WEB_RETRY_SEC` | `30` | Retry interval for PTB web fallback. |
| `NEXT_PREFETCH_SEC` | `120` | Prefetch lead time for next market. |
| `SWITCH_MIN_REMAINING_SEC` | `10` | Minimum seconds left before switching target. |

**Binance HTTP**
| Key | Default | Purpose |
| --- | --- | --- |
| `BINANCE_FORCE_IPV4` | `1` | Force IPv4 for requests. |
| `BINANCE_HTTP_TIMEOUT` | `10` | Request timeout in seconds. |
| `BINANCE_HTTP_PROXY` | empty | HTTP proxy. |
| `BINANCE_HTTPS_PROXY` | empty | HTTPS proxy. |
| `BINANCE_API_BASES` | list | Binance base URLs for fallback. |

**Web auth**
| Key | Default | Purpose |
| --- | --- | --- |
| `WEB_USER` | empty | UI login username. |
| `WEB_PASS` | empty | UI login password. |
| `WEB_AUTH_TTL_SEC` | `3600` | Login session TTL. |

**Note about .env.example**
`.env.example` includes some legacy keys that are not used by `polybot_web.py` at the moment, for example `BUY_FAIL_FAST` and `PTB_EXECUTION_WORKER`. You can ignore them.

## Running
- Run `./run_web.sh` or `python3 polybot_web.py`
- Open the UI at `http://127.0.0.1:8787`

**DRY vs REAL**
- Default is `DRY_RUN=1` which is simulation mode.
- The `DRY/REAL` button in the UI toggles runtime mode.

## UI Guide
**Top bar**
- `SLUG` current 5 minute market.
- `T` time left in the interval.
- `WS` WebSocket status.
- `MODE` DRY or REAL.
- `BAL` balance.
- `SESSION` session PnL.
- `WR` win rate.

**Target market**
- `CURRENT` trades the current interval.
- `NEXT` places orders in the next market.
- `PREVIOUS` is read only for previous market data.

**Order panel**
- `BUY UP/DOWN` market buy with the USD amount.
- `BUY NEXT` for the next market target.
- `LIMIT BUY` with price in cents and USD size.
- `CASH OUT` sells the position in the active target market.
- `PREV` cashes out the previous market position.
- `LIMIT SELL` with price in cents and shares from position.

**Logs and open orders**
- `SYSTEM` and `TRADE` tabs separate logs.
- `COPY` copies the log.
- Open Orders shows active orders with a cancel button per order.

## Command Manual (Input Box)
You can send commands via the input box.

**Trading**
- `bu <usd>` buy UP market.
- `bd <usd>` buy DOWN market.
- `bnu <usd>` buy UP in the next market.
- `bnd <usd>` buy DOWN in the next market.
- `lu <cent> <usd>` limit buy UP.
- `ld <cent> <usd>` limit buy DOWN.
- `su <cent> [shares]` limit sell UP, shares are optional.
- `sd <cent> [shares]` limit sell DOWN, shares are optional.
- `cu` cash out UP.
- `cd` cash out DOWN.
- `cpu` cash out UP automatically, includes off market positions.
- `cpd` cash out DOWN automatically, includes off market positions.
- `ca` cancel all open orders.
- `co <order_id>` cancel a specific order.

**System**
- `dry on` or `dry off` toggle DRY or REAL.
- `target current|next|previous` set target market.
- `r` force refresh token/market.
- `q` stop bot.

## Security and Access
- The `state`, `chart`, and `cmd` APIs only accept local client requests.
- Do not set `WEB_HOST=0.0.0.0` unless you understand the risk.
- For remote access, use SSH port forwarding and keep binding to `127.0.0.1`.
- Enable `WEB_USER` and `WEB_PASS` for UI login.
- Never commit `.env`.

## Troubleshooting
**Auth failed**
- Check `PK`, `WALLET_ADDRESS`, `POLY_PROXY`, and `CLOB_FUNDER`.
- Make sure `CLOB_SIGNATURE_TYPE` matches your wallet setup.

**Address already in use**
- Change `WEB_PORT` or stop the old process.

**WebSocket reconnect loop**
- Check network connectivity.
- Make sure Polymarket endpoints are reachable.

**PTB appears late**
- Wait a few seconds after switching markets.
- If it is often empty, consider `PTB_WEB_FALLBACK=1`.

## Dev Notes
- Frontend lives in `webui/` and is served statically.
- If `webui/index.html` is missing, the server falls back to built in HTML.
- `run_web.sh` assumes a venv at `./venv`.
