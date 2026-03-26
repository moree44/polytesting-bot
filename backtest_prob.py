#!/usr/bin/env python3
"""Backtest PTB-based probability using Binance 1m data.

Pre-open: use price just before interval start, PTB = open of interval.
Live-mid: use price at t0+120s (3rd 1m candle), rem=180s.

Outputs win-rate and Brier score.
"""
from __future__ import annotations

import argparse
import math
import os
import time
from collections import defaultdict

import requests

BINANCE_API_BASES = [
    base.strip().rstrip("/")
    for base in os.getenv(
        "BINANCE_API_BASES",
        "https://api.binance.com,https://api1.binance.com,https://api2.binance.com,https://api3.binance.com,https://data-api.binance.vision",
    ).split(",")
    if base.strip()
]
BINANCE_HTTP_TIMEOUT = float(os.getenv("BINANCE_HTTP_TIMEOUT", "10"))
BINANCE_HTTP_PROXY = os.getenv("BINANCE_HTTP_PROXY", "").strip()
BINANCE_HTTPS_PROXY = os.getenv("BINANCE_HTTPS_PROXY", "").strip()


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
        raise RuntimeError(" | ".join(errors))
    raise last_error if last_error is not None else RuntimeError("No Binance API base configured")


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cur,
            "endTime": end_ms,
            "limit": 1000,
        }
        r = binance_http_get("/api/v3/klines", params)
        rows = r.json()
        if not rows:
            break
        for c in rows:
            ts = int(c[0]) // 1000
            out.append({
                "ts": ts,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
            })
        last_ts = int(rows[-1][0]) // 1000
        cur = (last_ts + 60) * 1000
        if len(rows) < 1000:
            break
        time.sleep(0.05)
    return out


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def estimate_annual_vol(closes: list[float], dts: list[int]) -> float:
    if len(closes) < 8:
        return 0.75
    rets = []
    for i in range(1, len(closes)):
        px0, px1 = closes[i - 1], closes[i]
        if px0 <= 0 or px1 <= 0:
            continue
        rets.append(math.log(px1 / px0))
    if len(rets) < 6:
        return 0.75
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / max(1, (len(rets) - 1))
    std_step = math.sqrt(max(var, 0.0))
    avg_dt = sum(dts) / max(1, len(dts))
    if avg_dt <= 0:
        return 0.75
    sec_year = 365.0 * 24.0 * 3600.0
    ann = std_step * math.sqrt(sec_year / avg_dt)
    return clamp(ann, 0.01, 5.0)


def prob_ptb(s: float, k: float, rem_sec: int, sigma: float) -> float:
    tau = rem_sec / (365.0 * 24.0 * 3600.0)
    den = sigma * math.sqrt(max(tau, 1e-12))
    if den < 1e-9:
        p = 1.0 if s > k else (0.0 if s < k else 0.5)
    else:
        z = (math.log(k / s) + 0.5 * (sigma ** 2) * tau) / den
        p = 1.0 - norm_cdf(z)
    return clamp(p, 0.02, 0.98)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--vol-window-sec", type=int, default=240)
    args = ap.parse_args()

    end_ms = int(time.time()) * 1000
    start_ms = end_ms - args.days * 24 * 3600 * 1000

    print(f"Fetching Binance 1m data: {args.symbol} ({args.days}d)")
    rows = fetch_klines(args.symbol, start_ms, end_ms)
    if not rows:
        print("No data returned.")
        return

    rows.sort(key=lambda x: x["ts"])
    by_ts = {r["ts"]: r for r in rows}
    ts_list = [r["ts"] for r in rows]

    # build list of interval starts
    first = (ts_list[0] // 300) * 300
    last = (ts_list[-1] // 300) * 300

    stats = {
        "pre": {"n": 0, "win": 0, "brier": 0.0},
        "live": {"n": 0, "win": 0, "brier": 0.0},
    }
    calib = {"pre": defaultdict(list), "live": defaultdict(list)}

    for t0 in range(first, last + 1, 300):
        # need 5 candles
        candles = [by_ts.get(t0 + i * 60) for i in range(5)]
        if any(c is None for c in candles):
            continue
        ptb = candles[0]["open"]
        close_end = candles[-1]["close"]
        y = 1 if close_end > ptb else 0

        # vol window
        vol_start = t0 - args.vol_window_sec
        hist = [by_ts.get(t) for t in range(vol_start, t0, 60)]
        hist = [h for h in hist if h]
        closes = [h["close"] for h in hist]
        dts = [60 for _ in range(len(closes))]
        sigma = estimate_annual_vol(closes, dts)

        # pre-open: use last close before t0
        prev = by_ts.get(t0 - 60)
        if prev:
            s_pre = prev["close"]
            p_pre = prob_ptb(s_pre, ptb, 300, sigma)
            stats["pre"]["n"] += 1
            stats["pre"]["win"] += 1 if (p_pre >= 0.5) == (y == 1) else 0
            stats["pre"]["brier"] += (p_pre - y) ** 2
            b = int(p_pre * 10)
            calib["pre"][b].append(y)

        # live-mid: use close at t0+120, rem=180
        mid = by_ts.get(t0 + 120)
        if mid:
            s_live = mid["close"]
            p_live = prob_ptb(s_live, ptb, 180, sigma)
            stats["live"]["n"] += 1
            stats["live"]["win"] += 1 if (p_live >= 0.5) == (y == 1) else 0
            stats["live"]["brier"] += (p_live - y) ** 2
            b = int(p_live * 10)
            calib["live"][b].append(y)

    for k in ("pre", "live"):
        n = stats[k]["n"]
        if n == 0:
            print(f"{k}: no samples")
            continue
        win = stats[k]["win"] / n
        brier = stats[k]["brier"] / n
        print(f"{k.upper()}  n={n}  win={win*100:.1f}%  brier={brier:.4f}")
        print("  calib bins (p->emp):")
        for b in sorted(calib[k].keys()):
            ys = calib[k][b]
            if not ys:
                continue
            p = (b + 0.5) / 10.0
            emp = sum(ys) / len(ys)
            print(f"    {p:.1f} -> {emp:.2f} (n={len(ys)})")


if __name__ == "__main__":
    main()
