"""Liquidity Risk: accept with edge and hedge using a simple TWAP schedule.

Idea
- Accept only tenders with a clean edge vs the mid price.
- Hedge the accepted quantity in multiple smaller market slices to reduce impact.

How to run
1) Set API_KEY below (or export RIT_API_KEY).
2) Start the RIT Client (local REST API).
3) python twap_hedge.py
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

TWAP_SLICES = 5
TWAP_INTERVAL_SECS = 1.0


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


def schedule_twap(hedges: list[dict], ticker: str, action: str, qty: float):
    slices = max(1, TWAP_SLICES)
    slice_qty = max(1.0, qty / slices)
    hedges.append({
        "ticker": ticker,
        "action": action,
        "remaining": qty,
        "slice_qty": slice_qty,
        "next_time": time.time(),
        "interval": TWAP_INTERVAL_SECS,
    })


def process_hedges(client: RITClient, hedges: list[dict]):
    now = time.time()
    still = []
    for h in hedges:
        if h["remaining"] <= 0:
            continue
        if now < h["next_time"]:
            still.append(h)
            continue
        qty = min(h["slice_qty"], h["remaining"])
        try:
            client.place_order(h["ticker"], "MARKET", qty, h["action"])
            h["remaining"] -= qty
            h["next_time"] = now + h["interval"]
            if h["remaining"] > 0:
                still.append(h)
        except Exception:
            h["next_time"] = now + h["interval"]
            still.append(h)
    return still


def main():
    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)
    tickers = [s["ticker"] for s in client.get_securities()]
    seen = set()
    hedges = []

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        hedges = process_hedges(client, hedges)

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
            edge = max(MIN_EDGE, mid * EDGE_BPS / 10000.0)

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
                    hedge_action = "SELL" if action == "BUY" else "BUY"
                    schedule_twap(hedges, ticker, hedge_action, qty)
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
