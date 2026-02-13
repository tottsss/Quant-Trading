"""Realized-vs-implied volatility strategy for the Volatility Trading Case.

Idea
- Estimate realized volatility from recent RTM returns.
- Compute implied volatility for the near-ATM call/put.
- Trade a small straddle when implied deviates from realized.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python vol_realized_vs_implied.py
"""

import math
import os
import re
import time
from collections import deque
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.5
ASSUMED_T_YEARS = 1.0 / 12.0
RISK_FREE = 0.0

WINDOW = 120  # number of RTM mid returns for realized vol estimate
TICKS_PER_YEAR = 252 * 60  # adjust if you want a different annualization

EDGE_THRESHOLD = 0.03  # vol points (e.g., 0.03 = 3 vol)
STRADDLE_QTY = 5
RTM_HEDGE_QTY = 1000
HEDGE_TRIGGER = 0.7
MAX_POS_PER_OPTION = 100

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


def compute_portfolio_delta(S: float, positions: dict, sigma: float) -> float:
    delta = positions.get("RTM", 0)
    for t in OPTION_TICKERS:
        pos = positions.get(t, 0)
        if pos == 0:
            continue
        call, K = option_specs(t)
        if call is None:
            continue
        d = bs_delta(S, K, ASSUMED_T_YEARS, RISK_FREE, sigma, call=call)
        delta += d * pos * 100.0
    return delta


def choose_atm_strike(S: float) -> float:
    return min(range(45, 55), key=lambda k: abs(S - k))


def main():
    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    returns = deque(maxlen=WINDOW)
    last_mid = None
    last_trade_ts = 0.0
    delta_limit = 10000
    last_news_id = 0

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
        mid = (rtm_bid + rtm_ask) / 2.0

        if last_mid is not None and mid > 0 and last_mid > 0:
            returns.append(math.log(mid / last_mid))
        last_mid = mid

        if len(returns) < max(10, WINDOW // 3):
            time.sleep(POLL_SECS)
            continue

        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
        realized = math.sqrt(var_r) * math.sqrt(TICKS_PER_YEAR)

        K = choose_atm_strike(mid)
        call_ticker = f"RTM1C{K}"
        put_ticker = f"RTM1P{K}"

        call_bid, call_ask = best_bid_ask(client, call_ticker)
        put_bid, put_ask = best_bid_ask(client, put_ticker)
        if call_bid is None or call_ask is None or put_bid is None or put_ask is None:
            time.sleep(POLL_SECS)
            continue

        call_mid = (call_bid + call_ask) / 2.0
        put_mid = (put_bid + put_ask) / 2.0

        iv_call = implied_vol(mid, float(K), ASSUMED_T_YEARS, RISK_FREE, call_mid, call=True)
        iv_put = implied_vol(mid, float(K), ASSUMED_T_YEARS, RISK_FREE, put_mid, call=False)
        if iv_call is None or iv_put is None:
            time.sleep(POLL_SECS)
            continue

        iv_avg = (iv_call + iv_put) / 2.0
        vol_edge = iv_avg - realized

        positions = {s["ticker"]: s.get("position", 0) for s in client.get_securities()}
        if time.time() - last_trade_ts > 2.0:
            if vol_edge > EDGE_THRESHOLD:
                if positions.get(call_ticker, 0) > -MAX_POS_PER_OPTION:
                    client.place_order(call_ticker, "LIMIT", STRADDLE_QTY, "SELL", price=call_ask)
                if positions.get(put_ticker, 0) > -MAX_POS_PER_OPTION:
                    client.place_order(put_ticker, "LIMIT", STRADDLE_QTY, "SELL", price=put_ask)
                last_trade_ts = time.time()
            elif vol_edge < -EDGE_THRESHOLD:
                if positions.get(call_ticker, 0) < MAX_POS_PER_OPTION:
                    client.place_order(call_ticker, "LIMIT", STRADDLE_QTY, "BUY", price=call_bid)
                if positions.get(put_ticker, 0) < MAX_POS_PER_OPTION:
                    client.place_order(put_ticker, "LIMIT", STRADDLE_QTY, "BUY", price=put_bid)
                last_trade_ts = time.time()

        # Hedge delta using the implied vol average as a proxy for option deltas
        delta = compute_portfolio_delta(mid, positions, iv_avg)
        if abs(delta) > HEDGE_TRIGGER * delta_limit:
            hedge_action = "SELL" if delta > 0 else "BUY"
            client.place_order("RTM", "MARKET", RTM_HEDGE_QTY, hedge_action)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
