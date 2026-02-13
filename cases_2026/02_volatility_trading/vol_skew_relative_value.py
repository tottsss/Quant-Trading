"""Skew/relative-value volatility strategy for the Volatility Trading Case.

Idea
- Compute implied vols for each strike.
- Fit a simple line (iv = a + b*K) per call/put wing.
- Trade outliers vs the fitted skew; hedge delta with RTM.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python vol_skew_relative_value.py
"""

import math
import os
import re
import time
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.75
ASSUMED_T_YEARS = 1.0 / 12.0
RISK_FREE = 0.0

Z_THRESHOLD = 1.2
ORDER_QTY = 10
MAX_POS_PER_OPTION = 150
RTM_HEDGE_QTY = 1000
HEDGE_TRIGGER = 0.75

OPTION_TICKERS = [f"RTM1C{K}" for K in range(45, 55)] + [f"RTM1P{K}" for K in range(45, 55)]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        if call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    if call:
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def implied_vol(S: float, K: float, T: float, r: float, price: float, call: bool) -> float | None:
    intrinsic = max(0.0, S - K) if call else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return 0.0

    low, high = 1e-4, 5.0
    for _ in range(60):
        mid = (low + high) / 2.0
        val = bs_price(S, K, T, r, mid, call=call)
        if abs(val - price) < 1e-4:
            return mid
        if val > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def best_bid_ask(client: RITClient, ticker: str):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def option_specs(ticker: str):
    m = re.match(r"RTM1([CP])(\d+)$", ticker)
    if not m:
        return None, None
    call = m.group(1) == "C"
    K = float(m.group(2))
    return call, K


def linear_fit(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1e-9
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    return intercept, slope


def compute_portfolio_delta(S: float, positions: dict, iv_map: dict) -> float:
    delta = positions.get("RTM", 0)
    for t, iv in iv_map.items():
        pos = positions.get(t, 0)
        if pos == 0:
            continue
        call, K = option_specs(t)
        if call is None:
            continue
        d = bs_delta(S, K, ASSUMED_T_YEARS, RISK_FREE, iv, call=call)
        delta += d * pos * 100.0
    return delta


def main():
    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    last_news_id = 0
    delta_limit = 10000
    last_order_ts = {}

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        news = client.get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            for n in news:
                text = (n.get("headline") or "") + " " + (n.get("body") or "")
                m = re.search(r"delta limit[^\d]*(\d+)", text, re.IGNORECASE)
                if m:
                    delta_limit = int(m.group(1))

        rtm_bid, rtm_ask = best_bid_ask(client, "RTM")
        if rtm_bid is None or rtm_ask is None:
            time.sleep(POLL_SECS)
            continue
        S = (rtm_bid + rtm_ask) / 2.0

        # Build IV surface
        calls = []  # (K, iv, ticker, bid, ask)
        puts = []
        iv_map = {}

        for t in OPTION_TICKERS:
            bid, ask = best_bid_ask(client, t)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            call, K = option_specs(t)
            if call is None:
                continue
            iv = implied_vol(S, K, ASSUMED_T_YEARS, RISK_FREE, mid, call=call)
            if iv is None:
                continue
            iv_map[t] = iv
            row = (K, iv, t, bid, ask)
            if call:
                calls.append(row)
            else:
                puts.append(row)

        if len(calls) < 3 or len(puts) < 3:
            time.sleep(POLL_SECS)
            continue

        call_intercept, call_slope = linear_fit([c[0] for c in calls], [c[1] for c in calls])
        put_intercept, put_slope = linear_fit([p[0] for p in puts], [p[1] for p in puts])

        # Residuals and z-scores
        call_residuals = [c[1] - (call_intercept + call_slope * c[0]) for c in calls]
        put_residuals = [p[1] - (put_intercept + put_slope * p[0]) for p in puts]
        call_std = (sum(r * r for r in call_residuals) / max(1, len(call_residuals) - 1)) ** 0.5 or 1e-6
        put_std = (sum(r * r for r in put_residuals) / max(1, len(put_residuals) - 1)) ** 0.5 or 1e-6

        positions = {s["ticker"]: s.get("position", 0) for s in client.get_securities()}
        now = time.time()

        for (K, iv, t, bid, ask), resid in zip(calls, call_residuals):
            z = resid / call_std
            pos = positions.get(t, 0)
            if now - last_order_ts.get(t, 0) < 1.0:
                continue
            if z > Z_THRESHOLD and pos > -MAX_POS_PER_OPTION:
                client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=ask)
                last_order_ts[t] = now
            elif z < -Z_THRESHOLD and pos < MAX_POS_PER_OPTION:
                client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=bid)
                last_order_ts[t] = now

        for (K, iv, t, bid, ask), resid in zip(puts, put_residuals):
            z = resid / put_std
            pos = positions.get(t, 0)
            if now - last_order_ts.get(t, 0) < 1.0:
                continue
            if z > Z_THRESHOLD and pos > -MAX_POS_PER_OPTION:
                client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=ask)
                last_order_ts[t] = now
            elif z < -Z_THRESHOLD and pos < MAX_POS_PER_OPTION:
                client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=bid)
                last_order_ts[t] = now

        delta = compute_portfolio_delta(S, positions, iv_map)
        if abs(delta) > HEDGE_TRIGGER * delta_limit:
            hedge_action = "SELL" if delta > 0 else "BUY"
            client.place_order("RTM", "MARKET", RTM_HEDGE_QTY, hedge_action)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
