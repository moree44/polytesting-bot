#!/usr/bin/env python3
"""
Polymarket BTC 5m Bot - Local Web UI
Run: python polybot_web.py
Open: http://127.0.0.1:8787
"""

import json
import math
import os
import queue
import re
import signal
import socket
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import urllib3.util.connection
import websocket
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

PK = os.getenv("PK")
ADDR = os.getenv("WALLET_ADDRESS")
POLY_PROXY = os.getenv("POLY_PROXY")
SIG_TYPE = int(os.getenv("CLOB_SIGNATURE_TYPE", "2"))
FUNDER = os.getenv("CLOB_FUNDER") or POLY_PROXY or ADDR
HOST = "https://clob.polymarket.com"
DRY_RUN = os.getenv("DRY_RUN", "1").lower() in ("1", "true", "yes", "on")
MAX_ENTRY_CENT = float(os.getenv("MAX_ENTRY_CENT", "99"))
CENT_DECIMALS = max(0, min(2, int(os.getenv("CENT_DECIMALS", "2"))))
MIN_MARKET_TIME_LEFT = int(os.getenv("MIN_MARKET_TIME_LEFT", "45"))
GTC_FALLBACK_TIMEOUT = int(os.getenv("GTC_FALLBACK_TIMEOUT", "0"))
# PATCH 1: grace period 20 -> 8 agar position sync lebih cepat
POSITION_SYNC_GRACE = int(os.getenv("POSITION_SYNC_GRACE", "8"))
POSITION_DUST_SHARES = float(os.getenv("POSITION_DUST_SHARES", "0.005"))
POSITION_DUST_USD = float(os.getenv("POSITION_DUST_USD", "0.02"))
ENTRY_SLIPPAGE_BPS = int(os.getenv("ENTRY_SLIPPAGE_BPS", "50"))
EXIT_SLIPPAGE_BPS = int(os.getenv("EXIT_SLIPPAGE_BPS", "80"))
MIN_ORDER_SHARES = float(os.getenv("MIN_ORDER_SHARES", "0.01"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "0.01"))
STRICT_EXECUTION = os.getenv("STRICT_EXECUTION", "1").lower() in ("1", "true", "yes", "on")
TERM_STATUS_INTERVAL = max(3, int(os.getenv("TERM_STATUS_INTERVAL", "10")))
PTB_MAX_DRIFT_SEC = max(1, min(15, int(os.getenv("PTB_MAX_DRIFT_SEC", "1"))))
PTB_WEB_FALLBACK = os.getenv("PTB_WEB_FALLBACK", "0").lower() in ("1", "true", "yes", "on")
PTB_WEB_RETRY_SEC = max(10, min(300, int(os.getenv("PTB_WEB_RETRY_SEC", "30"))))
PTB_WORKER_HOST = os.getenv("PTB_WORKER_HOST", "127.0.0.1")
PTB_WORKER_PORT = int(os.getenv("PTB_WORKER_PORT", "8788"))
PTB_EXECUTION_WORKER_URL = os.getenv("PTB_EXECUTION_WORKER_URL", f"http://{PTB_WORKER_HOST}:{PTB_WORKER_PORT}").rstrip("/")
PTB_EXECUTION_WORKER = os.getenv("PTB_EXECUTION_WORKER", "0").lower() in ("1", "true", "yes", "on")
PTB_WORKER_ORDER_TIMEOUT = max(0.5, min(10.0, float(os.getenv("PTB_WORKER_ORDER_TIMEOUT", "2.5"))))
CHART_SAMPLE_SEC = max(1.0, min(5.0, float(os.getenv("CHART_SAMPLE_SEC", "1.0"))))
CHART_MAX_CANDLES_1M = max(120, min(1440, int(os.getenv("CHART_MAX_CANDLES_1M", "360"))))
BUY_CMD_GUARD_SEC = max(0.3, min(3.0, float(os.getenv("BUY_CMD_GUARD_SEC", "1.2"))))
UNCERTAIN_BUY_VERIFY_SEC = max(5, min(60, int(os.getenv("UNCERTAIN_BUY_VERIFY_SEC", "18"))))
UNCERTAIN_BUY_POLL_SEC = max(0.5, min(3.0, float(os.getenv("UNCERTAIN_BUY_POLL_SEC", "1.0"))))
MARKET_BUY_ORDER_TYPE_RAW = str(os.getenv("MARKET_BUY_ORDER_TYPE", "FAK")).upper()
USER_WS_ENABLED = os.getenv("USER_WS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
PROB_VOL_WINDOW_SEC = max(60, min(1200, int(os.getenv("PROB_VOL_WINDOW_SEC", "240"))))
PROB_DEFAULT_VOL_ANNUAL = max(0.05, min(4.0, float(os.getenv("PROB_DEFAULT_VOL_ANNUAL", "0.75"))))
PROB_W_MARKET = max(0.0, float(os.getenv("PROB_W_MARKET", "0.50")))
PROB_W_PTB = max(0.0, float(os.getenv("PROB_W_PTB", "0.40")))
PROB_W_MICRO = max(0.0, float(os.getenv("PROB_W_MICRO", "0.10")))
PROB_SCORE_DRIFT_SEC = max(3, min(30, int(os.getenv("PROB_SCORE_DRIFT_SEC", "15"))))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))
WEB_UI_VERSION = "web-v3.1"
NEXT_PREFETCH_SEC = max(30, min(240, int(os.getenv("NEXT_PREFETCH_SEC", "120"))))
SWITCH_MIN_REMAINING_SEC = max(5, min(180, int(os.getenv("SWITCH_MIN_REMAINING_SEC", "10"))))
BINANCE_HTTP_TIMEOUT = max(3.0, min(20.0, float(os.getenv("BINANCE_HTTP_TIMEOUT", "10"))))
BINANCE_FORCE_IPV4 = os.getenv("BINANCE_FORCE_IPV4", "1").lower() in ("1", "true", "yes", "on")
BINANCE_HTTP_PROXY = os.getenv("BINANCE_HTTP_PROXY", "").strip()
BINANCE_HTTPS_PROXY = os.getenv("BINANCE_HTTPS_PROXY", "").strip()
BINANCE_API_BASES = [
    base.strip().rstrip("/")
    for base in os.getenv(
        "BINANCE_API_BASES",
        "https://api.binance.com,https://api1.binance.com,https://api2.binance.com,https://api3.binance.com,https://data-api.binance.vision",
    ).split(",")
    if base.strip()
]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_INDEX = os.path.join(BASE_DIR, "webui", "index.html")
MARKET_BUY_ORDER_TYPE = OrderType.FOK if MARKET_BUY_ORDER_TYPE_RAW == "FOK" else OrderType.FAK
MARKET_BUY_ORDER_TYPE_LABEL = "FOK" if MARKET_BUY_ORDER_TYPE_RAW == "FOK" else "FAK"

try:
    client = ClobClient(HOST, key=PK, chain_id=137, signature_type=SIG_TYPE, funder=FUNDER)
    client.set_api_creds(client.create_or_derive_api_creds())
    print(f"Auth OK (sig_type={SIG_TYPE}, funder={FUNDER})")
except Exception as e:
    print(f"Auth failed: {e}")
    raise SystemExit(1)

state_lock = threading.RLock()
state = {
    "up_token": None,
    "down_token": None,
    "next_up_token": None,
    "next_down_token": None,
    "prev_up_token": None,
    "prev_down_token": None,
    "up_bid": "-",
    "up_ask": "-",
    "down_bid": "-",
    "down_ask": "-",
    "prev_up_bid": "-",
    "prev_up_ask": "-",
    "prev_down_bid": "-",
    "prev_down_ask": "-",
    "interval_end": 0,
    "current_slug": "-",
    "target_market": "current",
    "current_market_id": "",
    "next_slug": None,
    "next_interval_end": 0,
    "prev_slug": None,
    "prev_interval_end": 0,
    "ws_ok": False,
    "last_ws": 0,
    "last_quote_ts": 0,
    "last_switch_ts": 0,
    "switch_pending": False,
    "log": [],
    "trade_log": [],
    "positions": {},
    "open_orders": [],
    "open_orders_remote": [],
    "balance": "-",
    "session_pnl": 0.0,
    "session_pnl_dry": 0.0,
    "session_pnl_real": 0.0,
    "running": True,
    "dry_run": DRY_RUN,
    "last_real_action_ts": 0.0,
    "snapshot_seq": 0,
    "btc_price_now": None,
    "btc_price_to_beat": None,
    "prob_up": 0.5,
    "prob_down": 0.5,
    "prob_confidence": 0.0,
    "prob_market": 0.5,
    "prob_ptb": 0.5,
    "prob_micro": 0.5,
    "prob_win_total": 0,
    "prob_win_wins": 0,
    "prob_win_losses": 0,
    "prob_win_rate": 0.0,
    "prob_last_result": "-",
    "prob_open_up": None,
    "prob_open_down": None,
    "prob_open_confidence": None,
    "prob_open_slug": "-",
    "prob_open_at": 0,
    "worker_ok": False,
    "worker_quotes": {},
    "worker_recent_events": [],
    "worker_last_sync_ts": 0,
}

cmd_queue: "queue.Queue[str]" = queue.Queue(maxsize=200)
ws_app = None
_ws_connecting = False
_ws_lock = threading.Lock()
_ws_reconnect_delay = 3.0
user_ws_app = None
_userws_connecting = False
_userws_lock = threading.Lock()
_userws_reconnect_delay = 3.0
_userws_seen_lock = threading.Lock()
_userws_seen_keys = []
_userws_seen_keyset = set()
_USERWS_SEEN_MAX = 600
_userws_trade_fill_lock = threading.Lock()
_userws_trade_fill_ids = []
_userws_trade_fill_idset = set()
_USERWS_TRADE_FILL_MAX = 800
last_market_switch = 0
active_tokens = set()
market_queues = {}
market_queue_lock = threading.Lock()
next_quote_cache = {}
_rtds_connecting = False
_rtds_lock = threading.Lock()
_cl_lock = threading.Lock()
_cl_ring = []
_CL_RING_MAX = 90
_ptb_web_last_try = {}
_buy_cmd_lock = threading.Lock()
_last_buy_cmd = {"sig": "", "ts": 0.0}
_buy_pending_lock = threading.Lock()
_buy_pending_until = {}
_cashout_lock = threading.Lock()
_cashout_inflight = set()
_prob_live_lock = threading.Lock()
_prob_live_by_slug = {}
_prob_open_by_slug = {}
_prob_scored_lock = threading.Lock()
_prob_scored_slugs = set()
MAX_CMD_BODY_BYTES = 4096
LOCAL_ONLY_NETS = ("127.", "::1", "0:0:0:0:0:0:0:1", "::ffff:127.")
chart_lock = threading.RLock()
chart_state = {
    "up_1m": [],
    "btc_1m": [],
}


def load_frontend_html() -> str:
    try:
        with open(FRONTEND_INDEX, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return HTML


def worker_api(path: str) -> str:
    return f"{PTB_EXECUTION_WORKER_URL}{path}"


def worker_post(path: str, payload: dict, timeout: float = 1.0):
    if not PTB_EXECUTION_WORKER:
        return None, "worker disabled"
    try:
        r = requests.post(worker_api(path), json=payload, timeout=timeout)
    except Exception as e:
        return None, str(e)[:180]
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "error": f"http {r.status_code}"}
    if r.status_code >= 400 or not data.get("ok", False):
        return None, str(data.get("error") or data.get("error_msg") or f"http {r.status_code}")[:220]
    return data, ""


def worker_watch_tokens(token_ids):
    ids = [str(t).strip() for t in (token_ids or []) if str(t).strip()]
    if not ids or not PTB_EXECUTION_WORKER:
        return
    worker_post("/api/watch", {"asset_ids": ids}, timeout=0.6)


def worker_market_order(token_id: str, side: str, amount: float, amount_kind: str, price: float = None, order_type: str = None):
    amount_kind_norm = str(amount_kind).lower()
    amount_fmt = ".2f" if amount_kind_norm in ("share", "shares") else ".2f"
    payload = {
        "token_id": str(token_id),
        "side": str(side),
        "amount": format(float(amount), amount_fmt),
        "amount_kind": str(amount_kind),
    }
    if price is not None and price > 0:
        payload["price"] = f"{float(price):.2f}"
    if order_type:
        payload["order_type"] = str(order_type)
    return worker_post("/api/order/market", payload, timeout=PTB_WORKER_ORDER_TIMEOUT)


def refresh_worker_state_once():
    if not PTB_EXECUTION_WORKER:
        return
    ok = False
    quotes = {}
    recent = []
    try:
        r = requests.get(worker_api("/api/state"), timeout=0.7)
        data = r.json() if r.ok else {}
        if r.ok:
            ok = True
            rows = data.get("quotes") or []
            quotes = {str(q.get("token_id", "")): q for q in rows if isinstance(q, dict)}
            recent = [str(x) for x in (data.get("recent_events") or [])][-80:]
    except Exception:
        ok = False
    with state_lock:
        state["worker_ok"] = ok
        state["worker_quotes"] = quotes
        state["worker_recent_events"] = recent
        state["worker_last_sync_ts"] = int(time.time())


def worker_state_loop():
    while state["running"]:
        try:
            refresh_worker_state_once()
        except Exception:
            pass
        time.sleep(1.0)


