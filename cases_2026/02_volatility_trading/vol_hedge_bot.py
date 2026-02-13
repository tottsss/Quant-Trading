import math
import re
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[1] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = "YOUR_API_KEY"
POLL_SECS = 0.5
PRICE_THRESHOLD = 0.05   # absolute price edge
ORDER_QTY = 10           # option contracts per trade
RTM_QTY = 1000           # shares per delta hedge chunk
ASSUMED_T_YEARS = 1.0 / 12.0
RISK_FREE = 0.0

OPTION_TICKERS = [f"RTM1C{K}" for K in range(45, 55)] + [f"RTM1P{K}" for K in range(45, 55)]


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S, K, T, r, sigma, call=True):
    if T <= 0 or sigma <= 0:
        if call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    if call:
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def best_bid_ask(client, ticker):
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


def parse_delta_limit(news_items, current_limit):
    limit = current_limit
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"delta limit[^\d]*(\d+)", text, re.IGNORECASE)
        if m:
            limit = int(m.group(1))
    return limit


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    last_news_id = 0
    current_vol = None
    delta_limit = 10000

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

        bid, ask = best_bid_ask(client, "RTM")
        if bid is None or ask is None:
            time.sleep(POLL_SECS)
            continue
        S = (bid + ask) / 2.0

        positions = {s["ticker"]: s.get("position", 0) for s in client.get_securities()}

        delta = positions.get("RTM", 0)
        for t in OPTION_TICKERS:
            pos = positions.get(t, 0)
            if pos == 0:
                continue
            m = re.match(r"RTM1([CP])(\d+)$", t)
            if not m:
                continue
            call = m.group(1) == "C"
            K = float(m.group(2))
            d = bs_delta(S, K, ASSUMED_T_YEARS, RISK_FREE, current_vol, call=call)
            delta += d * pos * 100.0

        for t in OPTION_TICKERS:
            bid, ask = best_bid_ask(client, t)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            m = re.match(r"RTM1([CP])(\d+)$", t)
            if not m:
                continue
            call = m.group(1) == "C"
            K = float(m.group(2))
            theo = bs_price(S, K, ASSUMED_T_YEARS, RISK_FREE, current_vol, call=call)

            if mid < theo - PRICE_THRESHOLD:
                client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=bid)
            elif mid > theo + PRICE_THRESHOLD:
                client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=ask)

        if abs(delta) > 0.8 * delta_limit:
            hedge_action = "SELL" if delta > 0 else "BUY"
            client.place_order("RTM", "MARKET", RTM_QTY, hedge_action)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
