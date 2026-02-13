import os
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.getenv("RIT_API_KEY", "YOUR_API_KEY")
POLL_SECS = 0.5
EDGE_BPS = 6
MIN_EDGE = 0.03
LIMIT_BUFFER = 0.8  # do not exceed 80% of gross/net


def best_bid_ask(client, ticker):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def infer_ticker(caption, tickers):
    if not caption:
        return None
    for t in tickers:
        if t in caption:
            return t
    return None


def within_limits(client, action, qty):
    try:
        limits = client.get_limits()
    except Exception:
        return True
    # Use aggregate limits if provided; otherwise allow
    for lim in limits:
        gross = lim.get("gross")
        net = lim.get("net")
        gross_limit = lim.get("gross_limit")
        net_limit = lim.get("net_limit")
        if gross is None or gross_limit is None:
            continue
        # approximate impact
        new_gross = gross + abs(qty)
        if new_gross > LIMIT_BUFFER * gross_limit:
            return False
        if net is not None and net_limit is not None:
            new_net = abs(net + (qty if action == "BUY" else -qty))
            if new_net > LIMIT_BUFFER * net_limit:
                return False
    return True


def main():
    client = RITClient(API_KEY)
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
            caption = t.get("caption") or ""
            ticker = infer_ticker(caption, tickers)

            if not ticker or qty <= 0:
                continue

            if not within_limits(client, action, qty):
                try:
                    client.decline_tender(tid)
                    seen.add(tid)
                except Exception:
                    pass
                continue

            bid, ask = best_bid_ask(client, ticker)
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            edge = max(MIN_EDGE, mid * EDGE_BPS / 10000.0)

            accept = False
            submit_price = None

            if is_fixed:
                if action == "BUY" and price <= mid - edge:
                    accept = True
                elif action == "SELL" and price >= mid + edge:
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
