"""Liquidity Risk: depth-adjusted edge.

Idea
- Estimate the price you would actually get if you hedge the tender using order book depth.
- Only accept if the tender price is better than that hedge price by a safety edge.
- For auctions, submit a price that bakes in the estimated hedge cost.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python depth_adjusted_edge.py
"""

import os
import time
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.5
EDGE_BPS = 5
MIN_EDGE = 0.03
MAX_LEVELS = 5
HEDGE_WITH_MARKET = True


def best_bid_ask(book: dict):
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def infer_ticker(tender: dict, tickers: list[str]):
    ticker = tender.get("ticker")
    if ticker:
        return ticker
    caption = tender.get("caption") or ""
    for t in tickers:
        if t in caption:
            return t
    return None


def estimate_fill_price(levels: list[dict], qty: float, max_levels: int) -> float | None:
    remaining = qty
    notional = 0.0
    used = 0
    for level in levels[:max_levels]:
        if remaining <= 0:
            break
        price = level.get("price")
        size = level.get("quantity") or level.get("qty") or 0
        if price is None or size <= 0:
            continue
        take = min(remaining, float(size))
        notional += take * float(price)
        remaining -= take
        used += take
    if used <= 0:
        return None
    if remaining > 0:
        return None
    return notional / used


def main():
    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)
    tickers = [s["ticker"] for s in client.get_securities()]
    seen = set()

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        try:
            tenders = client.get_tenders()
        except Exception:
            time.sleep(POLL_SECS)
            continue

        for t in tenders:
            tid = t.get("tender_id")
            if tid in seen:
                continue

            action = (t.get("action") or "").upper()
            qty = float(t.get("quantity") or 0)
            is_fixed = bool(t.get("is_fixed_bid"))
            price = t.get("price")
            ticker = infer_ticker(t, tickers)

            if not ticker or qty <= 0:
                continue

            book = client.get_book(ticker)
            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            edge = max(MIN_EDGE, mid * EDGE_BPS / 10000.0)

            if action == "BUY":
                hedge_side = "SELL"
                hedge_price = estimate_fill_price(book.get("bids", []), qty, MAX_LEVELS)
                if hedge_price is None:
                    continue
                accept_price = hedge_price - edge
                accept = is_fixed and price is not None and price <= accept_price
                submit_price = round(accept_price, 2)
            elif action == "SELL":
                hedge_side = "BUY"
                hedge_price = estimate_fill_price(book.get("asks", []), qty, MAX_LEVELS)
                if hedge_price is None:
                    continue
                accept_price = hedge_price + edge
                accept = is_fixed and price is not None and price >= accept_price
                submit_price = round(accept_price, 2)
            else:
                continue

            if is_fixed:
                if accept:
                    try:
                        client.accept_tender(tid)
                        seen.add(tid)
                        if HEDGE_WITH_MARKET:
                            client.place_order(ticker, "MARKET", qty, hedge_side)
                    except Exception:
                        pass
                else:
                    try:
                        client.decline_tender(tid)
                        seen.add(tid)
                    except Exception:
                        pass
            else:
                try:
                    client.accept_tender(tid, price=submit_price)
                    seen.add(tid)
                    if HEDGE_WITH_MARKET:
                        client.place_order(ticker, "MARKET", qty, hedge_side)
                except Exception:
                    pass

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
