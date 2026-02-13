"""News-driven volatility arbitrage for the Volatility Trading Case.

Idea
- Parse volatility forecast and delta limits from news.
- Price options with Black-Scholes using forecast volatility.
- Trade mispriced options and hedge delta with RTM.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python vol_news_arb_bot.py
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

POLL_SECS = 0.5
ORDER_COOLDOWN_SECS = 1.0
PRICE_THRESHOLD = 0.05  # absolute price edge
ORDER_QTY = 10  # option contracts per trade
RTM_HEDGE_QTY = 1000  # shares per delta hedge chunk
ASSUMED_T_YEARS = 1.0 / 12.0
RISK_FREE = 0.0

MAX_POS_PER_OPTION = 200
HEDGE_TRIGGER = 0.8  # hedge when |delta| exceeds 80% of limit

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


def best_bid_ask(client: RITClient, ticker: str):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def parse_vol_from_news(news_items):
    vol = None
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"between\s+(\d+(?:\.\d+)?)%\s+and\s+(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            vol = (a + b) / 2.0 / 100.0
            continue
        m = re.search(r"volatility[^\d]*(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
        if m:
            vol = float(m.group(1)) / 100.0
    return vol


def parse_delta_limit(news_items, current_limit: int) -> int:
    limit = current_limit
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"delta limit[^\d]*(\d+)", text, re.IGNORECASE)
        if m:
            limit = int(m.group(1))
    return limit


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


def main():
    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    last_news_id = 0
    current_vol = None
    delta_limit = 10000
    last_order_ts = {}

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        news = client.get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            current_vol = parse_vol_from_news(news) or current_vol
            delta_limit = parse_delta_limit(news, delta_limit)

        if current_vol is None:
            time.sleep(POLL_SECS)
            continue

        rtm_bid, rtm_ask = best_bid_ask(client, "RTM")
        if rtm_bid is None or rtm_ask is None:
            time.sleep(POLL_SECS)
            continue
        S = (rtm_bid + rtm_ask) / 2.0

        positions = {s["ticker"]: s.get("position", 0) for s in client.get_securities()}
        delta = compute_portfolio_delta(S, positions, current_vol)

        now = time.time()
        for t in OPTION_TICKERS:
            bid, ask = best_bid_ask(client, t)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            call, K = option_specs(t)
            if call is None:
                continue
            theo = bs_price(S, K, ASSUMED_T_YEARS, RISK_FREE, current_vol, call=call)
            edge = theo - mid

            pos = positions.get(t, 0)
            if pos >= MAX_POS_PER_OPTION and edge > 0:
                continue
            if pos <= -MAX_POS_PER_OPTION and edge < 0:
                continue

            if now - last_order_ts.get(t, 0) < ORDER_COOLDOWN_SECS:
                continue

            if edge > PRICE_THRESHOLD:
                client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=bid)
                last_order_ts[t] = now
            elif edge < -PRICE_THRESHOLD:
                client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=ask)
                last_order_ts[t] = now

        if abs(delta) > HEDGE_TRIGGER * delta_limit:
            hedge_action = "SELL" if delta > 0 else "BUY"
            client.place_order("RTM", "MARKET", RTM_HEDGE_QTY, hedge_action)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