def fmt_usd(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "-"


def candle_bucket(ts: int, sec: int) -> int:
    return int(ts // sec) * sec


def update_ohlc_row(rows, bucket_ts: int, price: float, extra: dict = None):
    if price is None:
        return
    px = float(price)
    if px <= 0:
        return
    if rows and int(rows[-1]["ts"]) == int(bucket_ts):
        row = rows[-1]
        row["high"] = max(float(row["high"]), px)
        row["low"] = min(float(row["low"]), px)
        row["close"] = px
        if extra:
            row.update(extra)
        return
    row = {
        "ts": int(bucket_ts),
        "open": px,
        "high": px,
        "low": px,
        "close": px,
    }
    if extra:
        row.update(extra)
    rows.append(row)


def aggregate_ohlc_rows(rows, interval_sec: int, max_rows: int = 240):
    grouped = []
    cur = None
    for row in rows:
        bucket_ts = candle_bucket(int(row["ts"]), interval_sec)
        if cur is None or cur["ts"] != bucket_ts:
            if cur is not None:
                grouped.append(cur)
            cur = {
                "ts": bucket_ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
            if "ptb" in row:
                cur["ptb"] = row.get("ptb")
        else:
            cur["high"] = max(float(cur["high"]), float(row["high"]))
            cur["low"] = min(float(cur["low"]), float(row["low"]))
            cur["close"] = float(row["close"])
            if "ptb" in row:
                cur["ptb"] = row.get("ptb")
    if cur is not None:
        grouped.append(cur)
    return grouped[-max_rows:]


def sample_chart_once():
    with state_lock:
        up_bid = state.get("up_bid")
        up_ask = state.get("up_ask")
        btc_now = state.get("btc_price_now")
        btc_ptb = state.get("btc_price_to_beat")
        slug = str(state.get("current_slug", "-"))
    up_mid = None
    if is_valid_price(up_bid) and is_valid_price(up_ask):
        up_mid = (float(up_bid) + float(up_ask)) / 2.0
    elif is_valid_price(up_bid):
        up_mid = float(up_bid)
    elif is_valid_price(up_ask):
        up_mid = float(up_ask)
    now_ts = int(time.time())
    bucket_ts = candle_bucket(now_ts, 60)
    with chart_lock:
        if up_mid is not None:
            update_ohlc_row(chart_state["up_1m"], bucket_ts, up_mid, {"slug": slug})
            chart_state["up_1m"] = chart_state["up_1m"][-CHART_MAX_CANDLES_1M:]
        if btc_now is not None:
            extra = {"ptb": float(btc_ptb)} if btc_ptb is not None else {}
            update_ohlc_row(chart_state["btc_1m"], bucket_ts, float(btc_now), extra)
            chart_state["btc_1m"] = chart_state["btc_1m"][-CHART_MAX_CANDLES_1M:]


@contextmanager
def force_ipv4_dns(enabled: bool):
    if not enabled:
        yield
        return
    original_allowed_gai_family = urllib3.util.connection.allowed_gai_family
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
    try:
        yield
    finally:
        urllib3.util.connection.allowed_gai_family = original_allowed_gai_family


def binance_http_get(path: str, params: dict):
    last_error = None
    errors = []
    proxies = {}
    if BINANCE_HTTP_PROXY:
        proxies["http"] = BINANCE_HTTP_PROXY
    if BINANCE_HTTPS_PROXY:
        proxies["https"] = BINANCE_HTTPS_PROXY
    for base_url in BINANCE_API_BASES:
        url = f"{base_url}{path}"
        try:
            with force_ipv4_dns(BINANCE_FORCE_IPV4):
                response = requests.get(
                    url,
                    params=params,
                    timeout=BINANCE_HTTP_TIMEOUT,
                    proxies=proxies or None,
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            errors.append(f"{base_url} -> {exc}")
    if errors:
        log("[CHART] Binance endpoint failures: " + " | ".join(errors))
    raise last_error if last_error is not None else RuntimeError("No Binance API base configured")


def fetch_binance_history(interval: str = "1m", limit: int = 200):
    try:
        r = binance_http_get("/api/v3/klines", {
            "symbol": "BTCUSDT",
            "interval": interval,
            "limit": limit,
        })
        data = r.json()
        if not isinstance(data, list):
            return []
        interval_sec = 300 if interval == "5m" else 60
        rows = []
        for c in data:
            ts_sec = int(c[0]) // 1000
            ts_snap = (ts_sec // interval_sec) * interval_sec
            rows.append({
                "ts": ts_snap,
                "open": float(c[1]),
                "high": float(c[2]),
                "low":  float(c[3]),
                "close": float(c[4]),
            })
        return rows
    except Exception as e:
        log(f"[CHART] Binance fetch error: {e}")
        return []


def init_chart_history():
    with state_lock:
        ptb = state.get("btc_price_to_beat")
    rows_1m = fetch_binance_history(interval="1m", limit=360)
    if rows_1m:
        with chart_lock:
            existing_ts = {r["ts"] for r in chart_state["btc_1m"]}
            new_rows = [r for r in rows_1m if r["ts"] not in existing_ts]
            if ptb is not None:
                for r in new_rows:
                    r["ptb"] = float(ptb)
            merged = new_rows + list(chart_state["btc_1m"])
            merged.sort(key=lambda x: x["ts"])
            chart_state["btc_1m"] = merged[-CHART_MAX_CANDLES_1M:]
        log(f"[CHART] Loaded {len(rows_1m)} historical 1m BTC candles from Binance")
    else:
        log("[CHART] Binance historical fetch returned no data")


def chart_loop():
    while state["running"]:
        try:
            sample_chart_once()
        except Exception:
            pass
        time.sleep(CHART_SAMPLE_SEC)


def make_chart_snapshot(tf: str = "1m"):
    tf_norm = "5m" if str(tf).lower() == "5m" else "1m"
    with chart_lock:
        up_rows = [dict(r) for r in chart_state["up_1m"]]
        btc_rows = [dict(r) for r in chart_state["btc_1m"]]
    if tf_norm == "5m":
        up_rows = aggregate_ohlc_rows(up_rows, 300, max_rows=180)
        btc_rows = aggregate_ohlc_rows(btc_rows, 300, max_rows=180)
    else:
        up_rows = up_rows[-180:]
        btc_rows = btc_rows[-180:]
    btc_closes = [float(r["close"]) for r in btc_rows]
    with state_lock:
        ptb_val = state.get("btc_price_to_beat")
        btc_now_val = state.get("btc_price_now")

    def _ema(prices, period):
        if not prices:
            return []
        k = 2.0 / (period + 1)
        result = [None] * len(prices)
        for i in range(len(prices)):
            if i < period - 1:
                result[i] = None
            elif i == period - 1:
                result[i] = sum(prices[:period]) / period
            else:
                result[i] = prices[i] * k + result[i-1] * (1 - k)
        return result

    def _rsi(prices, period=7):
        if len(prices) < period + 1:
            return [None] * len(prices)
        result = [None] * len(prices)
        gains, losses = [], []
        for i in range(1, period + 1):
            d = prices[i] - prices[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        if avg_l == 0:
            result[period] = 100.0
        else:
            rs = avg_g / avg_l
            result[period] = 100 - (100 / (1 + rs))
        for i in range(period + 1, len(prices)):
            d = prices[i] - prices[i-1]
            avg_g = (avg_g * (period - 1) + max(d, 0)) / period
            avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
            if avg_l == 0:
                result[i] = 100.0
            else:
                rs = avg_g / avg_l
                result[i] = round(100 - (100 / (1 + rs)), 2)
        return result

    def _bb(prices, period=20, std_mult=2.0):
        upper, mid, lower = [None]*len(prices), [None]*len(prices), [None]*len(prices)
        for i in range(period - 1, len(prices)):
            window = prices[i-period+1:i+1]
            m = sum(window) / period
            std = (sum((x - m)**2 for x in window) / period) ** 0.5
            mid[i] = round(m, 2)
            upper[i] = round(m + std_mult * std, 2)
            lower[i] = round(m - std_mult * std, 2)
        return upper, mid, lower

    ema9  = _ema(btc_closes, 9)
    ema21 = _ema(btc_closes, 21)
    rsi7  = _rsi(btc_closes, 7)
    bb_upper, bb_mid, bb_lower = _bb(btc_closes, 20, 2.0)

    ptb_dist = []
    running_ptb = None
    for r in btc_rows:
        p = r.get("ptb")
        if p is not None:
            running_ptb = float(p)
        if running_ptb and running_ptb > 0:
            d = round((float(r["close"]) - running_ptb) / running_ptb * 100, 4)
        else:
            d = None
        ptb_dist.append(d)

    def _last(arr):
        for v in reversed(arr):
            if v is not None:
                return round(v, 4)
        return None

    live = {
        "ema9":  _last(ema9),
        "ema21": _last(ema21),
        "rsi7":  _last(rsi7),
        "bb_upper": _last(bb_upper),
        "bb_mid":   _last(bb_mid),
        "bb_lower": _last(bb_lower),
        "ptb_dist_pct": _last(ptb_dist),
    }

    signal = "NEUTRAL"
    sig_reasons = []
    e9, e21 = live["ema9"], live["ema21"]
    rsi_v = live["rsi7"]
    dist = live["ptb_dist_pct"]
    if e9 and e21:
        if e9 > e21:
            sig_reasons.append("EMA_UP")
        elif e9 < e21:
            sig_reasons.append("EMA_DOWN")
    if rsi_v:
        if rsi_v > 60:
            sig_reasons.append("RSI_BULL")
        elif rsi_v < 40:
            sig_reasons.append("RSI_BEAR")
    if dist is not None:
        if dist > 0.15:
            sig_reasons.append("PTB_FAR_UP")
        elif dist < -0.15:
            sig_reasons.append("PTB_FAR_DOWN")
        elif abs(dist) < 0.05:
            sig_reasons.append("PTB_TOO_CLOSE")

    up_votes   = sum(1 for r in sig_reasons if "UP" in r or "BULL" in r)
    down_votes = sum(1 for r in sig_reasons if "DOWN" in r or "BEAR" in r)
    if "PTB_TOO_CLOSE" in sig_reasons:
        signal = "SKIP"
    elif up_votes >= 2:
        signal = "UP"
    elif down_votes >= 2:
        signal = "DOWN"
    elif up_votes > down_votes:
        signal = "LEAN_UP"
    elif down_votes > up_votes:
        signal = "LEAN_DOWN"

    return {
        "ok": True,
        "tf": tf_norm,
        "polymarket_up": [
            [int(r["ts"]), round(float(r["open"]), 6), round(float(r["high"]), 6), round(float(r["low"]), 6), round(float(r["close"]), 6)]
            for r in up_rows
        ],
        "btc": [
            [
                int(r["ts"]),
                round(float(r["open"]), 4),
                round(float(r["high"]), 4),
                round(float(r["low"]), 4),
                round(float(r["close"]), 4),
                round(float(r["ptb"]), 4) if r.get("ptb") is not None else None,
            ]
            for r in btc_rows
        ],
        "indicators": {
            "ema9":     [round(v, 2) if v is not None else None for v in ema9],
            "ema21":    [round(v, 2) if v is not None else None for v in ema21],
            "rsi7":     rsi7,
            "bb_upper": bb_upper,
            "bb_mid":   bb_mid,
            "bb_lower": bb_lower,
            "ptb_dist": ptb_dist,
        },
        "live": live,
        "signal": signal,
        "signal_reasons": sig_reasons,
        "price_source": "UP midpoint",
        "btc_source": "BTC now / PTB",
    }


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def estimate_annual_vol(window_sec: int = PROB_VOL_WINDOW_SEC) -> float:
    now = int(time.time())
    with _cl_lock:
        rows = [(ts, px) for ts, px in _cl_ring if ts >= (now - window_sec) and px > 0]
    if len(rows) < 8:
        return PROB_DEFAULT_VOL_ANNUAL
    rows.sort(key=lambda x: x[0])
    rets = []
    dts = []
    prev_ts, prev_px = rows[0]
    for ts, px in rows[1:]:
        dt = ts - prev_ts
        if dt <= 0 or prev_px <= 0 or px <= 0:
            prev_ts, prev_px = ts, px
            continue
        rets.append(math.log(px / prev_px))
        dts.append(dt)
        prev_ts, prev_px = ts, px
    if len(rets) < 6:
        return PROB_DEFAULT_VOL_ANNUAL
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / max(1, (len(rets) - 1))
    std_step = math.sqrt(max(var, 0.0))
    avg_dt = sum(dts) / max(1, len(dts))
    if avg_dt <= 0:
        return PROB_DEFAULT_VOL_ANNUAL
    sec_year = 365.0 * 24.0 * 3600.0
    ann = std_step * math.sqrt(sec_year / avg_dt)
    return clamp(ann, 0.01, 5.0)


def compute_probability_snapshot():
    with state_lock:
        up_bid = state.get("up_bid")
        up_ask = state.get("up_ask")
        down_bid = state.get("down_bid")
        down_ask = state.get("down_ask")
        ptb = state.get("btc_price_to_beat")
        now_px = state.get("btc_price_now")
        rem = max(0, int(state.get("interval_end", 0) or 0) - int(time.time()))
        quote_age_ms = max(0, int((time.time() - float(state.get("last_quote_ts", 0))) * 1000))
        slug = str(state.get("current_slug", "-"))

    mids_ok = all(is_valid_price(x) for x in (up_bid, up_ask, down_bid, down_ask))
    if mids_ok:
        ub = float(up_bid)
        ua = float(up_ask)
        db = float(down_bid)
        da = float(down_ask)
        m_up = (ub + ua) / 2.0
        m_down = (db + da) / 2.0
        den = m_up + m_down
        p_market = (m_up / den) if den > 1e-9 else 0.5
        spread_up = max(ua - ub, 0.0)
        spread_down = max(da - db, 0.0)
        spread_avg = (spread_up + spread_down) / 2.0
        # Market quality drops when spread is wide or when UP+DOWN midpoint deviates from 1.
        spread_q = 1.0 - clamp(spread_avg / 0.10, 0.0, 1.0)
        sum_q = 1.0 - clamp(abs((m_up + m_down) - 1.0) / 0.20, 0.0, 1.0)
        market_quality = clamp((0.7 * spread_q) + (0.3 * sum_q), 0.0, 1.0)
    else:
        p_market = 0.5
        spread_avg = 0.5
        market_quality = 0.0

    p_ptb = p_market
    ptb_quality = 0.0
    if ptb is not None and now_px is not None and rem > 0:
        s = float(now_px)
        k = float(ptb)
        if s > 0 and k > 0:
            tau = rem / (365.0 * 24.0 * 3600.0)
            sigma = estimate_annual_vol()
            den = sigma * math.sqrt(max(tau, 1e-12))
            if den < 1e-9:
                p_ptb = 1.0 if s > k else (0.0 if s < k else 0.5)
            else:
                z = (math.log(k / s) + 0.5 * (sigma ** 2) * tau) / den
                p_ptb = 1.0 - norm_cdf(z)
            # PTB signal is meaningful only when the distance from strike is not tiny.
            dist_pct = abs((s - k) / k) * 100.0
            ptb_quality = clamp(dist_pct / 0.15, 0.0, 1.0)

    with chart_lock:
        btc_rows = [dict(r) for r in chart_state.get("btc_1m", [])][-48:]
    closes = [float(r.get("close", 0.0)) for r in btc_rows if float(r.get("close", 0.0)) > 0]

    def _ema_last(prices, period):
        if len(prices) < period:
            return None
        k = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for px in prices[period:]:
            ema = px * k + ema * (1 - k)
        return ema

    def _rsi_last(prices, period=7):
        if len(prices) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, period + 1):
            d = prices[i] - prices[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        for i in range(period + 1, len(prices)):
            d = prices[i] - prices[i - 1]
            avg_g = ((avg_g * (period - 1)) + max(d, 0.0)) / period
            avg_l = ((avg_l * (period - 1)) + max(-d, 0.0)) / period
        if avg_l <= 1e-12:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    def _tick_trend(max_sec=24):
        now_ts = int(time.time())
        with _cl_lock:
            rows = [(int(ts), float(px)) for ts, px in _cl_ring if (now_ts - int(ts)) <= max_sec and float(px) > 0]
        if len(rows) < 2:
            return None
        rows.sort(key=lambda x: x[0])
        first = float(rows[0][1])
        last = float(rows[-1][1])
        if first <= 0:
            return None
        return (last - first) / first

    # Micro model is intentionally weak (small correction only) to avoid overfitting.
    score = 0.0
    total_w = 0.0
    if len(closes) >= 6:
        mom = (closes[-1] - closes[-6]) / max(closes[-6], 1e-9)
        score += 1.0 * clamp(mom / 0.0012, -1.0, 1.0)
        total_w += 1.0
    ema9 = _ema_last(closes, 9)
    ema21 = _ema_last(closes, 21)
    if ema9 is not None and ema21 is not None and ema21 > 0:
        score += 0.9 * clamp((ema9 - ema21) / ema21 / 0.0018, -1.0, 1.0)
        total_w += 0.9
    rsi7 = _rsi_last(closes, 7)
    if rsi7 is not None:
        score += 0.7 * clamp((float(rsi7) - 50.0) / 18.0, -1.0, 1.0)
        total_w += 0.7
    tick_tr = _tick_trend(20)
    if tick_tr is not None:
        score += 0.5 * clamp(tick_tr / 0.0009, -1.0, 1.0)
        total_w += 0.5
    score_norm = (score / total_w) if total_w > 1e-9 else 0.0
    p_micro = clamp(0.5 + (0.10 * score_norm), 0.05, 0.95)
    micro_quality = clamp(total_w / 3.1, 0.0, 1.0)

    # Dynamic blend: market is anchor, PTB and micro are contextual corrections.
    w_m = (0.70 * market_quality) + 0.10
    w_p = 0.55 * ptb_quality
    w_x = 0.20 * micro_quality
    w_total = w_m + w_p + w_x
    if w_total <= 1e-9:
        p_blend = p_market
    else:
        p_blend = (w_m * p_market + w_p * p_ptb + w_x * p_micro) / w_total

    # Keep final output conservative by shrinking toward market-implied probability.
    p_up = clamp((0.80 * p_market) + (0.20 * p_blend), 0.02, 0.98)
    p_down = 1.0 - p_up

    spread_penalty = clamp(spread_avg / 0.10, 0.0, 1.0)
    c_time = clamp(rem / 300.0, 0.0, 1.0)
    c_spread = 1.0 - spread_penalty
    if quote_age_ms <= 1500:
        c_data = 1.0
    elif quote_age_ms <= 5000:
        c_data = 0.6
    else:
        c_data = 0.3

    comp = [p_market]
    if ptb_quality > 0:
        comp.append(p_ptb)
    if micro_quality > 0:
        comp.append(p_micro)
    disagreement = max(comp) - min(comp) if comp else 0.0
    c_agree = 1.0 - clamp(disagreement / 0.20, 0.0, 1.0)
    c_edge = clamp(abs(p_up - 0.5) / 0.12, 0.0, 1.0)
    confidence = clamp(
        (0.35 * c_edge) +
        (0.25 * c_spread) +
        (0.20 * c_data) +
        (0.20 * c_agree),
        0.0, 1.0
    )
    # Lower confidence near very end of interval due to fill/routing noise.
    if rem <= 25:
        confidence *= 0.85

    with state_lock:
        state["prob_up"] = p_up
        state["prob_down"] = p_down
        state["prob_confidence"] = confidence
        state["prob_market"] = p_market
        state["prob_ptb"] = p_ptb
        state["prob_micro"] = p_micro
    if slug and slug != "-":
        with _prob_live_lock:
            _prob_live_by_slug[slug] = {
                "p_up": p_up,
                "confidence": confidence,
                "ts": int(time.time()),
            }
    return {
        "p_up": p_up,
        "p_down": p_down,
        "confidence": confidence,
        "p_market": p_market,
        "p_ptb": p_ptb,
        "p_micro": p_micro,
    }


def lock_open_probability_if_needed(prob: dict = None):
    if prob is None:
        prob = compute_probability_snapshot()
    with state_lock:
        slug = str(state.get("current_slug", "-"))
        already_slug = str(state.get("prob_open_slug", "-"))
    if not slug or slug == "-":
        return
    if slug == already_slug:
        return
    p_up = float(prob.get("p_up", 0.5))
    conf = float(prob.get("confidence", 0.0))
    with state_lock:
        state["prob_open_slug"] = slug
        state["prob_open_up"] = p_up
        state["prob_open_down"] = 1.0 - p_up
        state["prob_open_confidence"] = conf
        state["prob_open_at"] = int(time.time())
    with _prob_live_lock:
        _prob_open_by_slug[slug] = {"p_up": p_up, "confidence": conf, "ts": int(time.time())}
    log(f"PROB LOCK {slug} up={p_up*100:.1f}% down={(1.0-p_up)*100:.1f}% conf={conf*100:.0f}%")


def score_probability_prediction(closed_slug: str, closed_end_ts: int, closed_ptb):
    slug = str(closed_slug or "").strip()
    if not slug or slug == "-":
        return
    if closed_ptb is None:
        return
    with _prob_scored_lock:
        if slug in _prob_scored_slugs:
            return
    with _prob_live_lock:
        pred = _prob_open_by_slug.get(slug) or _prob_live_by_slug.get(slug)
    if not pred:
        return
    final_px = cl_price_near(int(closed_end_ts), max_abs_drift=PROB_SCORE_DRIFT_SEC)
    if final_px is None:
        return
    ptb = float(closed_ptb)
    if final_px == ptb:
        return
    actual = "UP" if final_px > ptb else "DOWN"
    predicted = "UP" if float(pred.get("p_up", 0.5)) >= 0.5 else "DOWN"
    win = predicted == actual
    with _prob_scored_lock:
        if slug in _prob_scored_slugs:
            return
        _prob_scored_slugs.add(slug)
    with state_lock:
        total = int(state.get("prob_win_total", 0)) + 1
        wins = int(state.get("prob_win_wins", 0)) + (1 if win else 0)
        losses = int(state.get("prob_win_losses", 0)) + (0 if win else 1)
        wr = (wins / total) if total > 0 else 0.0
        state["prob_win_total"] = total
        state["prob_win_wins"] = wins
        state["prob_win_losses"] = losses
        state["prob_win_rate"] = wr
        state["prob_last_result"] = (
            f"{slug} pred={predicted} actual={actual} p_up={float(pred.get('p_up', 0.5))*100:.1f}%"
        )
    log(
        f"PROB RESULT {slug} pred={predicted} actual={actual} "
        f"p_up={float(pred.get('p_up', 0.5))*100:.1f}% "
        f"WR={wins}/{total} ({wr*100:.1f}%)"
    )


def cl_price_at(target_ts: int):
    with _cl_lock:
        if not _cl_ring:
            return None
        for ts, px in _cl_ring:
            if ts >= target_ts and (ts - target_ts) <= PTB_MAX_DRIFT_SEC:
                return px
        return None


def cl_price_near(target_ts: int, max_abs_drift: int = PROB_SCORE_DRIFT_SEC):
    with _cl_lock:
        if not _cl_ring:
            return None
        best_px = None
        best_abs = 10**9
        for ts, px in _cl_ring:
            d = abs(int(ts) - int(target_ts))
            if d <= max_abs_drift and d < best_abs:
                best_abs = d
                best_px = px
        return best_px


def refresh_ptb_if_missing():
    with state_lock:
        if state.get("btc_price_to_beat") is not None:
            return
        slug = state.get("current_slug", "")
        market_id = str(state.get("current_market_id", ""))
    st = slug_start_ts(slug)
    if not st:
        return
    if int(time.time()) < st:
        return
    now = int(time.time())
    if PTB_WEB_FALLBACK:
        last_try = int(_ptb_web_last_try.get(slug, 0))
        if (now - last_try) >= PTB_WEB_RETRY_SEC:
            _ptb_web_last_try[slug] = now
            web_ptb = fetch_ptb_from_polymarket(slug, market_id=market_id)
            if web_ptb is not None:
                with state_lock:
                    if state.get("btc_price_to_beat") is None:
                        state["btc_price_to_beat"] = web_ptb
                        log(f"PTB synced from polymarket {fmt_usd(web_ptb)}")
                return
    ptb = cl_price_at(st)
    if ptb is None:
        return
    with state_lock:
        if state.get("btc_price_to_beat") is None:
            state["btc_price_to_beat"] = ptb
            log(f"PTB locked {fmt_usd(ptb)} @ {st} (drift<={PTB_MAX_DRIFT_SEC}s)")


def fetch_ptb_from_polymarket(slug: str, market_id: str = ""):
    if not slug:
        return None
    try:
        url = f"https://polymarket.com/event/{slug}"
        html = requests.get(url, timeout=4, headers={"user-agent": "Mozilla/5.0"}).text
        anchors = []
        if market_id:
            anchors.append(f'"id":"{market_id}"')
        anchors.append(f'"slug":"{slug}"')
        for anchor in anchors:
            i = html.find(anchor)
            if i < 0:
                continue
            if anchor.startswith('"id":"'):
                chunk = html[i:min(len(html), i + 120000)]
            else:
                next_slug = html.find('"slug":"btc-updown-5m-', i + len(anchor))
                end = next_slug if next_slug > i else min(len(html), i + 120000)
                chunk = html[i:end]
            m = re.search(r'"eventMetadata":\{"priceToBeat":([0-9]+(?:\.[0-9]+)?)\}', chunk)
            if m:
                return float(m.group(1))
        return None
    except Exception:
        return None


def resolve_ptb_on_switch(slug: str, market_id: str = ""):
    st = slug_start_ts(slug)
    now_ts = int(time.time())
    if PTB_WEB_FALLBACK and st and now_ts >= st:
        _ptb_web_last_try[slug] = int(time.time())
        web_ptb = fetch_ptb_from_polymarket(slug, market_id=market_id)
        if web_ptb is not None:
            return web_ptb, "polymarket", st
    if st and now_ts >= st:
        cl_ptb = cl_price_at(st)
        if cl_ptb is not None:
            return cl_ptb, "chainlink", st
    return None, "pending", st


def log(msg: str):
    with state_lock:
        state["log"].append(msg)
        if len(state["log"]) > 100:
            state["log"] = state["log"][-100:]
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_trade(msg: str):
    with state_lock:
        state["trade_log"].append(msg)
        if len(state["trade_log"]) > 120:
            state["trade_log"] = state["trade_log"][-120:]
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [TRADE] {msg}", flush=True)


def bump_session_pnl(delta: float, is_dry: bool = None):
    try:
        d = float(delta)
    except Exception:
        return
    with state_lock:
        dry_mode = state.get("dry_run", True) if is_dry is None else bool(is_dry)
        key = "session_pnl_dry" if dry_mode else "session_pnl_real"
        cur = float(state.get(key, 0.0))
        state[key] = round(cur + d, 4)
        active_key = "session_pnl_dry" if bool(state.get("dry_run", True)) else "session_pnl_real"
        state["session_pnl"] = round(float(state.get(active_key, 0.0)), 4)


def is_local_client(ip: str) -> bool:
    s = str(ip or "").lower()
    return s.startswith(LOCAL_ONLY_NETS)


def upsert_position_merge(
    token_id: str,
    side: str,
    add_shares: float,
    add_notional: float,
    position_slug: str = None,
):
    if add_shares <= 0 or add_notional <= 0:
        return
    with state_lock:
        slug = position_slug or state.get("current_slug", "-")
        cur = state["positions"].get(token_id)
        if cur and str(cur.get("side", "")) == side:
            old_sh = float(cur.get("shares", 0.0))
            old_usd = float(cur.get("usd_in", old_sh * float(cur.get("entry", 0.0))))
            new_sh = round(old_sh + add_shares, 6)
            new_usd = round(old_usd + add_notional, 4)
            new_entry = (new_usd / new_sh) if new_sh > 0 else 0.0
            cur["shares"] = new_sh
            cur["usd_in"] = new_usd
            cur["entry"] = new_entry
            cur["opened_at"] = time.time()
            cur["slug"] = slug
        else:
            state["positions"][token_id] = {
                "side": side,
                "shares": round(add_shares, 6),
                "entry": (add_notional / add_shares),
                "usd_in": round(add_notional, 4),
                "opened_at": time.time(),
                "slug": slug,
            }


def merge_buy_fill_once(
    token_id: str,
    side: str,
    add_shares: float,
    add_notional: float,
    had_local_before: bool = False,
    position_slug: str = None,
) -> bool:
    if add_shares <= 0 or add_notional <= 0:
        return False
    should_skip = False
    if not had_local_before:
        with state_lock:
            cur = state["positions"].get(token_id)
            if cur and str(cur.get("side", "")) == side:
                cur_sh = float(cur.get("shares", 0.0))
                cur_usd = float(cur.get("usd_in", cur_sh * float(cur.get("entry", 0.0))))
                # If a fresh BUY opened this token with near-identical size/notional,
                # treat this as already-synced fill (avoid double merge from WS + infer path).
                tol_sh = max(0.02, float(add_shares) * 0.08)
                tol_usd = max(0.02, float(add_notional) * 0.08)
                if abs(cur_sh - float(add_shares)) <= tol_sh and abs(cur_usd - float(add_notional)) <= tol_usd:
                    should_skip = True
    if should_skip:
        return False
    upsert_position_merge(token_id, side, add_shares, add_notional, position_slug=position_slug)
    return True


def infer_binary_side_from_token(token_id: str):
    tok = str(token_id or "")
    if not tok:
        return None
    with state_lock:
        if tok in (str(state.get("up_token") or ""), str(state.get("next_up_token") or ""), str(state.get("prev_up_token") or "")):
            return "up"
        if tok in (str(state.get("down_token") or ""), str(state.get("next_down_token") or ""), str(state.get("prev_down_token") or "")):
            return "down"
        pos = state["positions"].get(tok)
        if pos and str(pos.get("side", "")) in ("up", "down"):
            return str(pos.get("side"))
    return None


def sync_position_from_trade_event(asset_id: str, action_side: str, size, price):
    try:
        act = str(action_side or "").upper()
        if act != BUY:
            return
        tok = str(asset_id or "")
        if not tok:
            return
        shares = float(size)
        px = float(price)
        if shares <= 0 or px <= 0:
            return
        side = infer_binary_side_from_token(tok)
        if side not in ("up", "down"):
            return
        # Guard against double-merge when the same fill was already reconciled
        # by another execution path (e.g. immediate infer_buy_fill path).
        with state_lock:
            cur = state["positions"].get(tok)
            local_sh = float(cur.get("shares", 0.0)) if cur else 0.0
            local_opened_at = float(cur.get("opened_at", 0.0)) if cur else 0.0
        onchain_sh = get_available_shares(tok)
        # Avoid double-count when local BUY infer merged first, while onchain balance is still lagging.
        if (
            cur
            and local_sh > POSITION_DUST_SHARES
            and (time.time() - local_opened_at) <= 6.0
            and onchain_sh <= POSITION_DUST_SHARES
        ):
            return
        tol = 0.02
        if onchain_sh > 0 and local_sh > 0 and abs(local_sh - onchain_sh) <= tol:
            return
        if onchain_sh > 0 and (local_sh + shares) > (onchain_sh + tol):
            return
        add_notional = round(shares * px, 6)
        upsert_position_merge(tok, side, round(shares, 6), add_notional)
    except Exception:
        return


def apply_sell_fill_from_trade_event(asset_id: str, size, price):
    try:
        tok = str(asset_id or "")
        if not tok:
            return
        sell_shares = float(size)
        fill_px = float(price)
        if sell_shares <= 0 or fill_px <= 0:
            return
        side = infer_binary_side_from_token(tok)
        if side not in ("up", "down"):
            return
        # CASHOUT path already computes/logs realized pnl; avoid double counting.
        with _cashout_lock:
            if side in _cashout_inflight:
                return
        with state_lock:
            pos = state["positions"].get(tok)
            if not pos:
                return
            old_sh = float(pos.get("shares", 0.0))
            if old_sh <= POSITION_DUST_SHARES:
                return
            old_entry = float(pos.get("entry", 0.0))
            old_usd = float(pos.get("usd_in", old_sh * old_entry))
            sold = min(max(sell_shares, 0.0), old_sh)
            if sold <= 0:
                return
            pnl = (fill_px - old_entry) * sold
            rem = max(old_sh - sold, 0.0)
            if rem <= POSITION_DUST_SHARES:
                state["positions"].pop(tok, None)
            else:
                new_sh = round(rem, 6)
                new_usd = round(old_usd * (new_sh / max(old_sh, 0.000001)), 4)
                pos["shares"] = new_sh
                pos["usd_in"] = new_usd
            status = "CLOSED" if rem <= POSITION_DUST_SHARES else "PARTIAL"
            rem_txt = f" rem={rem:.4f}" if status == "PARTIAL" else ""
        bump_session_pnl(pnl, is_dry=False)
        log_trade(
            f"SELL {side.upper()} {status} sh={sold:.4f}{rem_txt} "
            f"px={to_cent_display(fill_px)} pnl={pnl:+.4f}"
        )
    except Exception:
        return


def is_dust_position(pos: dict) -> bool:
    try:
        shares = float(pos.get("shares", 0.0))
        entry = float(pos.get("entry", 0.0))
        usd = shares * entry
        return shares <= POSITION_DUST_SHARES or usd <= POSITION_DUST_USD
    except Exception:
        return True


def resolve_position_token(side: str, allow_off_market: bool = False):
    with state_lock:
        cur_tok = state["up_token"] if side == "up" else state["down_token"]
        cur_pos = state["positions"].get(cur_tok) if cur_tok else None
        if cur_pos and not is_dust_position(cur_pos):
            return cur_tok, cur_pos, False
        if cur_pos and is_dust_position(cur_pos):
            state["positions"].pop(cur_tok, None)
            cur_pos = None
        if not allow_off_market:
            return cur_tok, None, False
        candidates = [(tok, p) for tok, p in state["positions"].items() if p.get("side") == side]
        candidates = [(tok, p) for tok, p in candidates if not is_dust_position(p)]
    if not candidates:
        return cur_tok, None, False
    tok, pos = max(candidates, key=lambda x: float(x[1].get("opened_at", 0)))
    return tok, pos, True


def resolve_position_token_for_target(side: str, target_market: str):
    target = str(target_market or "current").lower()
    with state_lock:
        if target == "next":
            tok = state.get("next_up_token") if side == "up" else state.get("next_down_token")
        elif target == "previous":
            tok = state.get("prev_up_token") if side == "up" else state.get("prev_down_token")
        else:
            tok = state.get("up_token") if side == "up" else state.get("down_token")
        pos = state["positions"].get(tok) if tok else None
        if pos and is_dust_position(pos):
            state["positions"].pop(tok, None)
            pos = None
    if pos:
        return tok, pos, False
    if target == "current":
        return resolve_position_token(side, allow_off_market=True)
    return tok, None, False


def parse_clob_ids(raw):
    return raw if isinstance(raw, list) else json.loads(raw)


def is_valid_price(p) -> bool:
    try:
        v = float(p)
        return 0.001 < v < 1.0
    except (ValueError, TypeError):
        return False


def best_book_prices(bids, asks):
    bid_vals = []
    ask_vals = []
    for b in bids or []:
        p = b.get("price") if isinstance(b, dict) else getattr(b, "price", None)
        if is_valid_price(p):
            bid_vals.append(float(p))
    for a in asks or []:
        p = a.get("price") if isinstance(a, dict) else getattr(a, "price", None)
        if is_valid_price(p):
            ask_vals.append(float(p))
    bid = str(max(bid_vals)) if bid_vals else "-"
    ask = str(min(ask_vals)) if ask_vals else "-"
    return bid, ask


def to_cent_display(p) -> str:
    if not is_valid_price(p):
        return "-"
    cents = max(0.0, min(100.0, float(p) * 100.0))
    if CENT_DECIMALS == 0:
        return f"{int(round(cents))}¢"
    val = f"{cents:.{CENT_DECIMALS}f}".rstrip("0").rstrip(".")
    return f"{val}¢"


def norm_usd(v: float) -> float:
    return round(max(v, 0.0), 2)


def norm_size(v: float) -> float:
    return round(max(v, 0.0), 4)


def fak_buy_size_from_usd(usd: float, price: float):
    if price <= 0:
        return 0.0
    usd_d = Decimal(str(norm_usd(usd))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    px_d = Decimal(str(price))
    size = (usd_d / px_d).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    step = Decimal("0.0001")
    for _ in range(400):
        if size <= 0:
            break
        maker = size * px_d
        if maker == maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN):
            return float(size)
        size -= step
    return 0.0


def fak_sell_size(shares: float) -> float:
    s = Decimal(str(max(shares, 0.0))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return float(s)


def parse_limit_price(raw: str) -> float:
    s = str(raw).strip()
    if not s:
        raise ValueError("empty price")
    if s.isdigit():
        cent = int(s)
        if cent < 1 or cent > 99:
            raise ValueError("cent out of range")
        return round(cent / 100.0, 4)
    p = float(s)
    if p <= 0 or p > 1:
        raise ValueError("price out of range")
    return round(p, 4)


def reset_orderbook():
    with state_lock:
        state["last_ws"] = 0


def is_placeholder_market() -> bool:
    with state_lock:
        ub = state["up_bid"]
        ua = state["up_ask"]
        db = state["down_bid"]
        da = state["down_ask"]
    if not all(is_valid_price(x) for x in (ub, ua, db, da)):
        return True
    ubf, uaf, dbf, daf = float(ub), float(ua), float(db), float(da)
    return ubf <= 0.02 and dbf <= 0.02 and uaf >= 0.98 and daf >= 0.98


def refresh_switch_status():
    with state_lock:
        pending = state["switch_pending"]
    if not pending:
        return
    if not is_placeholder_market():
        with state_lock:
            state["switch_pending"] = False
        log("market quotes active")


def set_best(asset_id: str, bid, ask):
    changed = False
    with state_lock:
        if asset_id not in active_tokens:
            return
        bid_s = str(bid) if bid is not None else "-"
        ask_s = str(ask) if ask is not None else "-"
        if asset_id == state["up_token"]:
            side = "up"
        elif asset_id == state["down_token"]:
            side = "down"
        elif asset_id == state["next_up_token"]:
            side = "next_up"
        elif asset_id == state["next_down_token"]:
            side = "next_down"
        elif asset_id == state["prev_up_token"]:
            side = "prev_up"
        elif asset_id == state["prev_down_token"]:
            side = "prev_down"
        else:
            return
        if side in ("next_up", "next_down"):
            if is_valid_price(bid_s) and is_valid_price(ask_s):
                next_quote_cache[asset_id] = (bid_s, ask_s, int(time.time()))
            return
        if side in ("prev_up", "prev_down"):
            if is_valid_price(bid_s):
                state[f"{side}_bid"] = bid_s
            if is_valid_price(ask_s):
                state[f"{side}_ask"] = ask_s
            return
        if side:
            if is_valid_price(bid_s):
                changed = changed or (state[f"{side}_bid"] != bid_s)
                state[f"{side}_bid"] = bid_s
            if is_valid_price(ask_s):
                changed = changed or (state[f"{side}_ask"] != ask_s)
                state[f"{side}_ask"] = ask_s
            if changed:
                state["last_quote_ts"] = int(time.time())
    refresh_switch_status()


def immediate_poll():
    time.sleep(0.1)
    try:
        with state_lock:
            tokens = {"up": state["up_token"], "down": state["down_token"]}
        results = {}

        def fetch(side, tok):
            try:
                ob = client.get_order_book(tok)
                bids = ob.bids if hasattr(ob, "bids") else []
                asks = ob.asks if hasattr(ob, "asks") else []
                results[side] = best_book_prices(bids, asks)
            except Exception:
                pass

        threads = [
            threading.Thread(target=fetch, args=(s, t), daemon=True)
            for s, t in tokens.items() if t
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=3)

        with state_lock:
            for side, (bid_p, ask_p) in results.items():
                if is_valid_price(bid_p):
                    state[f"{side}_bid"] = bid_p
                if is_valid_price(ask_p):
                    state[f"{side}_ask"] = ask_p
            state["last_ws"] = int(time.time())
            state["last_quote_ts"] = int(time.time())
        refresh_switch_status()
        log("prices loaded")
    except Exception:
        pass


def refresh_quotes_once():
    try:
        immediate_poll()
    except Exception:
        pass


def sample_top_prices(token_id: str):
    try:
        ob = client.get_order_book(token_id)
        bids = ob.bids if hasattr(ob, "bids") else []
        asks = ob.asks if hasattr(ob, "asks") else []
        bid_s, ask_s = best_book_prices(bids, asks)
        bid = float(bid_s) if is_valid_price(bid_s) else None
        ask = float(ask_s) if is_valid_price(ask_s) else None
        return bid, ask
    except Exception:
        return None, None


def is_quote_fresh(max_age: float = 1.0) -> bool:
    with state_lock:
        return (time.time() - state["last_quote_ts"]) < max_age


def slug_start_ts(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[-1])
    except Exception:
        return 0


def is_placeholder_pair(bid, ask) -> bool:
    if bid is None or ask is None:
        return True
    return bid <= 0.02 and ask >= 0.98


def is_placeholder_quote_str(bid_s, ask_s) -> bool:
    if not (is_valid_price(bid_s) and is_valid_price(ask_s)):
        return True
    return is_placeholder_pair(float(bid_s), float(ask_s))


def get_cached_quote(token_id: str, max_age: int = 120):
    if not token_id:
        return None, None
    with state_lock:
        hit = next_quote_cache.get(token_id)
    if not hit:
        return None, None
    bid_s, ask_s, ts = hit
    if (int(time.time()) - int(ts)) > max_age:
        return None, None
    if not (is_valid_price(bid_s) and is_valid_price(ask_s)):
        return None, None
    return bid_s, ask_s


def put_cached_quote(token_id: str, bid=None, ask=None):
    if not token_id:
        return
    with state_lock:
        old_bid, old_ask, _ = next_quote_cache.get(token_id, (None, None, 0))
        bid_s = str(bid) if is_valid_price(bid) else old_bid
        ask_s = str(ask) if is_valid_price(ask) else old_ask
        next_quote_cache[token_id] = (bid_s, ask_s, int(time.time()))


def next_market_quotes():
    with state_lock:
        nxt_up = state.get("next_up_token")
        nxt_down = state.get("next_down_token")
        nxt_slug = state.get("next_slug")
        nxt_end = int(state.get("next_interval_end", 0) or 0)
        target = str(state.get("target_market", "current"))
        worker_quotes = dict(state.get("worker_quotes", {}) or {})
    up_bid, up_ask = get_cached_quote(nxt_up, max_age=25) if nxt_up else (None, None)
    down_bid, down_ask = get_cached_quote(nxt_down, max_age=25) if nxt_down else (None, None)
    if (not is_valid_price(up_bid) or not is_valid_price(up_ask)) and nxt_up:
        row = worker_quotes.get(str(nxt_up), {})
        up_bid = row.get("best_bid", up_bid)
        up_ask = row.get("best_ask", up_ask)
    if (not is_valid_price(down_bid) or not is_valid_price(down_ask)) and nxt_down:
        row = worker_quotes.get(str(nxt_down), {})
        down_bid = row.get("best_bid", down_bid)
        down_ask = row.get("best_ask", down_ask)
    ready = bool(
        nxt_up and nxt_down
        and is_valid_price(up_bid) and is_valid_price(up_ask)
        and is_valid_price(down_bid) and is_valid_price(down_ask)
    )
    return {
        "target": target,
        "next_slug": nxt_slug or "-",
        "next_interval_end": nxt_end,
        "next_ready": ready,
        "next_up_bid": up_bid or "-",
        "next_up_ask": up_ask or "-",
        "next_down_bid": down_bid or "-",
        "next_down_ask": down_ask or "-",
    }


def smart_fetch_tokens(force_switch: bool = False) -> bool:
    global last_market_switch, active_tokens, _ws_reconnect_delay
    now = int(time.time())
    candidates = []
    for offset in (0, 300, 600):
        t = (now // 300) * 300 + offset
        slug = f"btc-updown-5m-{t}"
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5).json()
            if not r or r[0].get("closed"):
                continue
            m = r[0]["markets"][0]
            if not m.get("acceptingOrders"):
                continue
            ids = parse_clob_ids(m["clobTokenIds"])
            end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            api_end_ts = int(end_dt.timestamp())
            slug_start = slug_start_ts(slug)
            derived_end_ts = slug_start + 300 if slug_start else api_end_ts
            end_ts = min(api_end_ts, derived_end_ts) if api_end_ts > 0 else derived_end_ts
            rem = max(0, end_ts - now)
            up_bid, up_ask = sample_top_prices(ids[0])
            down_bid, down_ask = sample_top_prices(ids[1])
            up_placeholder = is_placeholder_pair(up_bid, up_ask)
            down_placeholder = is_placeholder_pair(down_bid, down_ask)
            spread_penalty = 0.0
            if up_bid is not None and up_ask is not None:
                spread_penalty += max(up_ask - up_bid, 0)
            if down_bid is not None and down_ask is not None:
                spread_penalty += max(down_ask - down_bid, 0)
            score = 0
            if not up_placeholder:
                score += 60
            if not down_placeholder:
                score += 60
            if up_placeholder and down_placeholder:
                score -= 80
            score -= spread_penalty * 100
            score -= rem / 10.0
            if rem > 390:
                score -= 120
            candidates.append({
                "slug": slug, "ids": ids, "end_ts": end_ts, "rem": rem, "score": score,
                "up_bid": up_bid, "up_ask": up_ask, "down_bid": down_bid, "down_ask": down_ask,
                "market_id": str(m.get("id", "")),
            })
        except Exception:
            continue
    if not candidates:
        log("No active market found")
        return False

    with state_lock:
        cur_up = state.get("up_token")
        cur_slug = state.get("current_slug")
        has_open_pos = any(not is_dust_position(p) for p in state["positions"].values())
    current_candidate = None
    for c in candidates:
        if (cur_up and c["ids"][0] == cur_up) or (cur_slug and c["slug"] == cur_slug):
            current_candidate = c
            break

    if (
        current_candidate
        and has_open_pos
        and current_candidate["rem"] > SWITCH_MIN_REMAINING_SEC
        and not force_switch
    ):
        best = current_candidate
    else:
        near_candidates = [c for c in candidates if c["rem"] <= 390]
        pool = near_candidates if near_candidates else candidates
        best = max(pool, key=lambda c: c["score"])
    ids = best["ids"]
    with state_lock:
        old_up = state["up_token"]
        old_slug = state.get("current_slug")
        old_end = int(state.get("interval_end", 0) or 0)
        old_up_bid = state.get("up_bid", "-")
        old_up_ask = state.get("up_ask", "-")
        old_down_bid = state.get("down_bid", "-")
        old_down_ask = state.get("down_ask", "-")
        old_ptb = state.get("btc_price_to_beat")
        if old_up and state["down_token"] and old_slug:
            state["prev_up_token"] = old_up
            state["prev_down_token"] = state["down_token"]
            state["prev_slug"] = old_slug
            state["prev_interval_end"] = old_end
            state["prev_up_bid"] = old_up_bid
            state["prev_up_ask"] = old_up_ask
            state["prev_down_bid"] = old_down_bid
            state["prev_down_ask"] = old_down_ask
        state["up_token"] = ids[0]
        state["down_token"] = ids[1]
        state["interval_end"] = best["end_ts"]
        state["current_slug"] = best["slug"]
        state["current_market_id"] = str(best.get("market_id", ""))
        prev_up = state.get("prev_up_token")
        prev_down = state.get("prev_down_token")
    active_tokens = {t for t in (ids[0], ids[1], prev_up, prev_down) if t}
    worker_watch_tokens([ids[0], ids[1], prev_up, prev_down])
    if old_up != ids[0]:
        if old_slug and old_end > 0 and old_ptb is not None:
            score_probability_prediction(old_slug, old_end, old_ptb)
        reset_orderbook()
        up_cache_bid, up_cache_ask = get_cached_quote(ids[0], max_age=150)
        down_cache_bid, down_cache_ask = get_cached_quote(ids[1], max_age=150)
        with state_lock:
            if is_valid_price(up_cache_bid) and is_valid_price(up_cache_ask):
                state["up_bid"] = up_cache_bid
                state["up_ask"] = up_cache_ask
            elif is_valid_price(best["up_bid"]) and is_valid_price(best["up_ask"]):
                state["up_bid"] = str(best["up_bid"])
                state["up_ask"] = str(best["up_ask"])
            if is_valid_price(down_cache_bid) and is_valid_price(down_cache_ask):
                state["down_bid"] = down_cache_bid
                state["down_ask"] = down_cache_ask
            elif is_valid_price(best["down_bid"]) and is_valid_price(best["down_ask"]):
                state["down_bid"] = str(best["down_bid"])
                state["down_ask"] = str(best["down_ask"])
            next_quote_cache.pop(ids[0], None)
            next_quote_cache.pop(ids[1], None)
        with state_lock:
            state["last_switch_ts"] = now
            state["switch_pending"] = True
            state["next_up_token"] = None
            state["next_down_token"] = None
            state["next_slug"] = None
            state["next_interval_end"] = 0
            state["prob_open_slug"] = "-"
            state["prob_open_up"] = None
            state["prob_open_down"] = None
            state["prob_open_confidence"] = None
            state["prob_open_at"] = 0
        log(f"SWITCH -> {best['slug']} (rem={best['rem']}s)")
        ptb, ptb_src, interval_start = resolve_ptb_on_switch(best["slug"], best.get("market_id", ""))
        with state_lock:
            state["btc_price_to_beat"] = ptb
        if ptb is not None:
            log(f"PTB set {fmt_usd(ptb)} @ {interval_start} [{ptb_src}]")
        else:
            log(f"PTB pending (need Chainlink sample within ±{PTB_MAX_DRIFT_SEC}s of {interval_start})")
        last_market_switch = now
        if ws_app:
            try:
                with _ws_lock:
                    _ws_reconnect_delay = 0.2
                ws_app.close()
                log("WS restart for market switch")
            except Exception:
                try:
                    ws_subscribe(ws_app)
                    log("WS resubscribe new market")
                except Exception:
                    pass
        threading.Thread(target=immediate_poll, daemon=True).start()
        threading.Thread(target=prefetch_next_market, daemon=True).start()
    else:
        with state_lock:
            if best["up_bid"] is not None:
                state["up_bid"] = str(best["up_bid"])
            if best["up_ask"] is not None:
                state["up_ask"] = str(best["up_ask"])
            if best["down_bid"] is not None:
                state["down_bid"] = str(best["down_bid"])
            if best["down_ask"] is not None:
                state["down_ask"] = str(best["down_ask"])
    return True


def prefetch_next_market():
    global active_tokens
    now = int(time.time())
    next_t = ((now // 300) + 1) * 300
    slug = f"btc-updown-5m-{next_t}"
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            timeout=5
        ).json()
        if not r or r[0].get("closed"):
            return
        m = r[0]["markets"][0]
        if not m.get("acceptingOrders"):
            return
        ids = parse_clob_ids(m["clobTokenIds"])
        if len(ids) < 2:
            return
        with state_lock:
            cur_up = state["up_token"]
            if ids[0] == cur_up:
                return
            state["next_up_token"] = ids[0]
            state["next_down_token"] = ids[1]
            state["next_slug"] = slug
            state["next_interval_end"] = next_t + 300
        active_tokens = active_tokens | {ids[0], ids[1]}
        worker_watch_tokens([ids[0], ids[1]])
        if ws_app:
            try:
                ws_subscribe(ws_app)
                log(f"Pre-subscribed next market: {slug}")
            except Exception:
                pass
    except Exception:
        pass


def market_watcher():
    _prefetched_for = 0
    while state["running"]:
        with state_lock:
            rem = state["interval_end"] - int(time.time())
            cur_end = state["interval_end"]
            has_open_pos = any(not is_dust_position(p) for p in state["positions"].values())

        if rem <= NEXT_PREFETCH_SEC and cur_end != _prefetched_for:
            _prefetched_for = cur_end
            threading.Thread(target=prefetch_next_market, daemon=True).start()

        force_switch = rem <= SWITCH_MIN_REMAINING_SEC
        stale_refresh = (int(time.time()) - last_market_switch) > 600 and not has_open_pos
        if force_switch or stale_refresh:
            smart_fetch_tokens(force_switch=force_switch)

        time.sleep(10)


def ws_subscribe(ws):
    with state_lock:
        up = state["up_token"]
        down = state["down_token"]
        next_up = state["next_up_token"]
        next_down = state["next_down_token"]
        prev_up = state.get("prev_up_token")
        prev_down = state.get("prev_down_token")
    ids = [t for t in [up, down, next_up, next_down, prev_up, prev_down] if t]
    ids = list(dict.fromkeys(ids))
    if ids:
        ws.send(json.dumps({
            "type": "market",
            "assets_ids": ids,
            "custom_feature_enabled": True
        }))


def start_rtds():
    global _rtds_connecting
    with _rtds_lock:
        if _rtds_connecting:
            return
        _rtds_connecting = True

    def on_open(ws):
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "{\"symbol\":\"btc/usd\"}",
            }],
        }))
        log("RTDS connected")

    def on_message(_ws, msg: str):
        try:
            d = json.loads(msg)
            payload = d.get("payload", {})
            latest = None
            if d.get("topic") == "crypto_prices_chainlink" and payload.get("symbol") == "btc/usd":
                val = payload.get("value")
                if val is None:
                    return
                price = float(val)
                raw_ts = int(payload.get("timestamp", 0))
                ts = raw_ts // 1000 if raw_ts > 2_000_000_000 else raw_ts
                if ts <= 0:
                    ts = int(time.time())
                latest = (ts, price)
            elif isinstance(payload.get("data"), list):
                rows = payload.get("data") or []
                if not rows:
                    return
                with _cl_lock:
                    for row in rows:
                        try:
                            price = float(row.get("value"))
                            raw_ts = int(row.get("timestamp", 0))
                            ts = raw_ts // 1000 if raw_ts > 2_000_000_000 else raw_ts
                            if ts <= 0:
                                continue
                            _cl_ring.append((ts, price))
                            latest = (ts, price)
                        except Exception:
                            continue
                    if latest is not None:
                        cutoff = latest[0] - _CL_RING_MAX
                        while _cl_ring and _cl_ring[0][0] < cutoff:
                            _cl_ring.pop(0)
                if latest is not None:
                    with state_lock:
                        state["btc_price_now"] = latest[1]
                    refresh_ptb_if_missing()
                return
            else:
                return

            with _cl_lock:
                _cl_ring.append((ts, price))
                cutoff = ts - _CL_RING_MAX
                while _cl_ring and _cl_ring[0][0] < cutoff:
                    _cl_ring.pop(0)
            with state_lock:
                state["btc_price_now"] = price
            refresh_ptb_if_missing()
        except Exception:
            pass

    def on_close(_ws, _code, _msg):
        global _rtds_connecting
        with _rtds_lock:
            _rtds_connecting = False
        log("RTDS reconnecting...")
        if state["running"]:
            threading.Thread(target=lambda: (time.sleep(5), start_rtds()), daemon=True).start()

    def on_error(_ws, err):
        log(f"RTDS error: {str(err)[:120]}")

    try:
        rtds = websocket.WebSocketApp(
            "wss://ws-live-data.polymarket.com",
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
        )
        rtds.run_forever(ping_interval=20, ping_timeout=10)
    finally:
        with _rtds_lock:
            _rtds_connecting = False


def start_ws():
    global ws_app, _ws_connecting, _ws_reconnect_delay

    with _ws_lock:
        if _ws_connecting:
            return
        _ws_connecting = True

    try:
        def on_open(ws):
            with state_lock:
                state["ws_ok"] = True
            ws_subscribe(ws)
            log("WS connected")

        def on_message(_ws, msg: str):
            try:
                d = json.loads(msg)
                et = d.get("event_type")
                with state_lock:
                    state["last_ws"] = int(time.time())
                if et == "best_bid_ask":
                    set_best(d["asset_id"], d.get("best_bid"), d.get("best_ask"))
                elif et == "price_change" and "price_changes" in d:
                    for pc in d["price_changes"]:
                        set_best(
                            pc["asset_id"],
                            pc.get("best_bid", pc.get("best_bid_price")),
                            pc.get("best_ask", pc.get("best_ask_price")),
                        )
                elif et == "book":
                    bids = d.get("bids", [])
                    asks = d.get("asks", [])
                    bid_p, ask_p = best_book_prices(bids, asks)
                    set_best(
                        d.get("asset_id", ""),
                        bid_p if is_valid_price(bid_p) else None,
                        ask_p if is_valid_price(ask_p) else None,
                    )
            except Exception:
                pass

        def on_close(_ws, _code, _msg):
            global _ws_connecting, _ws_reconnect_delay
            with state_lock:
                state["ws_ok"] = False
            with _ws_lock:
                _ws_connecting = False
                delay = float(_ws_reconnect_delay)
                _ws_reconnect_delay = 3.0
            if state["running"]:
                log("WS reconnecting...")
                threading.Thread(
                    target=lambda: (time.sleep(max(0.1, delay)), start_ws()),
                    daemon=True
                ).start()

        def on_error(_ws, err):
            log(f"WS error: {err}")

        ws_app = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
        )
        ws_app.run_forever(ping_interval=20, ping_timeout=10)
    finally:
        with _ws_lock:
            _ws_connecting = False


def remember_user_ws_key(key: str) -> bool:
    if not key:
        return False
    with _userws_seen_lock:
        if key in _userws_seen_keyset:
            return False
        _userws_seen_keyset.add(key)
        _userws_seen_keys.append(key)
        if len(_userws_seen_keys) > _USERWS_SEEN_MAX:
            old = _userws_seen_keys.pop(0)
            _userws_seen_keyset.discard(old)
    return True


def remember_user_ws_fill_trade_id(trade_id: str) -> bool:
    tid = str(trade_id or "").strip()
    if not tid:
        return False
    with _userws_trade_fill_lock:
        if tid in _userws_trade_fill_idset:
            return False
        _userws_trade_fill_idset.add(tid)
        _userws_trade_fill_ids.append(tid)
        if len(_userws_trade_fill_ids) > _USERWS_TRADE_FILL_MAX:
            old = _userws_trade_fill_ids.pop(0)
            _userws_trade_fill_idset.discard(old)
    return True


# PATCH 5: fungsi baru untuk sync position segera setelah USER WS fill
def _reconcile_after_fill(asset_id: str, action_side: str = ""):
    """Segera sync position setelah USER WS konfirmasi fill.

    For BUY fills, do not clear local position on transient zero balance reads.
    SELL-side depletion is still reconciled immediately.
    """
    time.sleep(0.5)
    try:
        act = str(action_side or "").upper()
        with state_lock:
            up_tok = state.get("up_token")
            down_tok = state.get("down_token")
            up_ask = state.get("up_ask")
            down_ask = state.get("down_ask")
        if asset_id == up_tok:
            side, ask_s = "up", up_ask
        elif asset_id == down_tok:
            side, ask_s = "down", down_ask
        else:
            return
        avail = round(get_available_shares(asset_id), 4)

        # SELL-side depletion can be reconciled immediately.
        if avail <= POSITION_DUST_SHARES:
            if act == BUY:
                # BUY fills may race with delayed balance propagation.
                # Skip destructive clear and let normal reconciler/user trade sync handle it.
                return
            with state_lock:
                had_pos = asset_id in state["positions"]
                if had_pos:
                    state["positions"].pop(asset_id, None)
            if had_pos:
                log(f"Position closed via USER WS fill: {side.upper()} shares depleted")
            return

        # BUY side: kalau ada shares tapi belum ada local position
        with state_lock:
            has_local = asset_id in state["positions"]
        if not has_local:
            entry = estimate_entry_from_trades(asset_id) or (
                float(ask_s) if is_valid_price(ask_s) else 0.5
            )
            with state_lock:
                state["positions"][asset_id] = {
                    "side": side,
                    "shares": avail,
                    "entry": entry,
                    "usd_in": round(avail * entry, 4),
                    "opened_at": time.time(),
                    "slug": state.get("current_slug", "-"),
                }
            log(f"Position synced via USER WS: {side.upper()} {avail:.4f}sh @ {to_cent_display(entry)}")
    except Exception:
        pass


def start_user_ws():
    global user_ws_app, _userws_connecting, _userws_reconnect_delay
    if not USER_WS_ENABLED:
        return

    with _userws_lock:
        if _userws_connecting:
            return
        _userws_connecting = True

    try:
        creds = getattr(client, "creds", None)
        if not creds:
            log("USER WS skipped: API creds not available")
            return

        def on_open(ws):
            try:
                ws.send(json.dumps({
                    "type": "user",
                    "auth": {
                        "apiKey": creds.api_key,
                        "secret": creds.api_secret,
                        "passphrase": creds.api_passphrase,
                    },
                }))
                log("USER WS connected")
            except Exception as e:
                log(f"USER WS subscribe error: {str(e)[:120]}")

        def on_message(_ws, msg: str):
            if msg == "PONG":
                return
            try:
                d = json.loads(msg)
                if not isinstance(d, dict):
                    return
                et = str(d.get("event_type", "")).lower()
                if et == "trade":
                    tid = str(d.get("id", "")).strip()
                    status = str(d.get("status", "")).upper()
                    side = str(d.get("side", "")).upper() or "?"
                    size = str(d.get("size", "?"))
                    px_raw = d.get("price")
                    try:
                        px_txt = to_cent_display(float(px_raw))
                    except Exception:
                        px_txt = str(px_raw if px_raw is not None else "?")
                    tok = str(d.get("asset_id", ""))[-8:]
                    k = f"trade:{tid}:{status}:{side}:{size}:{px_txt}"
                    if remember_user_ws_key(k):
                        log_trade(
                            f"USER {status or 'TRADE'} {side} sh={size} px={px_txt} tok=*{tok}"
                        )
                        if status in ("MATCHED", "FILLED", "PARTIAL") and remember_user_ws_fill_trade_id(tid):
                            # BUY: sync position quickly from exact trade payload.
                            sync_position_from_trade_event(
                                d.get("asset_id", ""),
                                side,
                                d.get("size", 0),
                                d.get("price", 0),
                            )
                            # SELL: realize pnl immediately for limit-sell fills.
                            if side == SELL:
                                apply_sell_fill_from_trade_event(
                                    d.get("asset_id", ""),
                                    d.get("size", 0),
                                    d.get("price", 0),
                                )
                        # Keep reconcile path for SELL/depletion and any missed states.
                        if status in ("MATCHED", "FILLED", "PARTIAL"):
                            threading.Thread(
                                target=_reconcile_after_fill,
                                args=(str(d.get("asset_id", "")), side),
                                daemon=True,
                            ).start()
                elif et == "order":
                    oid = str(d.get("id", "")).strip()
                    otype = str(d.get("type", "")).upper()
                    status = str(d.get("status", "")).upper()
                    k = f"order:{oid}:{otype}:{status}"
                    if remember_user_ws_key(k):
                        log(f"USER ORDER {otype or '?'} [{oid[:10]}] {status}".strip())
                    if oid and otype == "CANCELLATION":
                        with state_lock:
                            state["open_orders_remote"] = [
                                o for o in state.get("open_orders_remote", [])
                                if str((o or {}).get("id", "")) != oid
                            ]
                    if oid and status in ("MATCHED", "FILLED"):
                        with state_lock:
                            state["open_orders_remote"] = [
                                    o for o in state.get("open_orders_remote", [])
                                    if str((o or {}).get("id", "")) != oid
                            ]
                        order_side = str(d.get("side", "")).upper()
                        threading.Thread(
                            target=_reconcile_after_fill,
                            args=(str(d.get("asset_id", "")), order_side),
                            daemon=True,
                         ).start()
            except Exception:
                pass

        def on_close(_ws, _code, _msg):
            global _userws_connecting, _userws_reconnect_delay
            with _userws_lock:
                _userws_connecting = False
                delay = float(_userws_reconnect_delay)
                _userws_reconnect_delay = 3.0
            if state["running"] and USER_WS_ENABLED:
                log("USER WS reconnecting...")
                threading.Thread(
                    target=lambda: (time.sleep(max(0.1, delay)), start_user_ws()),
                    daemon=True,
                ).start()

        def on_error(_ws, err):
            log(f"USER WS error: {str(err)[:120]}")

        user_ws_app = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/user",
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
        )
        user_ws_app.run_forever(ping_interval=20, ping_timeout=10)
    finally:
        with _userws_lock:
            _userws_connecting = False


def poll_loop():
    while state["running"]:
        with state_lock:
            quote_stale = int(time.time()) - state["last_quote_ts"] > 2
            tokens = {"up": state["up_token"], "down": state["down_token"]}
            switch_pending = state["switch_pending"]
            switch_age = int(time.time()) - state["last_switch_ts"]
            has_position = bool(state["positions"])
            ws_silent = int(time.time()) - state["last_quote_ts"] > 300
            ws_was_ok = state["ws_ok"]

        if ws_silent and ws_was_ok and not switch_pending:
            log("WS silent >5min, force reconnect")
            with state_lock:
                state["ws_ok"] = False
            with _ws_lock:
                already = _ws_connecting
            if not already:
                threading.Thread(
                    target=lambda: (time.sleep(1), start_ws()),
                    daemon=True
                ).start()

        need_poll = quote_stale or switch_pending
        if need_poll and tokens["up"]:
            try:
                for side, tok in tokens.items():
                    if not tok:
                        continue
                    ob = client.get_order_book(tok)
                    bids = ob.bids if hasattr(ob, "bids") else []
                    asks = ob.asks if hasattr(ob, "asks") else []
                    bid_p, ask_p = best_book_prices(bids, asks)
                    with state_lock:
                        if switch_pending and switch_age < 25:
                            cur_bid = state[f"{side}_bid"]
                            cur_ask = state[f"{side}_ask"]
                            cur_good = (
                                is_valid_price(cur_bid)
                                and is_valid_price(cur_ask)
                                and not is_placeholder_quote_str(cur_bid, cur_ask)
                            )
                            new_placeholder = is_placeholder_quote_str(bid_p, ask_p)
                            if cur_good and new_placeholder:
                                continue
                        if is_valid_price(bid_p):
                            state[f"{side}_bid"] = bid_p
                        if is_valid_price(ask_p):
                            state[f"{side}_ask"] = ask_p
                        state["last_ws"] = int(time.time())
                        state["last_quote_ts"] = int(time.time())
                refresh_switch_status()
                if switch_pending and switch_age > 20 and is_placeholder_market():
                    if ws_app:
                        try:
                            ws_subscribe(ws_app)
                            log("resubscribe due to placeholder quotes")
                        except Exception:
                            pass
                    try:
                        smart_fetch_tokens()
                        log("reselect market due to persistent placeholder quotes")
                    except Exception:
                        pass
            except Exception:
                pass

        # PATCH 2: sleep 0.6s untuk has_position — cegah bid/ask lompat-lompat
        if switch_pending:
            time.sleep(0.3)
        elif has_position:
            time.sleep(0.6)
        else:
            time.sleep(1.0)


def fetch_balance():
    while state["running"]:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=SIG_TYPE)
            bal = client.get_balance_allowance(params).get("balance", "0")
            with state_lock:
                state["balance"] = f"${float(bal) / 1_000_000:.2f}"
        except Exception:
            pass
        time.sleep(15)


