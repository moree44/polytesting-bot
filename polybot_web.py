#!/usr/bin/env python3
"""
Polymarket BTC 5m Bot - Local Web UI
Run: python polybot_web.py
Open: http://127.0.0.1:8787
"""

import json
import os
import queue
import re
import signal
import threading
import time
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
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
CENT_DECIMALS = max(0, min(2, int(os.getenv("CENT_DECIMALS", "0"))))
MIN_MARKET_TIME_LEFT = int(os.getenv("MIN_MARKET_TIME_LEFT", "45"))
GTC_FALLBACK_TIMEOUT = int(os.getenv("GTC_FALLBACK_TIMEOUT", "0"))
POSITION_SYNC_GRACE = int(os.getenv("POSITION_SYNC_GRACE", "20"))
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
BUY_CMD_GUARD_SEC = max(0.3, min(3.0, float(os.getenv("BUY_CMD_GUARD_SEC", "1.2"))))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))
WEB_UI_VERSION = "web-v3.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_INDEX = os.path.join(BASE_DIR, "webui", "index.html")

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
    "up_bid": "-",
    "up_ask": "-",
    "down_bid": "-",
    "down_ask": "-",
    "interval_end": 0,
    "current_slug": "-",
    "target_market": "current",
    "current_market_id": "",
    "next_slug": None,
    "next_interval_end": 0,
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
    "running": True,
    "dry_run": DRY_RUN,
    "last_real_action_ts": 0.0,
    "snapshot_seq": 0,
    "btc_price_now": None,
    "btc_price_to_beat": None,
}

cmd_queue: "queue.Queue[str]" = queue.Queue()
ws_app = None
_ws_connecting = False      # guard: prevent duplicate WS connections
_ws_lock = threading.Lock()
_ws_reconnect_delay = 3.0   # default reconnect delay after WS close
last_market_switch = 0
active_tokens = set()
market_queues = {}
market_queue_lock = threading.Lock()
next_quote_cache = {}  # token_id -> (bid:str, ask:str, ts:int)
_rtds_connecting = False
_rtds_lock = threading.Lock()
_cl_lock = threading.Lock()
_cl_ring = []  # list[(ts_seconds:int, price:float)]
_CL_RING_MAX = 90
_ptb_web_last_try = {}  # slug -> unix_ts
_buy_cmd_lock = threading.Lock()
_last_buy_cmd = {"sig": "", "ts": 0.0}


