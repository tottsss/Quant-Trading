"""Liquidity Risk: size-tiered edge.

Idea
- Demand a bigger edge for larger tenders because they are harder to unwind.
- Accept fixed tenders only if price beats mid by the size-adjusted edge.
- For auctions, submit a price using the same size-adjusted edge.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python size_tiered_edge.py
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

# Size tiers (shares/contracts). Adjust to your case.
TIER_1 = 2000
TIER_2 = 5000
MULT_1 = 1.0
MULT_2 = 1.5
MULT_3 = 2.0

HEDGE_WITH_MARKET = True


def best_bid_ask(client: RITClient, ticker: str):
    book = client.get_book(ticker)
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


def size_multiplier(qty: float) -> float:
    if qty <= TIER_1:
        return MULT_1
    if qty <= TIER_2:
        return MULT_2
    return MULT_3


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

            bid, ask = best_bid_ask(client, ticker)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            edge = max(MIN_EDGE, mid * EDGE_BPS / 10000.0) * size_multiplier(qty)

            accept = False
            submit_price = None

            if is_fixed:
                if action == "BUY" and price is not None and price <= mid - edge:
                    accept = True
                elif action == "SELL" and price is not None and price >= mid + edge:
                    accept = True
            else:
                if action == "BUY":
                    submit_price = round(mid - edge, 2)
                elif action == "SELL":
                    submit_price = round(mid + edge, 2)
                if submit_price is not None:
                    accept = True

            if accept:
                try:
                    client.accept_tender(tid, price=submit_price)
                    seen.add(tid)
                    if HEDGE_WITH_MARKET:
                        hedge_action = "SELL" if action == "BUY" else "BUY"
                        client.place_order(ticker, "MARKET", qty, hedge_action)
                except Exception:
                    pass
            else:
                try:
                    client.decline_tender(tid)
                    seen.add(tid)
                except Exception:
                    pass

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