# PATCH 3: sync_open_orders dengan filter status aktif dan indentasi yang benar
def sync_open_orders():
    while state["running"]:
        try:
            with state_lock:
                dry = state["dry_run"]
            if not dry:
                orders = client.get_orders()
                if isinstance(orders, list):
                    active = [
                        o for o in orders
                        if isinstance(o, dict) and any(
                            tok in str(o.get("status", "")).upper()
                            for tok in ("LIVE","OPEN","ACTIVE","UNMATCH","PARTIAL","PLACED","PENDING")
                        )
                    ]
                    with state_lock:
                        state["open_orders_remote"] = active
        except Exception:
            pass
        time.sleep(8)


def reconcile_positions():
    while state["running"]:
        try:
            with state_lock:
                dry = state["dry_run"]
                last_action = float(state.get("last_real_action_ts", 0.0))
            if dry:
                time.sleep(2)
                continue
            now_ts = time.time()
            in_grace = (now_ts - last_action) < POSITION_SYNC_GRACE
            with state_lock:
                up_tok = state.get("up_token")
                down_tok = state.get("down_token")
                next_up_tok = state.get("next_up_token")
                next_down_tok = state.get("next_down_token")
                prev_up_tok = state.get("prev_up_token")
                prev_down_tok = state.get("prev_down_token")
                up_ask = state.get("up_ask")
                down_ask = state.get("down_ask")
                next_up_ask = state.get("next_up_ask")
                next_down_ask = state.get("next_down_ask")
                prev_up_ask = state.get("prev_up_ask")
                prev_down_ask = state.get("prev_down_ask")
                current_slug = state.get("current_slug", "-")
                next_slug = state.get("next_slug") or "-"
                prev_slug = state.get("prev_slug") or "-"
            tracked = [
                (up_tok, "up", up_ask, current_slug),
                (down_tok, "down", down_ask, current_slug),
                (next_up_tok, "up", next_up_ask, next_slug),
                (next_down_tok, "down", next_down_ask, next_slug),
                (prev_up_tok, "up", prev_up_ask, prev_slug),
                (prev_down_tok, "down", prev_down_ask, prev_slug),
            ]
            seen_tokens = set()
            for tok, side, ask_s, pos_slug in tracked:
                if not tok:
                    continue
                if tok in seen_tokens:
                    continue
                seen_tokens.add(tok)
                if in_grace:
                    continue
                avail = round(get_available_shares(tok), 4)
                if avail <= 0.0001:
                    continue
                ask_v = float(ask_s) if is_valid_price(ask_s) else 0.0
                if avail <= POSITION_DUST_SHARES or (avail * max(ask_v, 0.0)) <= POSITION_DUST_USD:
                    with state_lock:
                        state["positions"].pop(tok, None)
                    continue
                with state_lock:
                    has_local = tok in state["positions"]
                if not has_local:
                    entry = estimate_entry_from_trades(tok) or (
                        float(ask_s) if is_valid_price(ask_s) else 0.5
                    )
                    with state_lock:
                        state["positions"][tok] = {
                            "side": side,
                            "shares": avail,
                            "entry": entry,
                            "usd_in": round(avail * entry, 4),
                            "opened_at": time.time(),
                            "slug": pos_slug,
                        }
                    log(f"Recovered {side.upper()} position shares={avail:.4f} entry={entry:.4f}")
            with state_lock:
                positions = list(state["positions"].items())
            for tok, pos in positions:
                avail = round(get_available_shares(tok), 4)
                opened_at = float(pos.get("opened_at", 0.0))
                entry = float(pos.get("entry", 0.0))
                if avail <= POSITION_DUST_SHARES or (avail * max(entry, 0.0)) <= POSITION_DUST_USD:
                    if in_grace or (opened_at and (now_ts - opened_at) < POSITION_SYNC_GRACE):
                        continue
                    with state_lock:
                        state["positions"].pop(tok, None)
                    continue
                if avail <= 0.0001:
                    if in_grace or (opened_at and (now_ts - opened_at) < POSITION_SYNC_GRACE):
                        continue
                    with state_lock:
                        state["positions"].pop(tok, None)
                elif abs(avail - float(pos.get("shares", 0))) > 0.0002:
                    if in_grace:
                        continue
                    with state_lock:
                        if tok in state["positions"]:
                            state["positions"][tok]["shares"] = avail
                            state["positions"][tok]["usd_in"] = round(
                                avail * float(state["positions"][tok]["entry"]), 4
                            )
        except Exception:
            pass
        time.sleep(2)