def load_frontend_html() -> str:
    try:
        with open(FRONTEND_INDEX, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return HTML


def fmt_usd(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "-"


def slug_start_ts(slug: str) -> int:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except Exception:
        return 0


def cl_price_at(target_ts: int):
    with _cl_lock:
        if not _cl_ring:
            return None
        # Strict mode: use the first sample at/after interval start.
        # This avoids using pre-boundary samples that can skew PTB in fast moves.
        for ts, px in _cl_ring:
            if ts >= target_ts and (ts - target_ts) <= PTB_MAX_DRIFT_SEC:
                return px
        return None


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


def upsert_position_merge(token_id: str, side: str, add_shares: float, add_notional: float):
    if add_shares <= 0 or add_notional <= 0:
        return
    with state_lock:
        slug = state.get("current_slug", "-")
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
        if cent < 1 or cent > 100:
            raise ValueError("cent out of range")
        return round(cent / 100.0, 4)
    p = float(s)
    if p <= 0 or p > 1:
        raise ValueError("price out of range")
    return round(p, 4)


def reset_orderbook():
    with state_lock:
        # Only reset WS timestamp, keep old prices visible until new data arrives.
        # Showing stale prices is better than showing "-" during the dead zone.
        state["last_ws"] = 0
        # Do NOT reset last_quote_ts - prevents poll_loop from spamming REST


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
        # Determine which side this token belongs to (current or next)
        if asset_id == state["up_token"]:
            side = "up"
        elif asset_id == state["down_token"]:
            side = "down"
        elif asset_id == state["next_up_token"]:
            side = "next_up"
        elif asset_id == state["next_down_token"]:
            side = "next_down"
        else:
            return
        if side in ("next_up", "next_down"):
            # Warm cache for upcoming market so switch can reuse live WS quotes.
            if is_valid_price(bid_s) and is_valid_price(ask_s):
                next_quote_cache[asset_id] = (bid_s, ask_s, int(time.time()))
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

        # Fetch UP dan DOWN token secara paralel — setengah waktu dibanding sequential
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


def next_market_quotes():
    with state_lock:
        nxt_up = state.get("next_up_token")
        nxt_down = state.get("next_down_token")
        nxt_slug = state.get("next_slug")
        nxt_end = int(state.get("next_interval_end", 0) or 0)
        target = str(state.get("target_market", "current"))
    up_bid, up_ask = get_cached_quote(nxt_up, max_age=25) if nxt_up else (None, None)
    down_bid, down_ask = get_cached_quote(nxt_down, max_age=25) if nxt_down else (None, None)
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


def smart_fetch_tokens() -> bool:
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

    near_candidates = [c for c in candidates if c["rem"] <= 390]
    pool = near_candidates if near_candidates else candidates
    best = max(pool, key=lambda c: c["score"])
    ids = best["ids"]
    with state_lock:
        old_up = state["up_token"]
        state["up_token"] = ids[0]
        state["down_token"] = ids[1]
        state["interval_end"] = best["end_ts"]
        state["current_slug"] = best["slug"]
        state["current_market_id"] = str(best.get("market_id", ""))
    active_tokens = {ids[0], ids[1]}
    if old_up != ids[0]:
        reset_orderbook()
        up_cache_bid, up_cache_ask = get_cached_quote(ids[0], max_age=150)
        down_cache_bid, down_cache_ask = get_cached_quote(ids[1], max_age=150)

        # Seed prices: prefer live WS data already buffered for next tokens,
        # fall back to REST snapshot from smart_fetch_tokens
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
                # Recreate WS on switch. This tends to recover the same
                # quote cadence as a fresh startup better than in-place resubscribe.
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
        threading.Thread(target=immediate_poll, daemon=True).start()
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
    """
    Fetch token IDs for the next 5m market bucket and subscribe WS early.
    Does NOT switch the active market — only warms up the WS connection
    so data is already streaming when the real switch happens at rem<=30.
    """
    global active_tokens
    now = int(time.time())
    # Next bucket = current bucket + 300s
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
            # Skip if next market is same as current
            if ids[0] == cur_up:
                return
            state["next_up_token"] = ids[0]
            state["next_down_token"] = ids[1]
            state["next_slug"] = slug
            state["next_interval_end"] = next_t + 300
        # Add next tokens to active_tokens so set_best doesn't discard their data
        # but they won't affect displayed prices since asset_id != up_token/down_token
        active_tokens = active_tokens | {ids[0], ids[1]}
        # Subscribe WS to current + next tokens
        if ws_app:
            try:
                ws_subscribe(ws_app)
                log(f"Pre-subscribed next market: {slug}")
            except Exception:
                pass
    except Exception:
        pass
def market_watcher():
    _prefetched_for = 0  # track which bucket we already prefetched
    while state["running"]:
        with state_lock:
            rem = state["interval_end"] - int(time.time())
            cur_end = state["interval_end"]

        # Pre-subscribe WS to next market at 90s remaining
        # so WS is already warm when the real switch happens
        if rem <= 90 and cur_end != _prefetched_for:
            _prefetched_for = cur_end
            threading.Thread(target=prefetch_next_market, daemon=True).start()

        # Real switch
        if rem <= 30 or (int(time.time()) - last_market_switch) > 600:
            smart_fetch_tokens()
            # Clear next tokens after switch
            with state_lock:
                state["next_up_token"] = None
                state["next_down_token"] = None
                state["next_slug"] = None
                state["next_interval_end"] = 0

        time.sleep(10)


def ws_subscribe(ws):
    with state_lock:
        up = state["up_token"]
        down = state["down_token"]
        next_up = state["next_up_token"]
        next_down = state["next_down_token"]
    ids = [t for t in [up, down, next_up, next_down] if t]
    ids = list(dict.fromkeys(ids))  # deduplicate, preserve order
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

    # Guard: if a connection attempt is already in progress, skip
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
            # Release guard before scheduling reconnect
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
        # Always release guard even if run_forever throws
        with _ws_lock:
            _ws_connecting = False


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

        # WS watchdog: silent >5min despite ws_ok=True
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
                            # During switch warm-up, avoid flicker by not replacing
                            # a good quote with temporary placeholder snapshots.
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

        # PERF: sleep lebih pendek agar harga lebih reaktif
        if switch_pending:
            time.sleep(0.3)
        elif has_position:
            time.sleep(0.2)  # was 0.7
        else:
            time.sleep(1.0)  # was 2.0


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


def sync_open_orders():
    while state["running"]:
        try:
            with state_lock:
                dry = state["dry_run"]
            if not dry:
                orders = client.get_orders()
                with state_lock:
                    state["open_orders_remote"] = orders if isinstance(orders, list) else []
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
                up_ask = state.get("up_ask")
                down_ask = state.get("down_ask")
            tracked = {
                up_tok: ("up", up_ask),
                down_tok: ("down", down_ask),
            }
            for tok, (side, ask_s) in tracked.items():
                if not tok:
                    continue
                if in_grace:
                    # Avoid false "recovery" from stale balance reads right after real actions.
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
                    has_side = any(str(p.get("side", "")) == side for p in state["positions"].values())
                if not has_local:
                    if has_side:
                        continue
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
                            "slug": state.get("current_slug", "-"),
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
                        # During grace window, keep local execution result authoritative.
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


def market_buy(side: str, usd: float):
    with state_lock:
        tok = state["up_token"] if side == "up" else state["down_token"]
        ask = state["up_ask"] if side == "up" else state["down_ask"]
        bid = state["up_bid"] if side == "up" else state["down_bid"]
        dry = state["dry_run"]
        last_ws = state["last_ws"]
        last_action = state["last_real_action_ts"]
    if not tok:
        log("No token loaded")
        return
    if dry:
        entry = float(ask) if is_valid_price(ask) else 0.5
        shares = round(usd / max(entry, 0.01), 4)
        upsert_position_merge(tok, side, shares, usd)
        log(f"[DRY] MKT {side.upper()} ${usd} @ {entry}")
        log_trade(f"BUY {side.upper()} DRY fill@{to_cent_display(entry)} sh={shares:.4f} usd=${usd:.2f}")
        return
    try:
        usd = norm_usd(usd)
        if usd < MIN_ORDER_USD:
            log(f"Blocked entry: amount too small (<${MIN_ORDER_USD})")
            return
        now = time.time()
        if not dry and (now - last_action) < 1.2:
            log("Blocked entry: action cooldown")
            return
        started_at = int(time.time())
        pre_shares = get_available_shares(tok)

        # PERF: skip sample_top_prices jika quote WS masih fresh (<1 detik)
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
                return
        if not is_valid_price(ask):
            log("Blocked entry: invalid ask")
            return
        with state_lock:
            rem = max(0, state["interval_end"] - int(time.time()))
        if rem < MIN_MARKET_TIME_LEFT:
            log(f"Blocked entry: market expires in {rem}s (<{MIN_MARKET_TIME_LEFT}s)")
            return
        ask_v = float(ask)
        bid_v = float(bid) if is_valid_price(bid) else None
        if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
            log("Blocked entry: placeholder ask (>=98c)")
            return
        if ask_v * 100 > MAX_ENTRY_CENT:
            log(f"Blocked entry: ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c")
            return
        buy_limit = min(round(ask_v * (1 + ENTRY_SLIPPAGE_BPS / 10000.0), 2), 0.99)
        mo = MarketOrderArgs(token_id=tok, amount=usd, side=BUY)
        signed = client.create_market_order(mo)
        try:
            resp = client.post_order(signed, OrderType.FAK)
        except Exception as e:
            msg = str(e)[:250]
            with state_lock:
                # Prevent immediate repeat-click when exchange status is ambiguous.
                state["last_real_action_ts"] = time.time()
            entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd)
            if shares > 0 and entry is not None:
                add_notional = round(float(entry) * float(shares), 4)
                upsert_position_merge(tok, side, shares, add_notional)
                log(f"MKT {side.upper()} api-uncertain but filled@{to_cent_display(entry)} shares={shares}")
                log_trade(f"BUY {side.upper()} uncertain-api fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
                return
            log(f"BUY {side.upper()} api-error no fill confirmed: {msg}")
            return
        status = resp.get("status", "?")
        oid = str(resp.get("id", ""))[:10]
        log(f"BUY {side.upper()} ask={to_cent_display(ask_v)} usd=${usd:.2f} lim={to_cent_display(buy_limit)}")
        with state_lock:
            state["last_real_action_ts"] = time.time()
        entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd)
        if shares > 0 and entry is not None:
            add_notional = round(float(entry) * float(shares), 4)
            upsert_position_merge(tok, side, shares, add_notional)
            if str(status).lower() in ("matched", "filled"):
                log(f"MKT {side.upper()} ${usd} fill@{to_cent_display(entry)} shares={shares} [{oid}]")
            else:
                log(f"MKT {side.upper()} ${usd} partial@{to_cent_display(entry)} shares={shares} [{status}]")
            log_trade(f"BUY {side.upper()} fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
        else:
            if not STRICT_EXECUTION:
                gtc_px, gtc_shares, gtc_oid = try_gtc_fallback_buy(tok, usd, ask_v)
                if gtc_shares > 0 and gtc_px is not None:
                    add_notional = round(float(gtc_shares) * float(gtc_px), 4)
                    upsert_position_merge(tok, side, round(gtc_shares, 4), add_notional)
                    log(f"GTC BUY {side.upper()} fill@{to_cent_display(gtc_px)} shares={gtc_shares:.4f} [{(gtc_oid or '')[:10]}]")
                    log_trade(f"BUY {side.upper()} GTC fill@{to_cent_display(gtc_px)} sh={gtc_shares:.4f} usd~${add_notional:.2f}")
                    return
            log(f"MKT {side.upper()} no-fill ask={to_cent_display(ask_v)} usd=${usd:.2f} [{status}]")
    except Exception as e:
        log(str(e)[:250])


def market_buy_next(side: str, usd: float):
    with state_lock:
        tok = state.get("next_up_token") if side == "up" else state.get("next_down_token")
        nxt_end = int(state.get("next_interval_end", 0) or 0)
        dry = state["dry_run"]
    if not tok:
        log(f"NEXT market token not ready for {side.upper()}")
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
        return
    ask_v = float(ask_s)
    bid_v = float(bid_s) if is_valid_price(bid_s) else None
    if ask_v >= 0.98 and bid_v is not None and bid_v <= 0.02:
        log(f"Blocked NEXT entry: placeholder ask {side.upper()}")
        return
    if ask_v * 100 > MAX_ENTRY_CENT:
        log(f"Blocked NEXT entry: ask {ask_v*100:.0f}c > limit {MAX_ENTRY_CENT:.0f}c")
        return
    rem = max(0, nxt_end - int(time.time())) if nxt_end > 0 else 999
    if rem < MIN_MARKET_TIME_LEFT:
        log(f"Blocked NEXT entry: market expires in {rem}s (<{MIN_MARKET_TIME_LEFT}s)")
        return
    if dry:
        usd = norm_usd(usd)
        if usd < MIN_ORDER_USD:
            log(f"Blocked NEXT entry: amount too small (<${MIN_ORDER_USD})")
            return
        shares = round(usd / max(ask_v, 0.01), 4)
        upsert_position_merge(tok, side, shares, usd)
        log(f"[DRY] NEXT MKT {side.upper()} ${usd} @ {ask_v}")
        log_trade(f"BUY NEXT {side.upper()} DRY fill@{to_cent_display(ask_v)} sh={shares:.4f} usd=${usd:.2f}")
        return
    try:
        usd = norm_usd(usd)
        if usd < MIN_ORDER_USD:
            log(f"Blocked NEXT entry: amount too small (<${MIN_ORDER_USD})")
            return
        started_at = int(time.time())
        pre_shares = get_available_shares(tok)
        mo = MarketOrderArgs(token_id=tok, amount=usd, side=BUY)
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FAK)
        status = resp.get("status", "?")
        oid = str(resp.get("id", ""))[:10]
        with state_lock:
            state["last_real_action_ts"] = time.time()
        entry, shares = infer_buy_fill(tok, started_at, pre_shares, usd)
        if shares > 0 and entry is not None:
            add_notional = round(float(entry) * float(shares), 4)
            upsert_position_merge(tok, side, shares, add_notional)
            log(f"NEXT MKT {side.upper()} ${usd} fill@{to_cent_display(entry)} shares={shares} [{oid or status}]")
            log_trade(f"BUY NEXT {side.upper()} fill@{to_cent_display(entry)} sh={shares:.4f} usd~${add_notional:.2f}")
        else:
            log(f"NEXT MKT {side.upper()} no-fill ask={to_cent_display(ask_v)} usd=${usd:.2f} [{status}]")
    except Exception as e:
        log(f"NEXT BUY {side.upper()} err: {str(e)[:200]}")


def limit_buy(side: str, price: float, usd: float):
    with state_lock:
        tok = state["up_token"] if side == "up" else state["down_token"]
        dry = state["dry_run"]
    if not tok:
        log("No token loaded")
        return
    if dry:
        usd = norm_usd(usd)
        size = norm_size(usd / max(price, 0.01))
        oid = f"dry-{int(time.time())}"[-10:]
        with state_lock:
            state["open_orders"].append({"id": oid, "side": side, "price": price, "size": size, "status": "dry"})
        log(f"[DRY] LIMIT {side.upper()} ${usd} @ {price} [{oid}]")
        return
    try:
        usd = norm_usd(usd)
        size = norm_size(usd / max(price, 0.01))
        oa = OrderArgs(token_id=tok, price=price, size=size, side=BUY)
        signed = client.create_order(oa)
        resp = client.post_order(signed, OrderType.GTC)
        oid = str(resp.get("id", ""))[:10]
        with state_lock:
            state["open_orders"].append({"id": oid, "side": side, "price": price, "size": size, "status": resp.get("status", "?")})
        log(f"LIMIT {side.upper()} ${usd} @ {price} [{oid}]")
    except Exception as e:
        log(str(e)[:250])


def limit_sell(side: str, price: float, shares: float, target_market: str = None):
    if target_market in ("current", "next"):
        tok, pos, off_market = resolve_position_token_for_target(side, target_market)
    else:
        tok, pos, off_market = resolve_position_token(side, allow_off_market=True)
    with state_lock:
        dry = state["dry_run"]
    if not tok or not pos:
        log(f"No {side.upper()} position for limit sell")
        return
    shares = norm_size(shares)
    if shares <= 0:
        log("Invalid sell size")
        return
    if dry:
        sell_sz = min(shares, float(pos.get("shares", 0.0)))
        if sell_sz < MIN_ORDER_SHARES or (sell_sz * max(price, 0.0)) < MIN_ORDER_USD:
            log(f"Blocked LIMIT SELL {side.upper()}: dust size")
            return
        oid = f"dry-s-{int(time.time())}"[-10:]
        with state_lock:
            state["open_orders"].append({
                "id": oid,
                "side": side,
                "price": price,
                "size": sell_sz,
                "status": "dry-sell",
                "type": "sell_limit",
            })
        log(f"[DRY] LIMIT SELL {side.upper()} {sell_sz} @ {price} [{oid}]")
        return
    try:
        available = get_available_shares(tok)
        sell_sz = min(shares, available)
        if sell_sz < MIN_ORDER_SHARES or (sell_sz * max(price, 0.0)) < MIN_ORDER_USD:
            log(f"Blocked LIMIT SELL {side.upper()}: dust or insufficient shares")
            return
        oa = OrderArgs(token_id=tok, price=price, size=norm_size(sell_sz), side=SELL)
        signed = client.create_order(oa)
        resp = client.post_order(signed, OrderType.GTC)
        oid = str(resp.get("id", ""))[:10]
        with state_lock:
            state["open_orders"].append({
                "id": oid,
                "side": side,
                "price": price,
                "size": norm_size(sell_sz),
                "status": resp.get("status", "?"),
                "type": "sell_limit",
            })
        log(f"LIMIT SELL {side.upper()} {sell_sz:.4f} @ {price} [{oid}]")
    except Exception as e:
        log(str(e)[:250])


def cash_out(side: str, target_market: str = None):
    if target_market in ("current", "next"):
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
                bid_s = state.get("next_up_bid") if side == "up" else state.get("next_down_bid")
            else:
                bid_s = state["up_bid"] if side == "up" else state["down_bid"]
        bid = float(bid_s) if is_valid_price(bid_s) else pos["entry"]
        pnl = (bid - pos["entry"]) * pos["shares"]
        with state_lock:
            state["positions"].pop(tok, None)
            state["open_orders"] = [o for o in state["open_orders"] if o["side"] != side]
        log(f"[DRY] CASHOUT {side.upper()} PnL {pnl:+.4f}")
        log_trade(f"SELL {side.upper()} DRY pnl={pnl:+.4f}")
        return
    attempts = 2 if STRICT_EXECUTION else 5
    for attempt in range(1, attempts + 1):
        # Refresh token+position every attempt in case market switched mid-cashout.
        tok, pos, _ = resolve_position_token(side, allow_off_market=True)
        if not pos:
            log(f"CASHOUT {side.upper()} position cleared after partial")
            return
        try:
            with state_lock:
                last_action = state["last_real_action_ts"]
            if (time.time() - last_action) < 1.0:
                time.sleep(1.0)
            started_at = int(time.time())

            # PERF: skip sample_top_prices jika quote WS masih fresh (<1 detik)
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
                    log(
                        f"CASHOUT {side.upper()} balance read unstable ({available:.6f}sh), "
                        f"retry {attempt}/{attempts}..."
                    )
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
                else:
                    log(
                        f"CASHOUT {side.upper()} skipped sell_size={sell_size:.6f}sh "
                        f"(min={MIN_ORDER_SHARES:.4f}), keep position"
                    )
                return
            if sell_size <= 0:
                if attempt < attempts:
                    log(f"CASHOUT {side.upper()} waiting balance {attempt}/{attempts}...")
                    time.sleep(2)
                    continue
                log(f"No onchain {side.upper()} shares available to cashout")
                return
            # PATCH 6: slippage naik 150bps per retry
            slip_bps = EXIT_SLIPPAGE_BPS + ((attempt - 1) * 150)
            sprice = round(max(bid * (1 - slip_bps / 10000.0), 0.01), 2)
            tok_short = str(tok)[-8:] if tok else "-"
            log(
                f"SELL {side.upper()} submit bid={to_cent_display(bid)} "
                f"lim={to_cent_display(sprice)} size={sell_size:.4f} tok=*{tok_short}"
            )
            oa = OrderArgs(token_id=tok, price=sprice, size=sell_size, side=SELL)
            signed = client.create_order(oa)
            resp = client.post_order(signed, OrderType.FAK)
            with state_lock:
                state["last_real_action_ts"] = time.time()
            fills = []
            for _ in range(3):
                time.sleep(0.4)
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
                log(
                    f"CASHOUT {side.upper()} sync adjust chain_rem={remaining:.6f} "
                    f"local_before={pos_shares_before:.6f} sold={sold:.6f} "
                    f"local_rem={local_expected_rem:.6f}"
                )
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
                log(f"CASHOUT {side.upper()} CLOSED sold={sold} px={to_cent_display(fill_px)} PnL {pnl:+.4f} | {resp.get('status', '?')}")
                log_trade(f"SELL {side.upper()} CLOSED sh={sold:.4f} px={to_cent_display(fill_px)} pnl={pnl:+.4f}")
            else:
                log(f"CASHOUT {side.upper()} PARTIAL sold={sold} rem={rem_state} px={to_cent_display(fill_px)} | {resp.get('status', '?')}")
                log_trade(f"SELL {side.upper()} PARTIAL sh={sold:.4f} rem={rem_state:.4f} px={to_cent_display(fill_px)} pnl={pnl:+.4f}")
            return
        except Exception as e:
            msg = str(e)[:250]
            if attempt < attempts and "not enough balance / allowance" in msg.lower():
                log(f"CASHOUT {side.upper()} retry {attempt}/{attempts}...")
                time.sleep(2)
                continue
            if attempt < attempts and "no orders found" in msg.lower():
                log(f"CASHOUT {side.upper()} FAK no-match, retry fresh price {attempt}/{attempts}...")
                time.sleep(0.3)
                continue
            if attempt < attempts and ("request exception" in msg.lower() or "timed out" in msg.lower()):
                log(f"CASHOUT {side.upper()} transport retry {attempt}/{attempts}...")
                time.sleep(0.8)
                continue
            log(msg)
            return


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
        log("All orders cancelled")
    except Exception as e:
        log(str(e)[:80])


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


def infer_buy_fill(token_id: str, since_ts: int, pre_shares: float, usd: float):
    fills = []
    for _ in range(3):
        time.sleep(0.4)
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


def current_market_key() -> str:
    with state_lock:
        slug = state.get("current_slug") or ""
    return slug or "default"


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
            raw = cmd_queue.get(timeout=0.2).strip()
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
                with state_lock:
                    target = state.get("target_market", "current")
                if target == "next":
                    enqueue_market_task(current_market_key(), market_buy_next, "up", usd)
                    continue
                if not allow_buy_command("up", usd):
                    log(f"Blocked duplicate BUY UP cmd (<{BUY_CMD_GUARD_SEC:.1f}s)")
                    continue
                enqueue_market_task(current_market_key(), market_buy, "up", usd)
            elif cmd == "bd" and len(parts) == 2:
                usd = float(parts[1])
                with state_lock:
                    target = state.get("target_market", "current")
                if target == "next":
                    enqueue_market_task(current_market_key(), market_buy_next, "down", usd)
                    continue
                if not allow_buy_command("down", usd):
                    log(f"Blocked duplicate BUY DOWN cmd (<{BUY_CMD_GUARD_SEC:.1f}s)")
                    continue
                enqueue_market_task(current_market_key(), market_buy, "down", usd)
            elif cmd == "bnu" and len(parts) == 2:
                enqueue_market_task(current_market_key(), market_buy_next, "up", float(parts[1]))
            elif cmd == "bnd" and len(parts) == 2:
                enqueue_market_task(current_market_key(), market_buy_next, "down", float(parts[1]))
            elif cmd == "lu" and len(parts) == 3:
                enqueue_market_task(current_market_key(), limit_buy, "up", parse_limit_price(parts[1]), float(parts[2]))
            elif cmd == "ld" and len(parts) == 3:
                enqueue_market_task(current_market_key(), limit_buy, "down", parse_limit_price(parts[1]), float(parts[2]))
            elif cmd == "su" and len(parts) == 3:
                with state_lock:
                    target = state.get("target_market", "current")
                enqueue_market_task(
                    current_market_key(),
                    limit_sell,
                    "up",
                    parse_limit_price(parts[1]),
                    float(parts[2]),
                    target,
                )
            elif cmd == "sd" and len(parts) == 3:
                with state_lock:
                    target = state.get("target_market", "current")
                enqueue_market_task(
                    current_market_key(),
                    limit_sell,
                    "down",
                    parse_limit_price(parts[1]),
                    float(parts[2]),
                    target,
                )
            elif cmd == "cu":
                with state_lock:
                    target = state.get("target_market", "current")
                enqueue_market_task(current_market_key(), cash_out, "up", target)
            elif cmd == "cd":
                with state_lock:
                    target = state.get("target_market", "current")
                enqueue_market_task(current_market_key(), cash_out, "down", target)
            elif cmd == "ca":
                enqueue_market_task(current_market_key(), cancel_all)
            elif cmd == "target" and len(parts) == 2 and parts[1] in ("current", "next"):
                with state_lock:
                    state["target_market"] = parts[1]
                log(f"Target market set to {parts[1].upper()}")
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
            with state_lock:
                slug = state.get("current_slug", "-")
                rem = max(0, int(state.get("interval_end", 0)) - int(time.time()))
                ws_ok = bool(state.get("ws_ok"))
                up_bid = to_cent_display(state.get("up_bid"))
                down_bid = to_cent_display(state.get("down_bid"))
                ptb = state.get("btc_price_to_beat")
                now_px = state.get("btc_price_now")
            ws_text = "LIVE" if ws_ok else "DOWN"
            if ptb is not None and now_px is not None:
                delta = float(now_px) - float(ptb)
                direction = "UP" if delta > 0 else ("DOWN" if delta < 0 else "FLAT")
                print(
                    f"[{time.strftime('%H:%M:%S')}] [STAT] {slug} rem={rem}s ws={ws_text} "
                    f"UP={up_bid} DOWN={down_bid} PTB={fmt_usd(ptb)} NOW={fmt_usd(now_px)} "
                    f"DELTA={direction} {fmt_usd(abs(delta))}",
                    flush=True,
                )
            else:
                print(
                    f"[{time.strftime('%H:%M:%S')}] [STAT] {slug} rem={rem}s ws={ws_text} "
                    f"UP={up_bid} DOWN={down_bid} PTB=- NOW=-",
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
  <title>POLYBOT // web-v3.0</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #080c0a;
      --surface: #0c110e;
      --surface2: #101812;
      --border: #1a2e20;
      --border2: #243d2b;
      --text: #c8ddd0;
      --muted: #5a7a62;
      --dim: #3a5442;
      --green: #39e87a;
      --green2: #27a855;
      --red: #e84040;
      --red2: #a82727;
      --amber: #e8b339;
      --cyan: #39d4e8;
      --up: #39e87a;
      --down: #e84040;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      background: var(--bg);
      color: var(--text);
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 13px;
      height: 100%;
      overflow-x: hidden;
    }

    /* scanline effect */
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0,0,0,0.03) 2px,
        rgba(0,0,0,0.03) 4px
      );
      pointer-events: none;
      z-index: 9999;
    }

    .terminal {
      max-width: 1280px;
      margin: 0 auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    /* ── TOPBAR ── */
    .topbar {
      display: flex;
      align-items: center;
      gap: 0;
      border: 1px solid var(--border);
      background: var(--surface);
      overflow: hidden;
    }
    .topbar-brand {
      padding: 6px 14px;
      font-weight: 700;
      font-size: 14px;
      color: var(--green);
      letter-spacing: 0.15em;
      border-right: 1px solid var(--border);
      white-space: nowrap;
    }
    .topbar-brand span { color: var(--muted); }
    .badges {
      display: flex;
      flex: 1;
      overflow: hidden;
    }
    .badge {
      padding: 6px 12px;
      border-right: 1px solid var(--border);
      font-size: 12px;
      white-space: nowrap;
      color: var(--muted);
    }
    .badge b { color: var(--text); }
    .badge.ws-live b { color: var(--green); }
    .badge.ws-down b { color: var(--red); }
    .badge.mode-real b { color: var(--amber); }
    .badge.mode-dry b { color: var(--cyan); }

    /* ── MAIN GRID ── */
    .grid {
      display: grid;
      grid-template-columns: 1fr 320px;
      gap: 6px;
    }
    .col-left { display: flex; flex-direction: column; gap: 6px; }
    .col-right { display: flex; flex-direction: column; gap: 6px; }

    /* ── PANEL ── */
    .panel {
      border: 1px solid var(--border);
      background: var(--surface);
      overflow: hidden;
    }
    .panel-header {
      padding: 4px 10px;
      background: var(--surface2);
      border-bottom: 1px solid var(--border);
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--dim);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .panel-header::before {
      content: '//';
      color: var(--border2);
    }

    /* ── ORDERBOOK TABLE ── */
    .ob-table {
      width: 100%;
      border-collapse: collapse;
    }
    .ob-table th {
      padding: 5px 10px;
      text-align: left;
      font-size: 10px;
      letter-spacing: 0.08em;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      background: var(--surface2);
      font-weight: 400;
    }
    .ob-table td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
      font-weight: 500;
    }
    .ob-table tr:last-child td { border-bottom: none; }
    .ob-table tr.row-up td { border-left: none; }
    .ob-table tr.row-down td { border-left: none; }

    .label-up { color: var(--up); font-weight: 700; letter-spacing: 0.05em; }
    .label-down { color: var(--down); font-weight: 700; letter-spacing: 0.05em; }
    .price { font-variant-numeric: tabular-nums; }
    .pos-val { color: var(--amber); }
    .pnl-pos { color: var(--green); }
    .pnl-neg { color: var(--red); }
    .pnl-zero { color: var(--muted); }

    /* ── ACTION BUTTONS ── */
    .btn-cashout {
      padding: 5px 12px;
      font-family: inherit;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      border: 1px solid var(--border2);
      background: var(--surface2);
      color: var(--muted);
      cursor: pointer;
      transition: all 0.1s;
      text-transform: uppercase;
    }
    .btn-cashout:not(:disabled):hover {
      border-color: var(--amber);
      color: var(--amber);
      background: rgba(232,179,57,0.08);
    }
    .btn-cashout:disabled { opacity: 0.3; cursor: not-allowed; }
    .btn-cashout.has-pos {
      border-color: var(--amber);
      color: var(--amber);
    }

    /* ── COMMAND PANEL ── */
    .cmd-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 4px;
      padding: 8px;
    }
    .cmd-section {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .cmd-section-label {
      font-size: 9px;
      letter-spacing: 0.1em;
      color: var(--dim);
      text-transform: uppercase;
      padding: 0 2px;
    }
    .cmd-row {
      display: flex;
      gap: 4px;
    }
    .cmd-btn {
      flex: 1;
      padding: 7px 6px;
      font-family: inherit;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      border: 1px solid var(--border2);
      background: var(--surface2);
      color: var(--text);
      cursor: pointer;
      transition: all 0.08s;
      text-transform: uppercase;
    }
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
    @keyframes flash {
      0% { filter: brightness(2); }
      100% { filter: brightness(1); }
    }

    /* ── INPUT ROW ── */
    .input-row {
      display: flex;
      gap: 4px;
      padding: 0 8px 8px;
    }
    .cmd-input {
      flex: 1;
      padding: 7px 10px;
      font-family: inherit;
      font-size: 12px;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border2);
      outline: none;
    }
    .cmd-input:focus { border-color: var(--green2); }
    .cmd-input::placeholder { color: var(--dim); }
    .send-btn {
      padding: 7px 16px;
      font-family: inherit;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      background: var(--green2);
      color: #000;
      border: none;
      cursor: pointer;
      transition: filter 0.1s;
      text-transform: uppercase;
    }
    .send-btn:hover { filter: brightness(1.2); }

    /* ── LOGS ── */
    .log-wrap {
      padding: 8px 10px;
      max-height: 200px;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border2) transparent;
    }
    .log-line {
      display: block;
      line-height: 1.6;
      font-size: 12px;
      color: var(--muted);
      white-space: pre-wrap;
      word-break: break-all;
    }
    .log-line.trade { color: var(--amber); }
    .log-line.err { color: var(--red); }
    .log-line.ok { color: var(--green); }

    /* ── OPEN ORDERS ── */
    .oo-wrap {
      padding: 8px 10px;
      max-height: 140px;
      overflow-y: auto;
      font-size: 11px;
      color: var(--muted);
      white-space: pre-wrap;
      scrollbar-width: thin;
    }

    /* ── HINT ── */
    .hint {
      padding: 4px 10px 6px;
      font-size: 10px;
      color: var(--dim);
      line-height: 1.5;
    }

    @media (max-width: 860px) {
      .grid { grid-template-columns: 1fr; }
      .cmd-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div class="terminal">

  <!-- TOPBAR -->
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

  <!-- MAIN GRID -->
  <div class="grid">
    <div class="col-left">

      <!-- ORDERBOOK -->
      <div class="panel">
        <div class="panel-header">orderbook</div>
        <table class="ob-table">
          <thead>
            <tr>
              <th>SIDE</th><th>BID</th><th>ASK</th>
              <th>POS</th><th>ENTRY</th><th>PNL</th><th>ACTION</th>
            </tr>
          </thead>
          <tbody id="ob">
            <tr class="row-up">
              <td class="label-up">UP</td>
              <td class="price" id="up-bid">-</td>
              <td class="price" id="up-ask">-</td>
              <td id="up-pos">-</td>
              <td id="up-entry">-</td>
              <td class="pnl-zero" id="up-pnl">-</td>
              <td><button class="btn-cashout" id="btn-cu" onclick="fire('cu')" disabled>SELL UP</button></td>
            </tr>
            <tr class="row-down">
              <td class="label-down">DOWN</td>
              <td class="price" id="down-bid">-</td>
              <td class="price" id="down-ask">-</td>
              <td id="down-pos">-</td>
              <td id="down-entry">-</td>
              <td class="pnl-zero" id="down-pnl">-</td>
              <td><button class="btn-cashout" id="btn-cd" onclick="fire('cd')" disabled>SELL DOWN</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- LOGS -->
      <div class="panel">
        <div class="panel-header">system log</div>
        <div class="log-wrap" id="log-wrap">
          <span class="log-line">waiting...</span>
        </div>
      </div>

      <!-- TRADE LOG -->
      <div class="panel">
        <div class="panel-header">trade log</div>
        <div class="log-wrap" id="tlog-wrap">
          <span class="log-line">no trades yet</span>
        </div>
      </div>

    </div>
    <div class="col-right">

      <!-- QUICK COMMANDS -->
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
            <div class="cmd-row">
              <button class="cmd-btn sell" onclick="fire('cu')">SELL UP</button>
            </div>
            <div class="cmd-row">
              <button class="cmd-btn sell" onclick="fire('cd')">SELL DOWN</button>
            </div>
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
        <div class="hint">
          bu/bd = buy up/down &nbsp;|&nbsp; cu/cd = cashout up/down<br>
          lu [price] [usd] = limit buy up &nbsp;|&nbsp; ca = cancel all
        </div>
        <div class="input-row">
          <input class="cmd-input" id="cmd" placeholder="> e.g. lu 74 1" autocomplete="off"/>
          <button class="send-btn" onclick="submitInput()">SEND</button>
        </div>
      </div>

      <!-- OPEN ORDERS -->
      <div class="panel">
        <div class="panel-header">open orders</div>
        <div class="oo-wrap" id="oo">[]</div>
      </div>

    </div>
  </div>
</div>

<script>
// UI refresh rate: 100ms for near-real-time feel
const TICK_MS = 100;
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
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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

    // Orderbook rows (static DOM, only update text/class for reliable click handling)
    const upPnl = pnlStr(s.up_pnl);
    const downPnl = pnlStr(s.down_pnl);
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
    upPnlEl.textContent = upPnl;
    downPnlEl.textContent = downPnl;
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

    document.getElementById('oo').textContent =
      JSON.stringify(s.open_orders, null, 2) || '[]';
  } catch(e) {}
  finally {
    tickInFlight = false;
  }
}

