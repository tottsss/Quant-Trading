import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[1] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = "YOUR_API_KEY"
POLL_SECS = 0.3
ORDER_QTY = 500
MAX_SKEW = 0.05
SKEW_PER_SHARE = 1e-5


def best_bid_ask(client, ticker):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def get_aggregate_limit(client):
    try:
        limits = client.get_limits()
    except Exception:
        return None
    for l in limits:
        name = (l.get("name") or "").lower()
        if "aggregate" in name:
            return l.get("gross_limit") or l.get("net_limit")
    gross_limits = [l.get("gross_limit") for l in limits if l.get("gross_limit") is not None]
    return min(gross_limits) if gross_limits else None


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    tickers = [s["ticker"] for s in client.get_securities()]
    agg_limit = get_aggregate_limit(client)

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        sec = client.get_securities()
        pos = {s["ticker"]: s.get("position", 0) for s in sec}
        agg_pos = sum(abs(p) for p in pos.values())

        for t in tickers:
            bid, ask = best_bid_ask(client, t)
            if bid is None or ask is None:
                continue

            mid = (bid + ask) / 2.0
            p = pos.get(t, 0)
            skew = max(-MAX_SKEW, min(MAX_SKEW, p * SKEW_PER_SHARE))
            quote_bid = round(mid - (ask - bid) / 2.0 - skew, 2)
            quote_ask = round(mid + (ask - bid) / 2.0 - skew, 2)

            if agg_limit is not None and agg_pos > 0.9 * agg_limit:
                if p > 0:
                    client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=quote_ask)
                elif p < 0:
                    client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=quote_bid)
            else:
                client.place_order(t, "LIMIT", ORDER_QTY, "BUY", price=quote_bid)
                client.place_order(t, "LIMIT", ORDER_QTY, "SELL", price=quote_ask)

            time.sleep(0.05)
            client.cancel_all(ticker=t)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