def estimate_entry_from_trades(token_id: str):
    try:
        trades = client.get_trades()
        if not isinstance(trades, list):
            return None
        best = None
        best_ts = -1
        for t in trades:
            if str(t.get("asset_id", "")) != str(token_id):
                continue
            if str(t.get("side", "")).upper() != BUY:
                continue
            try:
                px = float(t.get("price", 0))
                ts = int(float(t.get("match_time", 0)))
            except Exception:
                continue
            if px <= 0:
                continue
            if ts > best_ts:
                best_ts = ts
                best = px
        return best
    except Exception:
        return None


def try_gtc_fallback_buy(token_id: str, usd: float, ask_v: float):
    if GTC_FALLBACK_TIMEOUT <= 0:
        return None, 0.0, None
    try:
        gtc_price = min(round(ask_v * 1.02, 4), 0.99)
        size = round(usd / max(gtc_price, 0.01), 4)
        oa = OrderArgs(token_id=token_id, price=gtc_price, size=size, side=BUY)
        signed = client.create_order(oa)
        resp = client.post_order(signed, OrderType.GTC)
        oid = str(resp.get("id", ""))
        if not oid:
            return None, 0.0, None
        log(f"GTC fallback placed {size} @ {gtc_price} [{oid[:10]}]")
        deadline = time.time() + GTC_FALLBACK_TIMEOUT
        while time.time() < deadline:
            time.sleep(2)
            try:
                o = client.get_order(oid)
            except Exception:
                continue
            matched = 0.0
            for k in ("size_matched", "matched_amount", "filled_size"):
                v = o.get(k) if isinstance(o, dict) else None
                try:
                    matched = float(v)
                    if matched > 0:
                        break
                except Exception:
                    continue
            if matched > 0:
                return gtc_price, matched, oid
        return None, 0.0, oid
    except Exception:
        return None, 0.0, None