setInterval(tick, TICK_MS);
tick();
</script>
</body>
</html>"""


def make_snapshot():
    # Keep current/off-market visibility for compatibility.
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
        view_up_tok = nxt_up_tok if target == "next" else cur_up_tok
        view_down_tok = nxt_down_tok if target == "next" else cur_down_tok
        view_up_bid_raw = nxt["next_up_bid"] if target == "next" else up_bid
        view_up_ask_raw = nxt["next_up_ask"] if target == "next" else up_ask
        view_down_bid_raw = nxt["next_down_bid"] if target == "next" else down_bid
        view_down_ask_raw = nxt["next_down_ask"] if target == "next" else down_ask
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
        skip_tokens = {cur_up_tok, cur_down_tok, nxt_up_tok, nxt_down_tok}
        for tok, pos in state["positions"].items():
            if tok in skip_tokens:
                continue
            if is_dust_position(pos):
                continue
            side = str(pos.get("side", "")).upper() or "?"
            shares = float(pos.get("shares", 0.0))
            entry = float(pos.get("entry", 0.0))
            slug = str(pos.get("slug", "-"))
            residual_positions.append({
                "token": str(tok)[-8:],
                "side": side,
                "shares": round(shares, 4),
                "entry": to_cent_display(entry),
                "slug": slug,
            })
        residual_positions = residual_positions[-10:]

        return {
            "version": WEB_UI_VERSION,
            "snapshot_seq": snapshot_seq,
            "up_bid": to_cent_display(up_bid),
            "up_ask": to_cent_display(up_ask),
            "down_bid": to_cent_display(down_bid),
            "down_ask": to_cent_display(down_ask),
            "interval_end": state["interval_end"],
            "current_slug": state["current_slug"],
            "ws_ok": state["ws_ok"],
            "last_ws": state["last_ws"],
            "quote_age_ms": max(0, int((time.time() - float(state.get("last_quote_ts", 0))) * 1000)),
            "balance": state["balance"],
            "dry_run": state["dry_run"],
            "open_orders": open_orders[-8:],
            "log": state["log"][-50:],
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
            "target_market": nxt["target"],
            "next_slug": nxt["next_slug"],
            "next_ready": nxt["next_ready"],
            "next_up_bid": to_cent_display(nxt["next_up_bid"]),
            "next_up_ask": to_cent_display(nxt["next_up_ask"]),
            "next_down_bid": to_cent_display(nxt["next_down_bid"]),
            "next_down_ask": to_cent_display(nxt["next_down_ask"]),
            "next_interval_end": nxt["next_interval_end"],
            "ob_view": target,
            "ob_up_bid": to_cent_display(view_up_bid_raw),
            "ob_up_ask": to_cent_display(view_up_ask_raw),
            "ob_down_bid": to_cent_display(view_down_bid_raw),
            "ob_down_ask": to_cent_display(view_down_ask_raw),
            "ob_up_pos_usd": f"${view_up_pos['usd_in']:.2f} / {float(view_up_pos['shares']):.4f}sh" if view_up_pos else "-",
            "ob_down_pos_usd": f"${view_down_pos['usd_in']:.2f} / {float(view_down_pos['shares']):.4f}sh" if view_down_pos else "-",
            "ob_up_entry": to_cent_display(view_up_pos["entry"]) if view_up_pos else "-",
            "ob_down_entry": to_cent_display(view_down_pos["entry"]) if view_down_pos else "-",
            "ob_up_pnl": ob_up_pnl,
            "ob_down_pnl": ob_down_pnl,
            "ob_up_has_pos": bool(view_up_pos),
            "ob_down_has_pos": bool(view_down_pos),
            "residual_positions": residual_positions,
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
        if self.path == "/":
            self._html(load_frontend_html())
            return
        if self.path == "/api/state":
            self._json(make_snapshot())
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/cmd":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
                cmd = str(data.get("cmd", "")).strip()
                if cmd:
                    cmd_queue.put(cmd)
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
    if not smart_fetch_tokens():
        print("No active market. Exit.")
        return

    threading.Thread(target=start_ws, daemon=True).start()
    threading.Thread(target=start_rtds, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=market_watcher, daemon=True).start()
    threading.Thread(target=fetch_balance, daemon=True).start()
    threading.Thread(target=sync_open_orders, daemon=True).start()
    threading.Thread(target=reconcile_positions, daemon=True).start()
    threading.Thread(target=command_loop, daemon=True).start()
    threading.Thread(target=terminal_status_loop, daemon=True).start()

    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    server.timeout = 0.5  # unblock handle_request every 0.5s to check state["running"]
    print(f"Web UI {WEB_UI_VERSION} running at http://{WEB_HOST}:{WEB_PORT}")
    while state["running"]:
        server.handle_request()
    server.server_close()
    print("Bye.")


if __name__ == "__main__":
    main()