def market_buy(side: str, usd: float, pending_key: str = ""):
    keep_pending = False
    with state_lock:
        tok = state["up_token"] if side == "up" else state["down_token"]
        ask = state["up_ask"] if side == "up" else state["down_ask"]
        bid = state["up_bid"] if side == "up" else state["down_bid"]
        dry = state["dry_run"]
        last_ws = state["last_ws"]
        last_action = state["last_real_action_ts"]
        had_local_before = tok in state["positions"] if tok else False
    if not tok:
        log("No token loaded")
        clear_buy_pending(pending_key)
        return
    if dry:
        entry = float(ask) if is_valid_price(ask) else 0.5
        shares = round(usd / max(entry, 0.01), 4)
        upsert_position_merge(tok, side, shares, usd)
        log(f"[DRY] MKT {side.upper()} ${usd} @ {entry}")
        log_trade(f"BUY {side.upper()} DRY fill@{to_cent_display(entry)} sh={shares:.4f} usd=${usd:.2f}")
        clear_buy_pending(pending_key)
        return
    try:
        usd = norm_usd(usd)
        if usd < MIN_ORDER_USD:
            log(f"Blocked entry: amount too small (<${MIN_ORDER_USD})")
            log_trade(f"BUY {side.upper()} blocked: amount < ${MIN_ORDER_USD:.2f}")
            clear_buy_pending(pending_key)
            return
        now = time.time()
        if not dry and (now - last_action) < 1.2:
            log("Blocked entry: action cooldown")
            log_trade(f"BUY {side.upper()} blocked: cooldown")
            clear_buy_pending(pending_key)
            return
        started_at = int(time.time())
        pre_shares = get_available_shares(tok)

        if not is_quote_fresh(1.0):
            fresh_bid, fresh_ask = sample_top_prices(tok)
            if fresh_ask is not None:
                ask = str(fresh_ask)
            if fresh_bid is not None:
                bid = str(fresh_bid)
            with state_lock:
                if is_valid_price(ask):
                    state[f"{side}_ask"] = ask
                if is_valid_price(bid):
                    state[f"{side}_bid"] = bid
                state["last_quote_ts"] = int(time.time())

        if int(time.time()) - last_ws > 5:
            refresh_quotes_once()
            with state_lock:
                ask = state["up_ask"] if side == "up" else state["down_ask"]
                bid = state["up_bid"] if side == "up" else state["down_bid"]
                last_ws = state["last_ws"]
            if int(time.time()) - last_ws > 8:
                log("Blocked entry: quote stale")
                log_trade(f"BUY {side.upper()} blocked: quote stale")
                clear_buy_pending(pending_key)
                return
        if not is_valid_price(ask):
            log("Blocked entry: invalid ask")
            log_trade(f"BUY {side.upper()} blocked: invalid ask")
            clear_buy_pending(pending_key)
            return
        ask_v = float(ask)
        bid_v = float(bid) if is_valid_price(bid) else None
        if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
            log("Blocked entry: placeholder ask (>=98c)")
            log_trade(f"BUY {side.upper()} blocked: placeholder ask")
            clear_buy_pending(pending_key)
            return
        if ask_v * 100 > MAX_ENTRY_CENT:
            log(f"Blocked entry: ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c")
            log_trade(f"BUY {side.upper()} blocked: ask>{MAX_ENTRY_CENT:.0f}c")
            clear_buy_pending(pending_key)
            return
        log(
            f"BUY {side.upper()} submit ask={to_cent_display(ask_v)} "
            f"usd=${usd:.2f} type={MARKET_BUY_ORDER_TYPE_LABEL}"
        )
        log_trade(
            f"BUY {side.upper()} submit ask={to_cent_display(ask_v)} "
            f"usd=${usd:.2f} type={MARKET_BUY_ORDER_TYPE_LABEL}"
        )
        resp = None
        buy_limit = None
        final_error = ""
        max_attempts = 3 if MARKET_BUY_ORDER_TYPE == OrderType.FAK else 2
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                fresh_bid, fresh_ask = sample_top_prices(tok)
                if fresh_ask is not None:
                    ask = str(fresh_ask)
                if fresh_bid is not None:
                    bid = str(fresh_bid)
                with state_lock:
                    if is_valid_price(ask):
                        state[f"{side}_ask"] = ask
                    if is_valid_price(bid):
                        state[f"{side}_bid"] = bid
                    state["last_quote_ts"] = int(time.time())
                if not is_valid_price(ask):
                    break
                ask_v = float(ask)
                bid_v = float(bid) if is_valid_price(bid) else None
                if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
                    final_error = "placeholder ask"
                    break
                if ask_v * 100 > MAX_ENTRY_CENT:
                    final_error = f"ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c"
                    break
            slip_bps = ENTRY_SLIPPAGE_BPS + ((attempt - 1) * 125)
            buy_limit = min(round(ask_v * (1 + slip_bps / 10000.0), 2), 0.99)
            if PTB_EXECUTION_WORKER:
                resp_data, worker_err = worker_market_order(
                    tok, "buy", usd, "usdc",
                    price=buy_limit, order_type=MARKET_BUY_ORDER_TYPE_LABEL,
                )
                if resp_data is not None:
                    resp = {"status": resp_data.get("status", "?"), "id": resp_data.get("order_id", "")}
                    break
                final_error = worker_err
                msg_l = worker_err.lower()
                if attempt < max_attempts and ("no orders found" in msg_l or "no match" in msg_l):
                    log(f"BUY {side.upper()} worker retry {attempt}/{max_attempts}")
                    time.sleep(0.15)
                    continue
                log(f"BUY {side.upper()} worker fallback: {worker_err}")
            mo = MarketOrderArgs(
                token_id=tok, amount=usd, side=BUY,
                price=buy_limit, order_type=MARKET_BUY_ORDER_TYPE,
            )
            signed = client.create_market_order(mo)
            try:
                resp = client.post_order(signed, MARKET_BUY_ORDER_TYPE)
                break
            except Exception as e:
                msg = str(e)[:250]
                final_error = msg
                msg_l = msg.lower()
                if attempt < max_attempts and (
                    "no orders found" in msg_l or "no match" in msg_l
                ):
                    log(f"BUY {side.upper()} retry {attempt}/{max_attempts}")
                    time.sleep(0.15)
                    continue
                # network retry
                if attempt < max_attempts and (
                    "request exception" in msg_l or "timed out" in msg_l or "status_code=none" in msg_l
                ):
                    log(f"BUY {side.upper()} network retry {attempt}/{max_attempts}...")
                    time.sleep(0.3)
                    continue
                with state_lock:
                    state["last_real_action_ts"] = time.time()
                entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd, attempts=4, poll_sec=0.5)
                if shares > 0 and entry is not None:
                    add_notional = round(float(entry) * float(shares), 4)
                    merge_buy_fill_once(tok, side, shares, add_notional, had_local_before=had_local_before)
                    log(f"MKT {side.upper()} api-uncertain but filled@{to_cent_display(entry)} shares={shares}")
                    log_trade(f"BUY {side.upper()} uncertain-api fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
                    return
                log(f"BUY {side.upper()} api-uncertain: auto-verify up to {UNCERTAIN_BUY_VERIFY_SEC}s")
                keep_pending = True
                set_buy_pending(pending_key, UNCERTAIN_BUY_VERIFY_SEC + 2)
                threading.Thread(
                    target=verify_uncertain_buy,
                    args=(tok, side, usd, started_at, pre_shares, had_local_before, msg, pending_key),
                    daemon=True,
                ).start()
                return
        if resp is None:
            status = final_error or "no match"
            if not STRICT_EXECUTION:
                gtc_px, gtc_shares, gtc_oid = try_gtc_fallback_buy(tok, usd, ask_v)
                if gtc_shares > 0 and gtc_px is not None:
                    add_notional = round(float(gtc_shares) * float(gtc_px), 4)
                    merge_buy_fill_once(tok, side, round(gtc_shares, 4), add_notional, had_local_before=had_local_before)
                    log(f"GTC BUY {side.upper()} fill@{to_cent_display(gtc_px)} shares={gtc_shares:.4f}")
                    log_trade(f"BUY {side.upper()} GTC fill@{to_cent_display(gtc_px)} sh={gtc_shares:.4f} usd~${add_notional:.2f}")
                    return
            log(f"MKT {side.upper()} no-fill ask={to_cent_display(ask_v)} usd=${usd:.2f} [{status}]")
            log_trade(f"BUY {side.upper()} no-fill [{status}]")
            return
        status = resp.get("status", "?")
        oid = str(resp.get("id", ""))[:10]
        log(f"BUY {side.upper()} ask={to_cent_display(ask_v)} usd=${usd:.2f} lim={to_cent_display(buy_limit)}")
        with state_lock:
            state["last_real_action_ts"] = time.time()
        entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd)
        if shares > 0 and entry is not None:
            add_notional = round(float(entry) * float(shares), 4)
            merged = merge_buy_fill_once(tok, side, shares, add_notional, had_local_before=had_local_before)
            if str(status).lower() in ("matched", "filled"):
                if merged:
                    log(f"MKT {side.upper()} ${usd} fill@{to_cent_display(entry)} shares={shares} [{oid}]")
                else:
                    log(f"MKT {side.upper()} ${usd} fill@{to_cent_display(entry)} shares={shares} [{oid}] (already-synced)")
            else:
                if merged:
                    log(f"MKT {side.upper()} ${usd} partial@{to_cent_display(entry)} shares={shares} [{status}]")
                else:
                    log(f"MKT {side.upper()} ${usd} partial@{to_cent_display(entry)} shares={shares} [{status}] (already-synced)")
            log_trade(f"BUY {side.upper()} fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
        else:
            if not STRICT_EXECUTION:
                gtc_px, gtc_shares, gtc_oid = try_gtc_fallback_buy(tok, usd, ask_v)
                if gtc_shares > 0 and gtc_px is not None:
                    add_notional = round(float(gtc_shares) * float(gtc_px), 4)
                    merge_buy_fill_once(tok, side, round(gtc_shares, 4), add_notional, had_local_before=had_local_before)
                    log_trade(f"BUY {side.upper()} GTC fill@{to_cent_display(gtc_px)} sh={gtc_shares:.4f} usd~${add_notional:.2f}")
                    return
            log(f"MKT {side.upper()} no-fill ask={to_cent_display(ask_v)} usd=${usd:.2f} [{status}]")
            log_trade(f"BUY {side.upper()} no-fill [{status}]")
    except Exception as e:
        log(str(e)[:250])
        log_trade(f"BUY {side.upper()} error: {str(e)[:120]}")
    finally:
        if pending_key and not keep_pending:
            clear_buy_pending(pending_key)


def market_buy_next(side: str, usd: float, token_id: str = None, market_slug: str = None):
    with state_lock:
        tok = token_id or (state.get("next_up_token") if side == "up" else state.get("next_down_token"))
        dry = state["dry_run"]
        last_action = state["last_real_action_ts"]
        next_slug = market_slug or state.get("next_slug") or "NEXT"
        had_local_before = tok in state["positions"] if tok else False
    if not tok:
        log(f"NEXT market token not ready for {side.upper()}")
        log_trade(f"BUY NEXT {side.upper()} blocked: token not ready")
        return
    bid_s, ask_s = get_cached_quote(tok, max_age=25)
    if not is_valid_price(ask_s):
        bid_v, ask_v = sample_top_prices(tok)
        if ask_v is not None:
            ask_s = str(ask_v)
        if bid_v is not None:
            bid_s = str(bid_v)
    if not is_valid_price(ask_s):
        log(f"Blocked NEXT entry: invalid ask {side.upper()}")
        log_trade(f"BUY NEXT {side.upper()} blocked: invalid ask")
        return
    ask_v = float(ask_s)
    bid_v = float(bid_s) if is_valid_price(bid_s) else None
    if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
        log(f"Blocked NEXT entry: placeholder ask {side.upper()}")
        log_trade(f"BUY NEXT {side.upper()} blocked: placeholder ask")
        return
    if ask_v * 100 > MAX_ENTRY_CENT:
        log(f"Blocked NEXT entry: ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c")
        log_trade(f"BUY NEXT {side.upper()} blocked: ask>{MAX_ENTRY_CENT:.0f}c")
        return
    if dry:
        usd = norm_usd(usd)
        shares = round(usd / max(ask_v, 0.01), 4)
        upsert_position_merge(tok, side, shares, usd, position_slug=next_slug)
        log(f"[DRY] NEXT MKT {side.upper()} ${usd} @ {ask_v}")
        log_trade(f"BUY NEXT {side.upper()} DRY fill@{to_cent_display(ask_v)} sh={shares:.4f} usd=${usd:.2f}")
        return
    try:
        usd = norm_usd(usd)
        if usd < MIN_ORDER_USD:
            return
        if not dry and (time.time() - last_action) < 1.2:
            log("Blocked NEXT entry: action cooldown")
            return
        log_trade(f"BUY NEXT {side.upper()} submit ask={to_cent_display(ask_v)} usd=${usd:.2f}")
        started_at = int(time.time())
        pre_shares = get_available_shares(tok)
        resp = None
        final_error = ""
        buy_limit = None
        max_attempts = 3 if MARKET_BUY_ORDER_TYPE == OrderType.FAK else 2
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                bid_v2, ask_v2 = sample_top_prices(tok)
                if ask_v2 is not None:
                    ask_v = float(ask_v2)
                if bid_v2 is not None:
                    bid_v = float(bid_v2)
                    put_cached_quote(tok, bid_v2, ask_v2)
                if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
                    final_error = "placeholder ask"
                    break
                if ask_v * 100 > MAX_ENTRY_CENT:
                    final_error = f"ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c"
                    break
            slip_bps = ENTRY_SLIPPAGE_BPS + ((attempt - 1) * 125)
            buy_limit = min(round(ask_v * (1 + slip_bps / 10000.0), 2), 0.99)
            mo = MarketOrderArgs(
                token_id=tok, amount=usd, side=BUY,
                price=buy_limit, order_type=MARKET_BUY_ORDER_TYPE,
            )
            signed = client.create_market_order(mo)
            try:
                resp = client.post_order(signed, MARKET_BUY_ORDER_TYPE)
                break
            except Exception as e:
                msg = str(e)[:200]
                final_error = msg
                msg_l = msg.lower()
                if attempt < max_attempts and ("no orders found" in msg_l or "no match" in msg_l):
                    time.sleep(0.15)
                    continue
                log(f"NEXT BUY {side.upper()} err: {msg}")
                log_trade(f"BUY NEXT {side.upper()} error: {msg[:120]}")
                return
        if resp is None:
            log(f"NEXT MKT {side.upper()} no-fill [{final_error or 'no match'}]")
            log_trade(f"BUY NEXT {side.upper()} no-fill [{(final_error or 'no match')[:100]}]")
            return
        oid = str(resp.get("id", ""))[:10]
        with state_lock:
            state["last_real_action_ts"] = time.time()
        entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd)
        if shares > 0 and entry is not None:
            add_notional = round(float(entry) * float(shares), 4)
            merged = merge_buy_fill_once(
                tok, side, shares, add_notional,
                had_local_before=had_local_before, position_slug=next_slug,
            )
            log(f"NEXT MKT {side.upper()} ${usd} fill@{to_cent_display(entry)} shares={shares} [{oid}]")
            if not merged:
                log(f"NEXT MKT {side.upper()} fill already-synced, skip double-merge")
            log_trade(f"BUY NEXT {side.upper()} fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
        else:
            log(f"NEXT MKT {side.upper()} no-fill")
            log_trade(f"BUY NEXT {side.upper()} no-fill")
    except Exception as e:
        log(f"NEXT BUY {side.upper()} err: {str(e)[:200]}")
        log_trade(f"BUY NEXT {side.upper()} error: {str(e)[:120]}")


def limit_buy(side: str, price: float, usd: float):
    with state_lock:
        tok = state["up_token"] if side == "up" else state["down_token"]
        ask_raw = state["up_ask"] if side == "up" else state["down_ask"]
        dry = state["dry_run"]
    if not tok:
        log("No token loaded")
        return
    # Refresh quote dulu
    if not is_quote_fresh(1.0):
        fresh_bid, fresh_ask = sample_top_prices(tok)
        if fresh_ask is not None:
            ask_raw = str(fresh_ask)
            with state_lock:
                state[f"{side}_ask"] = ask_raw
                state["last_quote_ts"] = int(time.time())
    ask_v = float(ask_raw) if is_valid_price(ask_raw) else None
    order_price = float(price)
    usd = norm_usd(usd)
    if usd < MIN_ORDER_USD:
        log(f"Blocked LIMIT BUY {side.upper()}: amount too small (<${MIN_ORDER_USD:.2f})")
        return
    size = norm_size(usd / max(order_price, 0.01))
    if size < MIN_ORDER_SHARES:
        log(f"Blocked LIMIT BUY {side.upper()}: size terlalu kecil ({size:.4f}sh)")
        return
    if dry:
        oid = f"dry-{int(time.time())}"[-10:]
        with state_lock:
            state["open_orders"].append({
                "id": oid, "side": side,
                "price": order_price, "size": size, "status": "dry"
            })
        log(
            f"[DRY] LIMIT {side.upper()} ${usd} "
            f"limit<={to_cent_display(price)} order@{to_cent_display(order_price)} "
            f"size={size:.4f} [{oid}]"
        )
        return
    try:
        oa = OrderArgs(token_id=tok, price=order_price, size=size, side=BUY)
        signed = client.create_order(oa)
        resp = client.post_order(signed, OrderType.GTC)
        oid = str(resp.get("id", ""))
        with state_lock:
            row = {
                "id": oid, "side": side,
                "price": order_price, "size": size,
                "status": resp.get("status", "?")
            }
            state["open_orders"].append(row)
            state["open_orders_remote"].append(dict(row))
        if ask_v is not None and ask_v <= order_price:
            fill_hint = " (marketable)"
        else:
            fill_hint = " (resting)"
        log(
            f"LIMIT {side.upper()} ${usd} "
            f"limit<={to_cent_display(price)} order@{to_cent_display(order_price)} "
            f"size={size:.4f} [{oid[:10]}]{fill_hint}"
        )
    except Exception as e:
        log(str(e)[:250])


def limit_sell(side: str, price: float, shares: float = None, target_market: str = None):
    if target_market in ("current", "next", "previous"):
        tok, pos, off_market = resolve_position_token_for_target(side, target_market)
    else:
        tok, pos, off_market = resolve_position_token(side, allow_off_market=True)
    with state_lock:
        dry = state["dry_run"]
    if not tok or not pos:
        log(f"No {side.upper()} position for limit sell")
        return
    sell_all = shares is None
    shares = norm_size(shares) if shares is not None else 0.0
    if not sell_all and shares <= 0:
        log("Invalid sell size")
        return
    if dry:
        pos_shares = float(pos.get("shares", 0.0))
        sell_sz = pos_shares if sell_all else min(shares, pos_shares)
        if sell_sz < MIN_ORDER_SHARES or (sell_sz * max(price, 0.0)) < MIN_ORDER_USD:
            log(f"Blocked LIMIT SELL {side.upper()}: dust size")
            return
        oid = f"dry-s-{int(time.time())}"[-10:]
        with state_lock:
            state["open_orders"].append({
                "id": oid, "side": side, "price": price, "size": sell_sz,
                "status": "dry-sell", "type": "sell_limit",
            })
        log(f"[DRY] LIMIT SELL {side.upper()} {sell_sz} @ {price} [{oid}]")
        return
    try:
        available = get_stable_available_shares(tok, attempts=3, delay_sec=0.12)
        sell_sz = available if sell_all else min(shares, available)
        if sell_sz < MIN_ORDER_SHARES or (sell_sz * max(price, 0.0)) < MIN_ORDER_USD:
            log(f"Blocked LIMIT SELL {side.upper()}: dust or insufficient shares")
            return
        oa = OrderArgs(token_id=tok, price=price, size=norm_size(sell_sz), side=SELL)
        signed = client.create_order(oa)
        resp = client.post_order(signed, OrderType.GTC)
        oid = str(resp.get("id", ""))
        with state_lock:
            row = {
                "id": oid, "side": side, "price": price,
                "size": norm_size(sell_sz), "status": resp.get("status", "?"), "type": "sell_limit",
            }
            state["open_orders"].append(row)
            state["open_orders_remote"].append(dict(row))
        log(f"LIMIT SELL {side.upper()} {sell_sz:.4f} @ {price} [{oid[:10]}]")
    except Exception as e:
        log(str(e)[:250])


def cash_out(side: str, target_market: str = None):
    side_key = str(side or "").lower()
    if side_key not in ("up", "down"):
        return
    with _cashout_lock:
        if side_key in _cashout_inflight:
            log(f"CASHOUT {side_key.upper()} already in progress")
            return
        _cashout_inflight.add(side_key)

    try:
        target = str(target_market or "").lower()
        use_fixed_target = target in ("current", "next", "previous")
        if target_market in ("current", "next", "previous"):
            tok, pos, off_market = resolve_position_token_for_target(side, target_market)
        else:
            tok, pos, off_market = resolve_position_token(side, allow_off_market=True)
        with state_lock:
            dry = state["dry_run"]
        if not pos:
            log(f"No {side.upper()} position")
            return
        if off_market:
            log(f"Using previous {side.upper()} position token for cashout")
        if dry:
            with state_lock:
                if target_market == "next":
                    next_tok = state.get("next_up_token") if side == "up" else state.get("next_down_token")
                    bid_s, _ = get_cached_quote(next_tok, max_age=25)
                elif target_market == "previous":
                    bid_s = state["prev_up_bid"] if side == "up" else state["prev_down_bid"]
                else:
                    bid_s = state["up_bid"] if side == "up" else state["down_bid"]
            bid = float(bid_s) if is_valid_price(bid_s) else pos["entry"]
            pnl = (bid - pos["entry"]) * pos["shares"]
            with state_lock:
                state["positions"].pop(tok, None)
                state["open_orders"] = [o for o in state["open_orders"] if o["side"] != side]
            bump_session_pnl(pnl, is_dry=True)
            log(f"[DRY] CASHOUT {side.upper()} PnL {pnl:+.4f}")
            log_trade(f"SELL {side.upper()} DRY pnl={pnl:+.4f}")
            return

        attempts = 2 if STRICT_EXECUTION else 5
        for attempt in range(1, attempts + 1):
            if use_fixed_target:
                tok, pos, _ = resolve_position_token_for_target(side, target)
            else:
                tok, pos, _ = resolve_position_token(side, allow_off_market=True)
            if not pos:
                log(f"CASHOUT {side.upper()} position cleared after partial")
                return
            try:
                with state_lock:
                    last_action = state["last_real_action_ts"]
                cooldown_remaining = 1.0 - (time.time() - last_action)
                if cooldown_remaining > 0.1:
                    time.sleep(min(cooldown_remaining, 0.2))
                started_at = int(time.time())
                if use_fixed_target and target == "next":
                    bid_s, ask_s = get_cached_quote(tok, max_age=25)
                    if attempt > 1 or not is_valid_price(bid_s):
                        fresh_bid, fresh_ask = sample_top_prices(tok)
                        if fresh_bid is not None:
                            bid_s = str(fresh_bid)
                        put_cached_quote(tok, fresh_bid, fresh_ask)
                elif use_fixed_target and target == "previous":
                    with state_lock:
                        bid_s = state["prev_up_bid"] if side == "up" else state["prev_down_bid"]
                    if attempt > 1 or not is_valid_price(bid_s):
                        fresh_bid, _ = sample_top_prices(tok)
                        if fresh_bid is not None:
                            bid_s = str(fresh_bid)
                            with state_lock:
                                if side == "up":
                                    state["prev_up_bid"] = bid_s
                                else:
                                    state["prev_down_bid"] = bid_s
                else:
                    if not is_quote_fresh(1.0):
                        fresh_bid, _ = sample_top_prices(tok)
                        if fresh_bid is not None:
                            bid_s = str(fresh_bid)
                            with state_lock:
                                if side == "up":
                                    state["up_bid"] = bid_s
                                else:
                                    state["down_bid"] = bid_s
                                state["last_quote_ts"] = int(time.time())
                        else:
                            with state_lock:
                                bid_s = state["up_bid"] if side == "up" else state["down_bid"]
                    else:
                        with state_lock:
                            bid_s = state["up_bid"] if side == "up" else state["down_bid"]

                bid = float(bid_s) if is_valid_price(bid_s) else float(pos["entry"])
                pos_shares_before = float(pos.get("shares", 0.0))
                available = max(get_available_shares(tok), 0.0)
                if available <= POSITION_DUST_SHARES and pos_shares_before > POSITION_DUST_SHARES:
                    if attempt < attempts:
                        log(f"CASHOUT {side.upper()} balance read unstable, retry {attempt}/{attempts}...")
                        time.sleep(0.5)
                        continue
                    log(f"CASHOUT {side.upper()} balance unavailable, keep local position")
                    return
                sell_size = fak_sell_size(min(float(pos["shares"]), available))
                if sell_size < MIN_ORDER_SHARES or (sell_size * max(bid, 0.0)) < MIN_ORDER_USD:
                    if pos_shares_before <= POSITION_DUST_SHARES:
                        with state_lock:
                            state["positions"].pop(tok, None)
                        log(f"CASHOUT {side.upper()} skipped dust ({sell_size:.6f}sh)")
                        log_trade(f"SELL {side.upper()} skipped dust")
                    else:
                        log(f"CASHOUT {side.upper()} skipped sell_size={sell_size:.6f}sh, keep position")
                        log_trade(f"SELL {side.upper()} skipped: sell size too small")
                    return
                if sell_size <= 0:
                    if attempt < attempts:
                        log(f"CASHOUT {side.upper()} waiting balance {attempt}/{attempts}...")
                        time.sleep(2)
                        continue
                    log(f"No onchain {side.upper()} shares available to cashout")
                    log_trade(f"SELL {side.upper()} no onchain shares")
                    return
                slip_bps = EXIT_SLIPPAGE_BPS + ((attempt - 1) * 150)
                if attempt >= 2:
                    slip_bps = EXIT_SLIPPAGE_BPS + 300 + ((attempt - 2) * 200)
                sprice = round(max(bid * (1 - slip_bps / 10000.0), 0.01), 2)
                if attempt >= 3:
                    sprice = round(max(min(sprice, bid - 0.05), 0.01), 2)
                tok_short = str(tok)[-8:] if tok else "-"
                log(
                    f"SELL {side.upper()} submit bid={to_cent_display(bid)} "
                    f"lim={to_cent_display(sprice)} size={sell_size:.4f} tok=*{tok_short}"
                )
                log_trade(f"SELL {side.upper()} submit lim={to_cent_display(sprice)} size={sell_size:.4f}")
                if PTB_EXECUTION_WORKER:
                    resp_data, worker_err = worker_market_order(
                        tok, "sell", sell_size, "shares", price=sprice, order_type="FAK",
                    )
                    if resp_data is not None:
                        resp = {"status": resp_data.get("status", "?")}
                    else:
                        msg_l = worker_err.lower()
                        if attempt < attempts and "no orders found" in msg_l:
                            log(f"CASHOUT {side.upper()} worker no-match, retry {attempt}/{attempts}...")
                            time.sleep(0.2)
                            continue
                        if worker_err:
                            log(f"CASHOUT {side.upper()} worker fallback: {worker_err}")
                        oa = OrderArgs(token_id=tok, price=sprice, size=sell_size, side=SELL)
                        signed = client.create_order(oa)
                        resp = client.post_order(signed, OrderType.FAK)
                else:
                    oa = OrderArgs(token_id=tok, price=sprice, size=sell_size, side=SELL)
                    signed = client.create_order(oa)
                    resp = client.post_order(signed, OrderType.FAK)
                with state_lock:
                    state["last_real_action_ts"] = time.time()
                fills = []
                for _ in range(2):
                    time.sleep(0.2)
                    fills = get_recent_fills(tok, SELL, started_at)
                    if fills:
                        break
                remaining = max(get_available_shares(tok), 0.0)
                sold = max(round(available - remaining, 6), 0.0)
                fill_px = bid
                if fills:
                    vwap, total_sz = vwap_from_fills(fills)
                    if vwap is not None and total_sz > 0:
                        fill_px = vwap
                        sold = min(sold if sold > 0 else total_sz, total_sz)
                pnl = (fill_px - pos["entry"]) * sold
                rem_state = remaining
                local_expected_rem = max(round(pos_shares_before - sold, 6), 0.0)
                if sold > 0 and remaining > (local_expected_rem + POSITION_DUST_SHARES):
                    rem_state = local_expected_rem
                with state_lock:
                    if rem_state <= POSITION_DUST_SHARES:
                        state["positions"].pop(tok, None)
                    else:
                        if tok in state["positions"]:
                            old_sh = max(float(pos.get("shares", 0.0)), 0.000001)
                            old_usd = float(pos.get("usd_in", old_sh * float(pos.get("entry", 0.0))))
                            new_sh = round(rem_state, 6)
                            new_usd = round(old_usd * (new_sh / old_sh), 4)
                            state["positions"][tok]["shares"] = new_sh
                            state["positions"][tok]["usd_in"] = new_usd
                    state["open_orders"] = [o for o in state["open_orders"] if o["side"] != side]
                if rem_state <= POSITION_DUST_SHARES:
                    bump_session_pnl(pnl, is_dry=False)
                    log(f"CASHOUT {side.upper()} CLOSED sold={sold} px={to_cent_display(fill_px)} PnL {pnl:+.4f} | {resp.get('status', '?')}")
                    log_trade(f"SELL {side.upper()} CLOSED sh={sold:.4f} px={to_cent_display(fill_px)} pnl={pnl:+.4f}")
                else:
                    bump_session_pnl(pnl, is_dry=False)
                    log(f"CASHOUT {side.upper()} PARTIAL sold={sold} rem={rem_state} px={to_cent_display(fill_px)}")
                    log_trade(f"SELL {side.upper()} PARTIAL sh={sold:.4f} rem={rem_state:.4f} px={to_cent_display(fill_px)} pnl={pnl:+.4f}")
                return
            except Exception as e:
                msg = str(e)[:250]
                msg_l = msg.lower()
                try:
                    chain_rem = max(get_available_shares(tok), 0.0)
                except Exception:
                    chain_rem = None
                if chain_rem is not None and chain_rem <= POSITION_DUST_SHARES:
                    with state_lock:
                        state["positions"].pop(tok, None)
                    log(f"CASHOUT {side.upper()} cleared after exchange fill sync")
                    return
                if "not enough balance / allowance" in msg_l and chain_rem is not None:
                    chain_rem_rounded = round(chain_rem, 6)
                    clear_after_sync = (
                        chain_rem_rounded <= POSITION_DUST_SHARES
                        or fak_sell_size(chain_rem_rounded) < MIN_ORDER_SHARES
                        or (chain_rem_rounded * max(float(pos.get("entry", 0.0)), 0.0)) <= POSITION_DUST_USD
                    )
                    with state_lock:
                        if tok in state["positions"]:
                            if clear_after_sync:
                                state["positions"].pop(tok, None)
                            else:
                                state["positions"][tok]["shares"] = chain_rem_rounded
                                state["positions"][tok]["usd_in"] = round(
                                    float(state["positions"][tok]["entry"]) * chain_rem_rounded, 4
                                )
                    if clear_after_sync:
                        log(f"CASHOUT {side.upper()} sync cleared residual chain_rem={chain_rem_rounded:.6f}sh")
                        return
                    if attempt < attempts:
                        log(f"CASHOUT {side.upper()} sync retry {attempt}/{attempts}...")
                        time.sleep(0.4)
                        continue
                if attempt < attempts and "no orders found" in msg_l:
                    log(f"CASHOUT {side.upper()} FAK no-match, retry fresh price {attempt}/{attempts}...")
                    time.sleep(0.3)
                    continue
                if attempt < attempts and ("request exception" in msg_l or "timed out" in msg_l):
                    log(f"CASHOUT {side.upper()} transport retry {attempt}/{attempts}...")
                    time.sleep(0.2)
                    continue
                if attempt < attempts and "not enough balance / allowance" in msg_l:
                    log(f"CASHOUT {side.upper()} retry {attempt}/{attempts}...")
                    time.sleep(0.6)
                    continue
                log(msg)
                log_trade(f"SELL {side.upper()} error: {msg[:120]}")
                return
    finally:
        with _cashout_lock:
            _cashout_inflight.discard(side_key)


# PATCH 4: cancel_all juga clear open_orders_remote
def cancel_all():
    with state_lock:
        dry = state["dry_run"]
    if dry:
        with state_lock:
            state["open_orders"].clear()
        log("[DRY] cancel all")
        return
    try:
        client.cancel_all()
        with state_lock:
            state["open_orders"].clear()
            state["open_orders_remote"].clear()
        log("All orders cancelled")
    except Exception as e:
        log(str(e)[:80])


def cancel_order(order_id: str):
    oid = str(order_id or "").strip()
    if not oid:
        log("Invalid order id")
        return
    with state_lock:
        dry = state["dry_run"]
    if dry:
        with state_lock:
            before = len(state["open_orders"])
            state["open_orders"] = [o for o in state["open_orders"] if str(o.get("id", "")) != oid]
            after = len(state["open_orders"])
        if after < before:
            log(f"[DRY] Order cancelled [{oid[:10]}]")
        else:
            log(f"[DRY] Order not found [{oid[:10]}]")
        return
    try:
        client.cancel(oid)
        with state_lock:
            state["open_orders"] = [o for o in state["open_orders"] if str(o.get("id", "")) != oid]
            state["open_orders_remote"] = [
                o for o in state["open_orders_remote"]
                if str((o or {}).get("id", "")) != oid
            ]
        log(f"Order cancelled [{oid[:10]}]")
    except Exception as e:
        log(f"Cancel failed [{oid[:10]}]: {str(e)[:120]}")


def get_available_shares(token_id: str) -> float:
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=SIG_TYPE,
        )
        bal_raw = client.get_balance_allowance(params).get("balance", "0")
        return max(float(bal_raw) / 1_000_000, 0.0)
    except Exception:
        return 0.0


def get_stable_available_shares(token_id: str, attempts: int = 3, delay_sec: float = 0.12) -> float:
    best = 0.0
    tries = max(1, int(attempts))
    for i in range(tries):
        v = get_available_shares(token_id)
        if v > best:
            best = v
        if best > POSITION_DUST_SHARES:
            break
        if i < (tries - 1):
            time.sleep(max(0.0, float(delay_sec)))
    return best


def get_recent_fills(token_id: str, side: str, since_ts: int):
    fills = []
    try:
        trades = client.get_trades()
        if not isinstance(trades, list):
            return fills
        for t in trades:
            if str(t.get("asset_id", "")) != str(token_id):
                continue
            if str(t.get("side", "")).upper() != side.upper():
                continue
            try:
                mt = int(float(t.get("match_time", 0)))
            except Exception:
                mt = 0
            if mt < since_ts - 2:
                continue
            try:
                sz = float(t.get("size", 0))
                px = float(t.get("price", 0))
            except Exception:
                continue
            if sz <= 0 or px <= 0:
                continue
            fills.append({"size": sz, "price": px, "match_time": mt, "id": t.get("id", "")})
    except Exception:
        return fills
    return fills


def vwap_from_fills(fills):
    total_sz = sum(f["size"] for f in fills)
    if total_sz <= 0:
        return None, 0.0
    total_notional = sum(f["size"] * f["price"] for f in fills)
    return total_notional / total_sz, total_sz


def infer_buy_fill(
    token_id: str,
    since_ts: int,
    pre_shares: float,
    usd: float,
    attempts: int = 3,
    poll_sec: float = 0.4,
):
    fills = []
    max_attempts = max(1, int(attempts))
    for i in range(max_attempts):
        if i > 0 and poll_sec > 0:
            time.sleep(float(poll_sec))
        fills = get_recent_fills(token_id, BUY, since_ts)
        if fills:
            break
    entry = None
    shares = 0.0
    if fills:
        vwap, total_sz = vwap_from_fills(fills)
        if vwap is not None and total_sz > 0:
            entry = vwap
            shares = round(total_sz, 4)
    if entry is None or shares <= 0:
        post_shares = get_available_shares(token_id)
        filled_shares = max(post_shares - pre_shares, 0.0)
        if filled_shares > 0.0001:
            entry = usd / filled_shares
            shares = round(filled_shares, 4)
    return entry, shares


def verify_uncertain_buy(
    token_id: str,
    side: str,
    usd: float,
    since_ts: int,
    pre_shares: float,
    had_local_before: bool,
    api_msg: str,
    pending_key: str = "",
):
    started = time.time()
    deadline = started + UNCERTAIN_BUY_VERIFY_SEC
    try:
        while state["running"] and time.time() < deadline:
            entry, shares = infer_buy_fill(token_id, since_ts, pre_shares, usd, attempts=1, poll_sec=0.0)
            if shares > 0 and entry is not None:
                add_notional = round(float(entry) * float(shares), 4)
                merged = True
                with state_lock:
                    has_now = token_id in state["positions"]
                    existing_shares = float(state["positions"].get(token_id, {}).get("shares", 0))
                if has_now and not had_local_before:
                    if abs(existing_shares - shares) < 0.05:
                        log(f"BUY {side.upper()} uncertain-api: already reconciled ({existing_shares:.4f}sh), skip merge")
                        return
                    merged = False
                if merged:
                    upsert_position_merge(token_id, side, shares, add_notional)
                age_s = max(0.0, time.time() - started)
                log(f"BUY {side.upper()} uncertain-api confirmed after {age_s:.1f}s fill@{to_cent_display(entry)} sh={shares:.4f}")
                log_trade(f"BUY {side.upper()} uncertain-api fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
                return
            time.sleep(UNCERTAIN_BUY_POLL_SEC)
        log(f"BUY {side.upper()} uncertain-api unresolved after {UNCERTAIN_BUY_VERIFY_SEC}s: {api_msg[:180]}")
    finally:
        clear_buy_pending(pending_key)


def enqueue_market_task(market_key: str, fn, *args):
    def worker(q: "queue.Queue[tuple]"):
        while state["running"]:
            try:
                task = q.get(timeout=1)
            except queue.Empty:
                continue
            if task is None:
                break
            f, a = task
            try:
                f(*a)
            except Exception as e:
                log(f"task error: {str(e)[:120]}")
            finally:
                q.task_done()

    with market_queue_lock:
        q = market_queues.get(market_key)
        if q is None:
            q = queue.Queue()
            market_queues[market_key] = q
            threading.Thread(target=worker, args=(q,), daemon=True).start()
    q.put((fn, args))


def run_fast_task(fn, *args):
    def runner():
        try:
            fn(*args)
        except Exception as e:
            log(f"task error: {str(e)[:120]}")
    threading.Thread(target=runner, daemon=True).start()


def current_market_key() -> str:
    with state_lock:
        slug = state.get("current_slug") or ""
    return slug or "default"


def buy_pending_key(side: str, target: str) -> str:
    t = str(target or "current").lower()
    with state_lock:
        cur_slug = str(state.get("current_slug") or "default")
        next_slug = str(state.get("next_slug") or "next")
    slug = next_slug if t == "next" else cur_slug
    return f"{t}:{slug}:{side}"


def buy_pending_seconds(key: str) -> float:
    now = time.time()
    with _buy_pending_lock:
        stale = [k for k, ts in _buy_pending_until.items() if ts <= now]
        for k in stale:
            _buy_pending_until.pop(k, None)
        until = float(_buy_pending_until.get(key, 0.0))
    return max(0.0, until - now)


def set_buy_pending(key: str, ttl_sec: float):
    if not key:
        return
    ttl = max(0.5, float(ttl_sec))
    with _buy_pending_lock:
        _buy_pending_until[key] = time.time() + ttl


def clear_buy_pending(key: str):
    if not key:
        return
    with _buy_pending_lock:
        _buy_pending_until.pop(key, None)


def allow_buy_command(side: str, usd: float) -> bool:
    mk = current_market_key()
    sig = f"{mk}:{side}:{round(float(usd), 2):.2f}"
    now = time.time()
    with _buy_cmd_lock:
        last_sig = str(_last_buy_cmd.get("sig", ""))
        last_ts = float(_last_buy_cmd.get("ts", 0.0))
        if sig == last_sig and (now - last_ts) < BUY_CMD_GUARD_SEC:
            return False
        _last_buy_cmd["sig"] = sig
        _last_buy_cmd["ts"] = now
        return True


def command_loop():
    while state["running"]:
        try:
            raw = cmd_queue.get(timeout=0.05).strip()
        except queue.Empty:
            continue
        parts = raw.split()
        if not parts:
            continue
        cmd = parts[0].lower()
        try:
            if cmd == "q":
                state["running"] = False
            elif cmd == "bu" and len(parts) == 2:
                usd = float(parts[1])
                if not allow_buy_command("up", usd):
                    log(f"Blocked duplicate BUY UP cmd (<{BUY_CMD_GUARD_SEC:.1f}s)")
                    continue
                with state_lock:
                    target = state.get("target_market", "current")
                if target == "previous":
                    log("Blocked BUY UP: PREVIOUS is view-only")
                    continue
                log(f"BUY UP queued usd=${usd:.2f} target={target.upper()}")
                log_trade(f"BUY UP queued usd=${usd:.2f} target={target.upper()}")
                if target == "next":
                    with state_lock:
                        next_tok = state.get("next_up_token")
                        next_slug = state.get("next_slug")
                    if not next_tok:
                        log("Blocked BUY UP: NEXT market token not ready")
                        continue
                    run_fast_task(market_buy_next, "up", usd, next_tok, next_slug)
                    continue
                pkey = buy_pending_key("up", target)
                left = buy_pending_seconds(pkey)
                if left > 0:
                    log(f"Blocked BUY UP: previous execution still pending ({left:.1f}s)")
                    continue
                set_buy_pending(pkey, 8.0)
                run_fast_task(market_buy, "up", usd, pkey)
            elif cmd == "bd" and len(parts) == 2:
                usd = float(parts[1])
                if not allow_buy_command("down", usd):
                    log(f"Blocked duplicate BUY DOWN cmd (<{BUY_CMD_GUARD_SEC:.1f}s)")
                    continue
                with state_lock:
                    target = state.get("target_market", "current")
                if target == "previous":
                    log("Blocked BUY DOWN: PREVIOUS is view-only")
                    continue
                log(f"BUY DOWN queued usd=${usd:.2f} target={target.upper()}")
                log_trade(f"BUY DOWN queued usd=${usd:.2f} target={target.upper()}")
                if target == "next":
                    with state_lock:
                        next_tok = state.get("next_down_token")
                        next_slug = state.get("next_slug")
                    if not next_tok:
                        log("Blocked BUY DOWN: NEXT market token not ready")
                        continue
                    run_fast_task(market_buy_next, "down", usd, next_tok, next_slug)
                    continue
                pkey = buy_pending_key("down", target)
                left = buy_pending_seconds(pkey)
                if left > 0:
                    log(f"Blocked BUY DOWN: previous execution still pending ({left:.1f}s)")
                    continue
                set_buy_pending(pkey, 8.0)
                run_fast_task(market_buy, "down", usd, pkey)
            elif cmd == "bnu" and len(parts) == 2:
                run_fast_task(market_buy_next, "up", float(parts[1]))
            elif cmd == "bnd" and len(parts) == 2:
                run_fast_task(market_buy_next, "down", float(parts[1]))
            elif cmd == "lu" and len(parts) == 3:
                enqueue_market_task(current_market_key(), limit_buy, "up", parse_limit_price(parts[1]), float(parts[2]))
            elif cmd == "ld" and len(parts) == 3:
                enqueue_market_task(current_market_key(), limit_buy, "down", parse_limit_price(parts[1]), float(parts[2]))
            elif cmd == "su" and len(parts) in (2, 3):
                with state_lock:
                    target = state.get("target_market", "current")
                shares = float(parts[2]) if len(parts) == 3 else None
                enqueue_market_task(current_market_key(), limit_sell, "up", parse_limit_price(parts[1]), shares, target)
            elif cmd == "sd" and len(parts) in (2, 3):
                with state_lock:
                    target = state.get("target_market", "current")
                shares = float(parts[2]) if len(parts) == 3 else None
                enqueue_market_task(current_market_key(), limit_sell, "down", parse_limit_price(parts[1]), shares, target)
            elif cmd == "cu":
                with state_lock:
                    target = state.get("target_market", "current")
                log_trade(f"SELL UP queued target={str(target).upper()}")
                run_fast_task(cash_out, "up", target)
            elif cmd == "cd":
                with state_lock:
                    target = state.get("target_market", "current")
                log_trade(f"SELL DOWN queued target={str(target).upper()}")
                run_fast_task(cash_out, "down", target)
            elif cmd == "ca":
                enqueue_market_task(current_market_key(), cancel_all)
            elif cmd == "co" and len(parts) == 2:
                enqueue_market_task(current_market_key(), cancel_order, parts[1])
            elif cmd == "cpu":
                log_trade("SELL UP queued target=AUTO")
                run_fast_task(cash_out, "up", None)
            elif cmd == "cpd":
                log_trade("SELL DOWN queued target=AUTO")
                run_fast_task(cash_out, "down", None)
            elif cmd == "target" and len(parts) == 2 and parts[1] in ("current", "next", "previous"):
                with state_lock:
                    state["target_market"] = parts[1]
                log(f"Target market set to {parts[1].upper()}")
                if parts[1] == "next":
                    threading.Thread(target=prefetch_next_market, daemon=True).start()
            elif cmd == "r":
                threading.Thread(target=smart_fetch_tokens, daemon=True).start()
            elif cmd == "dry" and len(parts) == 2 and parts[1] in ("on", "off"):
                with state_lock:
                    state["dry_run"] = parts[1] == "on"
                log(f"Mode switched to {'DRY' if state['dry_run'] else 'REAL'}")
            else:
                log(f"? {raw}")
        except ValueError:
            log("Invalid amount")


def terminal_status_loop():
    while state["running"]:
        try:
            prob = compute_probability_snapshot()
            lock_open_probability_if_needed(prob)
            with state_lock:
                slug = state.get("current_slug", "-")
                rem = max(0, int(state.get("interval_end", 0)) - int(time.time()))
                ws_ok = bool(state.get("ws_ok"))
                up_bid = to_cent_display(state.get("up_bid"))
                down_bid = to_cent_display(state.get("down_bid"))
                ptb = state.get("btc_price_to_beat")
                now_px = state.get("btc_price_now")
                wr = float(state.get("prob_win_rate", 0.0))
                wt = int(state.get("prob_win_total", 0))
                p_open = state.get("prob_open_up")
                c_open = state.get("prob_open_confidence")
            p_show = float(p_open) if p_open is not None else float(prob.get("p_up", 0.5))
            c_show = float(c_open) if c_open is not None else float(prob.get("confidence", 0.0))
            ws_text = "LIVE" if ws_ok else "DOWN"
            if ptb is not None and now_px is not None:
                delta = float(now_px) - float(ptb)
                direction = "UP" if delta > 0 else ("DOWN" if delta < 0 else "FLAT")
                print(
                    f"[{time.strftime('%H:%M:%S')}] [STAT] {slug} rem={rem}s ws={ws_text} "
                    f"UP={up_bid} DOWN={down_bid} PTB={fmt_usd(ptb)} NOW={fmt_usd(now_px)} "
                    f"DELTA={direction} {fmt_usd(abs(delta))} "
                    f"PUP={p_show*100:.1f}% CONF={c_show*100:.0f}% WR={wr*100:.1f}%({wt})",
                    flush=True,
                )
            else:
                print(
                    f"[{time.strftime('%H:%M:%S')}] [STAT] {slug} rem={rem}s ws={ws_text} "
                    f"UP={up_bid} DOWN={down_bid} PTB=- NOW=- "
                    f"PUP={p_show*100:.1f}% CONF={c_show*100:.0f}% WR={wr*100:.1f}%({wt})",
                    flush=True,
                )
        except Exception:
            pass
        time.sleep(TERM_STATUS_INTERVAL)


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>POLYBOT // web-v3.1</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #080c0a; --surface: #0c110e; --surface2: #101812;
      --border: #1a2e20; --border2: #243d2b; --text: #c8ddd0;
      --muted: #5a7a62; --dim: #3a5442; --green: #39e87a;
      --green2: #27a855; --red: #e84040; --red2: #a82727;
      --amber: #e8b339; --cyan: #39d4e8; --up: #39e87a; --down: #e84040;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      background: var(--bg); color: var(--text);
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 13px; height: 100%; overflow-x: hidden;
    }
    body::before {
      content: ''; position: fixed; inset: 0;
      background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
      pointer-events: none; z-index: 9999;
    }
    .terminal { max-width: 1280px; margin: 0 auto; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
    .topbar { display: flex; align-items: center; gap: 0; border: 1px solid var(--border); background: var(--surface); overflow: hidden; }
    .topbar-brand { padding: 6px 14px; font-weight: 700; font-size: 14px; color: var(--green); letter-spacing: 0.15em; border-right: 1px solid var(--border); white-space: nowrap; }
    .topbar-brand span { color: var(--muted); }
    .badges { display: flex; flex: 1; overflow: hidden; }
    .badge { padding: 6px 12px; border-right: 1px solid var(--border); font-size: 12px; white-space: nowrap; color: var(--muted); }
    .badge b { color: var(--text); }
    .badge.ws-live b { color: var(--green); }
    .badge.ws-down b { color: var(--red); }
    .badge.mode-real b { color: var(--amber); }
    .badge.mode-dry b { color: var(--cyan); }
    .grid { display: grid; grid-template-columns: 1fr 320px; gap: 6px; }
    .col-left, .col-right { display: flex; flex-direction: column; gap: 6px; }
    .panel { border: 1px solid var(--border); background: var(--surface); overflow: hidden; }
    .panel-header { padding: 4px 10px; background: var(--surface2); border-bottom: 1px solid var(--border); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--dim); display: flex; align-items: center; gap: 8px; }
    .panel-header::before { content: '//'; color: var(--border2); }
    .ob-table { width: 100%; border-collapse: collapse; }
    .ob-table th { padding: 5px 10px; text-align: left; font-size: 10px; letter-spacing: 0.08em; color: var(--muted); border-bottom: 1px solid var(--border); background: var(--surface2); font-weight: 400; }
    .ob-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 13px; font-weight: 500; }
    .ob-table tr:last-child td { border-bottom: none; }
    .label-up { color: var(--up); font-weight: 700; letter-spacing: 0.05em; }
    .label-down { color: var(--down); font-weight: 700; letter-spacing: 0.05em; }
    .price { font-variant-numeric: tabular-nums; }
    .pos-val { color: var(--amber); }
    .pnl-pos { color: var(--green); }
    .pnl-neg { color: var(--red); }
    .pnl-zero { color: var(--muted); }
    .btn-cashout { padding: 5px 12px; font-family: inherit; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; border: 1px solid var(--border2); background: var(--surface2); color: var(--muted); cursor: pointer; transition: all 0.1s; text-transform: uppercase; }
    .btn-cashout:not(:disabled):hover { border-color: var(--amber); color: var(--amber); background: rgba(232,179,57,0.08); }
    .btn-cashout:disabled { opacity: 0.3; cursor: not-allowed; }
    .btn-cashout.has-pos { border-color: var(--amber); color: var(--amber); }
    .cmd-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px; padding: 8px; }
    .cmd-section { display: flex; flex-direction: column; gap: 4px; }
    .cmd-section-label { font-size: 9px; letter-spacing: 0.1em; color: var(--dim); text-transform: uppercase; padding: 0 2px; }
    .cmd-row { display: flex; gap: 4px; }
    .cmd-btn { flex: 1; padding: 7px 6px; font-family: inherit; font-size: 11px; font-weight: 700; letter-spacing: 0.05em; border: 1px solid var(--border2); background: var(--surface2); color: var(--text); cursor: pointer; transition: all 0.08s; text-transform: uppercase; }
    .cmd-btn:hover { background: var(--border); }
    .cmd-btn:active { transform: scale(0.96); }
    .cmd-btn.buy-up { border-color: var(--green2); color: var(--green); }
    .cmd-btn.buy-up:hover { background: rgba(57,232,122,0.1); }
    .cmd-btn.buy-down { border-color: var(--red2); color: var(--red); }
    .cmd-btn.buy-down:hover { background: rgba(232,64,64,0.1); }
    .cmd-btn.sell { border-color: #4a3a10; color: var(--amber); }
    .cmd-btn.sell:hover { background: rgba(232,179,57,0.1); }
    .cmd-btn.neutral { border-color: var(--border2); color: var(--muted); }
    .cmd-btn.flash { animation: flash 0.2s ease; }
    @keyframes flash { 0% { filter: brightness(2); } 100% { filter: brightness(1); } }
    .input-row { display: flex; gap: 4px; padding: 0 8px 8px; }
    .cmd-input { flex: 1; padding: 7px 10px; font-family: inherit; font-size: 12px; background: var(--bg); color: var(--text); border: 1px solid var(--border2); outline: none; }
    .cmd-input:focus { border-color: var(--green2); }
    .cmd-input::placeholder { color: var(--dim); }
    .send-btn { padding: 7px 16px; font-family: inherit; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; background: var(--green2); color: #000; border: none; cursor: pointer; transition: filter 0.1s; text-transform: uppercase; }
    .send-btn:hover { filter: brightness(1.2); }
    .log-wrap { padding: 8px 10px; max-height: 200px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border2) transparent; }
    .log-line { display: block; line-height: 1.6; font-size: 12px; color: var(--muted); white-space: pre-wrap; word-break: break-all; }
    .log-line.trade { color: var(--amber); }
    .log-line.err { color: var(--red); }
    .log-line.ok { color: var(--green); }
    .oo-wrap { padding: 8px 10px; max-height: 180px; overflow-y: auto; scrollbar-width: thin; }
    .hint { padding: 4px 10px 6px; font-size: 10px; color: var(--dim); line-height: 1.5; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .cmd-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<div class="terminal">
  <div class="topbar">
    <div class="topbar-brand">POLY<span>BOT</span></div>
    <div class="badges">
      <div class="badge"><b id="slug">-</b></div>
      <div class="badge">T <b id="timer">--:--</b></div>
      <div class="badge" id="ws-badge">WS <b id="ws">-</b> <span id="ws-age" style="font-size:10px;color:var(--dim)"></span></div>
      <div class="badge" id="mode-badge">MODE <b id="mode">-</b></div>
      <div class="badge">BAL <b id="bal">-</b></div>
      <div class="badge" style="color:var(--dim)"><b id="ver">-</b></div>
    </div>
  </div>
  <div class="grid">
    <div class="col-left">
      <div class="panel">
        <div class="panel-header">orderbook</div>
        <table class="ob-table">
          <thead><tr><th>SIDE</th><th>BID</th><th>ASK</th><th>POS</th><th>ENTRY</th><th>PNL</th><th>ACTION</th></tr></thead>
          <tbody>
            <tr class="row-up">
              <td class="label-up">UP</td>
              <td class="price" id="up-bid">-</td><td class="price" id="up-ask">-</td>
              <td id="up-pos">-</td><td id="up-entry">-</td>
              <td class="pnl-zero" id="up-pnl">-</td>
              <td><button class="btn-cashout" id="btn-cu" onclick="fire('cu')" disabled>SELL UP</button></td>
            </tr>
            <tr class="row-down">
              <td class="label-down">DOWN</td>
              <td class="price" id="down-bid">-</td><td class="price" id="down-ask">-</td>
              <td id="down-pos">-</td><td id="down-entry">-</td>
              <td class="pnl-zero" id="down-pnl">-</td>
              <td><button class="btn-cashout" id="btn-cd" onclick="fire('cd')" disabled>SELL DOWN</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="panel">
        <div class="panel-header">system log</div>
        <div class="log-wrap" id="log-wrap"><span class="log-line">waiting...</span></div>
      </div>
      <div class="panel">
        <div class="panel-header">trade log</div>
        <div class="log-wrap" id="tlog-wrap"><span class="log-line">no trades yet</span></div>
      </div>
    </div>
    <div class="col-right">
      <div class="panel">
        <div class="panel-header">quick commands</div>
        <div class="cmd-grid">
          <div class="cmd-section">
            <div class="cmd-section-label">BUY</div>
            <div class="cmd-row">
              <button class="cmd-btn buy-up" onclick="fire('bu 1')">BU $1</button>
              <button class="cmd-btn buy-up" onclick="fire('bu 2')">BU $2</button>
            </div>
            <div class="cmd-row">
              <button class="cmd-btn buy-down" onclick="fire('bd 1')">BD $1</button>
              <button class="cmd-btn buy-down" onclick="fire('bd 2')">BD $2</button>
            </div>
          </div>
          <div class="cmd-section">
            <div class="cmd-section-label">SELL</div>
            <div class="cmd-row"><button class="cmd-btn sell" onclick="fire('cu')">SELL UP</button></div>
            <div class="cmd-row"><button class="cmd-btn sell" onclick="fire('cd')">SELL DOWN</button></div>
          </div>
        </div>
        <div class="cmd-grid" style="padding-top:0">
          <div class="cmd-section">
            <div class="cmd-section-label">MISC</div>
            <div class="cmd-row">
              <button class="cmd-btn neutral" onclick="fire('ca')">CANCEL ALL</button>
              <button class="cmd-btn neutral" onclick="fire('r')">REFRESH</button>
            </div>
          </div>
          <div class="cmd-section">
            <div class="cmd-section-label">MODE</div>
            <div class="cmd-row">
              <button class="cmd-btn neutral" onclick="fire('dry on')">DRY ON</button>
              <button class="cmd-btn neutral" onclick="fire('dry off')">DRY OFF</button>
            </div>
          </div>
        </div>
        <div class="hint">bu/bd = buy up/down &nbsp;|&nbsp; cu/cd = cashout up/down<br>lu [price] [usd] = limit buy up &nbsp;|&nbsp; ca = cancel all</div>
        <div class="input-row">
          <input class="cmd-input" id="cmd" placeholder="> e.g. lu 74 1" autocomplete="off"/>
          <button class="send-btn" onclick="submitInput()">SEND</button>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header">open orders <span id="oo-count" style="color:var(--muted);font-size:9px"></span></div>
        <div class="oo-wrap" id="oo"></div>
      </div>
    </div>
  </div>
</div>

<script>
const TICK_MS = 250;
let tickInFlight = false;
let lastSnapshotSeq = 0;

async function getState() {
  const r = await fetch('/api/state', { cache: 'no-store' });
  return r.json();
}
async function sendCmd(cmd) {
  await fetch('/api/cmd', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({cmd})
  });
}
function fire(cmd) {
  const btn = event.currentTarget;
  btn.classList.remove('flash');
  void btn.offsetWidth;
  btn.classList.add('flash');
  sendCmd(cmd);
}
function submitInput() {
  const el = document.getElementById('cmd');
  const v = el.value.trim();
  if (!v) return;
  sendCmd(v);
  el.value = '';
}
document.getElementById('cmd').addEventListener('keydown', e => {
  if (e.key === 'Enter') submitInput();
});
function fmtRem(rem) {
  const m = Math.floor(rem / 60).toString().padStart(2, '0');
  const s = Math.floor(rem % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}
function pnlClass(pnl) {
  if (pnl === '-' || pnl === null) return 'pnl-zero';
  const v = parseFloat(pnl);
  if (isNaN(v) || v === 0) return 'pnl-zero';
  return v > 0 ? 'pnl-pos' : 'pnl-neg';
}
function pnlStr(pnl) {
  if (pnl === '-' || pnl === null) return '-';
  const v = parseFloat(pnl);
  if (isNaN(v)) return '-';
  return (v >= 0 ? '+' : '') + v.toFixed(4);
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function renderLog(lines, wrapId) {
  const wrap = document.getElementById(wrapId);
  const atBottom = wrap.scrollHeight - wrap.scrollTop <= wrap.clientHeight + 20;
  const html = lines.map(l => {
    let cls = 'log-line';
    if (/CASHOUT|SELL|CLOSED|PARTIAL/.test(l)) cls += ' trade';
    else if (/BUY|fill|MKT/.test(l)) cls += ' ok';
    else if (/error|fail|block|stale|no-fill/i.test(l)) cls += ' err';
    return `<span class="${cls}">${escHtml(l)}</span>`;
  }).join('\n');
  wrap.innerHTML = html;
  if (atBottom) wrap.scrollTop = wrap.scrollHeight;
}

async function tick() {
  if (tickInFlight) return;
  tickInFlight = true;
  try {
    const s = await getState();
    const seq = Number(s.snapshot_seq || 0);
    if (seq && seq <= lastSnapshotSeq) return;
    if (seq) lastSnapshotSeq = seq;
    const rem = Math.max(0, s.interval_end - Math.floor(Date.now() / 1000));

    document.getElementById('slug').textContent = s.current_slug || '-';
    document.getElementById('timer').textContent = fmtRem(rem);

    const wsEl = document.getElementById('ws');
    const wsAge = document.getElementById('ws-age');
    const wsBadge = document.getElementById('ws-badge');
    wsEl.textContent = s.ws_ok ? 'LIVE' : 'DOWN';
    wsAge.textContent = s.quote_age_ms != null ? `(${s.quote_age_ms}ms)` : '';
    wsBadge.className = 'badge ' + (s.ws_ok ? 'ws-live' : 'ws-down');

    const modeEl = document.getElementById('mode');
    const modeBadge = document.getElementById('mode-badge');
    modeEl.textContent = s.dry_run ? 'DRY' : 'REAL';
    modeBadge.className = 'badge ' + (s.dry_run ? 'mode-dry' : 'mode-real');

    document.getElementById('bal').textContent = s.balance || '-';
    document.getElementById('ver').textContent = s.version || '-';

    document.getElementById('up-bid').textContent = s.up_bid;
    document.getElementById('up-ask').textContent = s.up_ask;
    document.getElementById('down-bid').textContent = s.down_bid;
    document.getElementById('down-ask').textContent = s.down_ask;
    const upPosEl = document.getElementById('up-pos');
    const downPosEl = document.getElementById('down-pos');
    upPosEl.textContent = s.up_pos_usd;
    downPosEl.textContent = s.down_pos_usd;
    upPosEl.className = s.up_has_pos ? 'pos-val' : '';
    downPosEl.className = s.down_has_pos ? 'pos-val' : '';
    document.getElementById('up-entry').textContent = s.up_entry;
    document.getElementById('down-entry').textContent = s.down_entry;
    const upPnlEl = document.getElementById('up-pnl');
    const downPnlEl = document.getElementById('down-pnl');
    upPnlEl.textContent = pnlStr(s.up_pnl);
    downPnlEl.textContent = pnlStr(s.down_pnl);
    upPnlEl.className = pnlClass(s.up_pnl);
    downPnlEl.className = pnlClass(s.down_pnl);
    const btnCu = document.getElementById('btn-cu');
    const btnCd = document.getElementById('btn-cd');
    btnCu.disabled = !s.up_has_pos;
    btnCd.disabled = !s.down_has_pos;
    btnCu.classList.toggle('has-pos', !!s.up_has_pos);
    btnCd.classList.toggle('has-pos', !!s.down_has_pos);

    renderLog((s.log || []).slice(-40), 'log-wrap');
    renderLog((s.trade_log || []).slice(-20), 'tlog-wrap');

    // PATCH 7: Open Orders render proper rows (bukan raw JSON)
    const ooEl = document.getElementById('oo');
    const ooCount = document.getElementById('oo-count');
    const oo = s.open_orders || [];
    ooCount.textContent = oo.length ? `(${oo.length})` : '';
    if (!oo.length) {
      ooEl.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:6px 0;">No open orders.</div>';
    } else {
      ooEl.innerHTML = oo.map(o => {
        const oid = String(o.id || o.order_id || '');
        const rawSide = String(o.side || o.type || '').toLowerCase();
        const side = rawSide === 'up' || rawSide === 'buy' ? 'BUY'
                   : rawSide === 'down' || rawSide === 'sell' ? 'SELL'
                   : rawSide.toUpperCase();
        const px = o.price ? `${escHtml(String(o.price))}¢` : '?';
        const sz = o.size ? `${escHtml(String(o.size))}sh` : '?';
        const st = String(o.status || '').slice(0, 10);
        const col = side === 'BUY' ? 'var(--green)' : 'var(--red)';
        const bc  = side === 'BUY' ? 'var(--green2)' : 'var(--red2)';
        return `<div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);">
          <span style="font-size:10px;font-weight:700;padding:2px 7px;border:1px solid ${bc};color:${col}">${side}</span>
          <span style="flex:1">
            <div style="font-size:12px;font-weight:700;color:var(--text)">${px} \u00d7 ${sz}</div>
            <div style="font-size:9px;color:var(--muted);margin-top:1px">${escHtml(st)}</div>
          </span>
          <button onclick="sendCmd('co ${oid}')" ${oid?'':'disabled'}
            style="font-family:inherit;font-size:9px;padding:3px 8px;border:1px solid var(--border2);background:var(--surface2);color:var(--muted);cursor:pointer">\u2715</button>
        </div>`;
      }).join('');
    }
  } catch(e) {}
  finally { tickInFlight = false; }
}

setInterval(tick, TICK_MS);
tick();
</script>
</body>
</html>"""


def make_snapshot():
    prob = compute_probability_snapshot()
    lock_open_probability_if_needed(prob)
    up_tok, up_pos, _ = resolve_position_token("up", allow_off_market=True)
    down_tok, down_pos, _ = resolve_position_token("down", allow_off_market=True)
    with state_lock:
        state["snapshot_seq"] = int(state.get("snapshot_seq", 0)) + 1
        snapshot_seq = state["snapshot_seq"]
        up_bid = state["up_bid"]
        down_bid = state["down_bid"]
        up_pnl = "-"
        down_pnl = "-"
        if up_pos:
            up_cur = float(up_bid) if is_valid_price(up_bid) else float(up_pos["entry"])
            up_pnl = round((up_cur - float(up_pos["entry"])) * float(up_pos["shares"]), 4)
        if down_pos:
            down_cur = float(down_bid) if is_valid_price(down_bid) else float(down_pos["entry"])
            down_pnl = round((down_cur - float(down_pos["entry"])) * float(down_pos["shares"]), 4)
        open_orders = state["open_orders"] if state["dry_run"] else state["open_orders_remote"]
        up_ask = state["up_ask"]
        down_ask = state["down_ask"]
        nxt = next_market_quotes()
        target = nxt["target"]
        cur_up_tok = state.get("up_token")
        cur_down_tok = state.get("down_token")
        nxt_up_tok = state.get("next_up_token")
        nxt_down_tok = state.get("next_down_token")
        prev_up_tok = state.get("prev_up_token")
        prev_down_tok = state.get("prev_down_token")
        prev_slug = state.get("prev_slug")
        prev_end = int(state.get("prev_interval_end", 0) or 0)
        prev_up_bid = state.get("prev_up_bid", "-")
        prev_up_ask = state.get("prev_up_ask", "-")
        prev_down_bid = state.get("prev_down_bid", "-")
        prev_down_ask = state.get("prev_down_ask", "-")
        worker_quotes = dict(state.get("worker_quotes", {}) or {})
        view_up_tok = nxt_up_tok if target == "next" else cur_up_tok
        view_down_tok = nxt_down_tok if target == "next" else cur_down_tok
        view_up_bid_raw = nxt["next_up_bid"] if target == "next" else up_bid
        view_up_ask_raw = nxt["next_up_ask"] if target == "next" else up_ask
        view_down_bid_raw = nxt["next_down_bid"] if target == "next" else down_bid
        view_down_ask_raw = nxt["next_down_ask"] if target == "next" else down_ask
        if target == "previous":
            view_up_tok = prev_up_tok
            view_down_tok = prev_down_tok
            view_up_bid_raw = prev_up_bid
            view_up_ask_raw = prev_up_ask
            view_down_bid_raw = prev_down_bid
            view_down_ask_raw = prev_down_ask
        view_up_pos = state["positions"].get(view_up_tok) if view_up_tok else None
        view_down_pos = state["positions"].get(view_down_tok) if view_down_tok else None
        ob_up_pnl = "-"
        ob_down_pnl = "-"
        if view_up_pos:
            up_cur = float(view_up_bid_raw) if is_valid_price(view_up_bid_raw) else float(view_up_pos.get("entry", 0.0))
            ob_up_pnl = round((up_cur - float(view_up_pos.get("entry", 0.0))) * float(view_up_pos.get("shares", 0.0)), 4)
        if view_down_pos:
            down_cur = float(view_down_bid_raw) if is_valid_price(view_down_bid_raw) else float(view_down_pos.get("entry", 0.0))
            ob_down_pnl = round((down_cur - float(view_down_pos.get("entry", 0.0))) * float(view_down_pos.get("shares", 0.0)), 4)
        residual_positions = []
        skip_tokens = {cur_up_tok, cur_down_tok, nxt_up_tok, nxt_down_tok, prev_up_tok, prev_down_tok}
        for tok, pos in state["positions"].items():
            if tok in skip_tokens or is_dust_position(pos):
                continue
            residual_positions.append({
                "token": str(tok)[-8:],
                "side": str(pos.get("side", "")).upper() or "?",
                "shares": round(float(pos.get("shares", 0.0)), 4),
                "entry": to_cent_display(float(pos.get("entry", 0.0))),
                "slug": str(pos.get("slug", "-")),
            })
        residual_positions = residual_positions[-10:]
        positions_view = {}
        for tok, pos in state["positions"].items():
            positions_view[str(tok)] = {
                "side": str(pos.get("side", "")),
                "shares": round(float(pos.get("shares", 0.0)), 6),
                "entry": round(float(pos.get("entry", 0.0)), 6),
                "usd_in": round(float(pos.get("usd_in", 0.0)), 6),
                "slug": str(pos.get("slug", "-")),
            }
        worker_ok = bool(state.get("worker_ok"))
        worker_recent = [str(x) for x in (state.get("worker_recent_events") or [])][-20:]
        worker_ui_enabled = worker_ok and target == "next"
        view_up_worker = worker_quotes.get(str(view_up_tok), {}) if (worker_ui_enabled and view_up_tok) else {}
        view_down_worker = worker_quotes.get(str(view_down_tok), {}) if (worker_ui_enabled and view_down_tok) else {}
        worker_up_bid = view_up_worker.get("best_bid")
        worker_up_ask = view_up_worker.get("best_ask")
        worker_down_bid = view_down_worker.get("best_bid")
        worker_down_ask = view_down_worker.get("best_ask")
        display_ob_up_bid = worker_up_bid if is_valid_price(worker_up_bid) else view_up_bid_raw
        display_ob_up_ask = worker_up_ask if is_valid_price(worker_up_ask) else view_up_ask_raw
        display_ob_down_bid = worker_down_bid if is_valid_price(worker_down_bid) else view_down_bid_raw
        display_ob_down_ask = worker_down_ask if is_valid_price(worker_down_ask) else view_down_ask_raw
        combined_log = (state["log"][-35:] + [f"[RW] {x}" for x in worker_recent])[-50:]

        return {
            "version": WEB_UI_VERSION,
            "snapshot_seq": snapshot_seq,
            "up_bid": to_cent_display(up_bid),
            "up_ask": to_cent_display(up_ask),
            "down_bid": to_cent_display(down_bid),
            "down_ask": to_cent_display(down_ask),
            "up_token": cur_up_tok,
            "down_token": cur_down_tok,
            "next_up_token": nxt_up_tok,
            "next_down_token": nxt_down_tok,
            "prev_up_token": prev_up_tok,
            "prev_down_token": prev_down_tok,
            "positions": positions_view,
            "interval_end": state["interval_end"],
            "current_slug": state["current_slug"],
            "ws_ok": state["ws_ok"],
            "last_ws": state["last_ws"],
            "quote_age_ms": max(0, int((time.time() - float(state.get("last_quote_ts", 0))) * 1000)),
            "balance": state["balance"],
            "session_pnl": round(
                float(
                    state.get(
                        "session_pnl_dry" if state.get("dry_run", True) else "session_pnl_real",
                        0.0,
                    )
                ),
                4,
            ),
            "session_pnl_dry": round(float(state.get("session_pnl_dry", 0.0)), 4),
            "session_pnl_real": round(float(state.get("session_pnl_real", 0.0)), 4),
            "dry_run": state["dry_run"],
            "open_orders": open_orders[-8:],
            "log": combined_log,
            "trade_log": state["trade_log"][-80:],
            "up_pos_usd": f"${up_pos['usd_in']:.2f} / {float(up_pos['shares']):.4f}sh" if up_pos else "-",
            "down_pos_usd": f"${down_pos['usd_in']:.2f} / {float(down_pos['shares']):.4f}sh" if down_pos else "-",
            "up_entry": to_cent_display(up_pos["entry"]) if up_pos else "-",
            "down_entry": to_cent_display(down_pos["entry"]) if down_pos else "-",
            "up_pnl": up_pnl,
            "down_pnl": down_pnl,
            "up_has_pos": bool(up_pos),
            "down_has_pos": bool(down_pos),
            "btc_now": state.get("btc_price_now"),
            "btc_ptb": state.get("btc_price_to_beat"),
            "btc_price_now": state.get("btc_price_now"),
            "btc_price_to_beat": state.get("btc_price_to_beat"),
            "prob_up": round(float(state.get("prob_open_up") if state.get("prob_open_up") is not None else prob.get("p_up", 0.5)), 6),
            "prob_down": round(float(state.get("prob_open_down") if state.get("prob_open_down") is not None else prob.get("p_down", 0.5)), 6),
            "prob_confidence": round(float(state.get("prob_open_confidence") if state.get("prob_open_confidence") is not None else prob.get("confidence", 0.0)), 6),
            "prob_market": round(float(prob.get("p_market", 0.5)), 6),
            "prob_ptb": round(float(prob.get("p_ptb", 0.5)), 6),
            "prob_micro": round(float(prob.get("p_micro", 0.5)), 6),
            "prob_win_total": int(state.get("prob_win_total", 0)),
            "prob_win_wins": int(state.get("prob_win_wins", 0)),
            "prob_win_losses": int(state.get("prob_win_losses", 0)),
            "prob_win_rate": float(state.get("prob_win_rate", 0.0)),
            "prob_last_result": str(state.get("prob_last_result", "-")),
            "prob_open_slug": str(state.get("prob_open_slug", "-")),
            "prob_open_at": int(state.get("prob_open_at", 0)),
            "target_market": nxt["target"],
            "next_slug": nxt["next_slug"],
            "next_ready": nxt["next_ready"],
            "next_up_bid": to_cent_display(nxt["next_up_bid"]),
            "next_up_ask": to_cent_display(nxt["next_up_ask"]),
            "next_down_bid": to_cent_display(nxt["next_down_bid"]),
            "next_down_ask": to_cent_display(nxt["next_down_ask"]),
            "next_interval_end": nxt["next_interval_end"],
            "prev_slug": prev_slug or "-",
            "prev_ready": bool(
                prev_up_tok and prev_down_tok
                and is_valid_price(prev_up_bid) and is_valid_price(prev_up_ask)
                and is_valid_price(prev_down_bid) and is_valid_price(prev_down_ask)
            ),
            "prev_up_bid": to_cent_display(prev_up_bid),
            "prev_up_ask": to_cent_display(prev_up_ask),
            "prev_down_bid": to_cent_display(prev_down_bid),
            "prev_down_ask": to_cent_display(prev_down_ask),
            "prev_interval_end": prev_end,
            "ob_view": target,
            "ob_up_bid": to_cent_display(display_ob_up_bid),
            "ob_up_ask": to_cent_display(display_ob_up_ask),
            "ob_down_bid": to_cent_display(display_ob_down_bid),
            "ob_down_ask": to_cent_display(display_ob_down_ask),
            "ob_up_pos_usd": f"${view_up_pos['usd_in']:.2f} / {float(view_up_pos['shares']):.4f}sh" if view_up_pos else "-",
            "ob_down_pos_usd": f"${view_down_pos['usd_in']:.2f} / {float(view_down_pos['shares']):.4f}sh" if view_down_pos else "-",
            "ob_up_entry": to_cent_display(view_up_pos["entry"]) if view_up_pos else "-",
            "ob_down_entry": to_cent_display(view_down_pos["entry"]) if view_down_pos else "-",
            "ob_up_pnl": ob_up_pnl,
            "ob_down_pnl": ob_down_pnl,
            "ob_up_has_pos": bool(view_up_pos),
            "ob_down_has_pos": bool(view_down_pos),
            "residual_positions": residual_positions,
            "residual_up_has_pos": any(r.get("side") == "UP" for r in residual_positions),
            "residual_down_has_pos": any(r.get("side") == "DOWN" for r in residual_positions),
            "worker_enabled": PTB_EXECUTION_WORKER,
            "worker_ok": worker_ok,
        }


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, code=200):
        raw = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _html(self, html, code=200):
        raw = html.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        remote_ip = self.client_address[0] if self.client_address else ""
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(load_frontend_html())
            return
        if parsed.path == "/api/state":
            if not is_local_client(remote_ip):
                self._json({"ok": False, "error": "forbidden"}, 403)
                return
            self._json(make_snapshot())
            return
        if parsed.path == "/api/chart":
            if not is_local_client(remote_ip):
                self._json({"ok": False, "error": "forbidden"}, 403)
                return
            qs = parse_qs(parsed.query or "")
            tf = str((qs.get("tf") or ["1m"])[0] or "1m")
            self._json(make_chart_snapshot(tf))
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/cmd":
            try:
                remote_ip = self.client_address[0] if self.client_address else ""
                if not is_local_client(remote_ip):
                    self._json({"ok": False, "error": "forbidden"}, 403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_CMD_BODY_BYTES:
                    self._json({"ok": False, "error": "payload too large"}, 413)
                    return
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                cmd = str(data.get("cmd", "")).strip()
                if cmd:
                    try:
                        cmd_queue.put_nowait(cmd)
                    except queue.Full:
                        self._json({"ok": False, "error": "busy"}, 429)
                        return
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "empty cmd"}, 400)
            except Exception:
                self._json({"ok": False, "error": "bad request"}, 400)
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, _format, *_args):
        return


def shutdown(*_args):
    with state_lock:
        state["running"] = False
    print("Shutting down...")


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"Mode: {'DRY' if state['dry_run'] else 'REAL'}")
    log(f"Market BUY order type: {MARKET_BUY_ORDER_TYPE_LABEL}")
    if not smart_fetch_tokens():
        print("No active market. Exit.")
        return

    log("[CHART] Fetching historical BTC candles from Binance...")
    init_chart_history()

    threading.Thread(target=start_ws, daemon=True).start()
    threading.Thread(target=start_user_ws, daemon=True).start()
    threading.Thread(target=start_rtds, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=market_watcher, daemon=True).start()
    threading.Thread(target=fetch_balance, daemon=True).start()
    threading.Thread(target=sync_open_orders, daemon=True).start()
    threading.Thread(target=reconcile_positions, daemon=True).start()
    threading.Thread(target=chart_loop, daemon=True).start()
    if PTB_EXECUTION_WORKER:
        threading.Thread(target=worker_state_loop, daemon=True).start()
    threading.Thread(target=command_loop, daemon=True).start()
    threading.Thread(target=terminal_status_loop, daemon=True).start()

    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    server.timeout = 0.5
    print(f"Web UI {WEB_UI_VERSION} running at http://{WEB_HOST}:{WEB_PORT}")
    while state["running"]:
        server.handle_request()
    server.server_close()
    print("Bye.")


if __name__ == "__main__":
    main()
