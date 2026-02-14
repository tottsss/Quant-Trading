"""Algorithmic Market Making standalone bot."""

import os
import time

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.3
ORDER_QTY = 500
MAX_SKEW = 0.05
SKEW_PER_SHARE = 1e-5


class RITClient:
    def __init__(self, api_key: str, base_url: str = "http://localhost:9999/v1", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-key": api_key})

    def _get(self, path: str, params: dict | None = None):
        return self.session.get(self.base_url + path, params=params, timeout=self.timeout)

    def _post(self, path: str, params: dict | None = None):
        return self.session.post(self.base_url + path, params=params, timeout=self.timeout)

    def get_case(self):
        r = self._get("/case")
        r.raise_for_status()
        return r.json()

    def get_limits(self):
        r = self._get("/limits")
        r.raise_for_status()
        return r.json()

    def get_securities(self):
        r = self._get("/securities")
        r.raise_for_status()
        return r.json()

    def get_book(self, ticker: str):
        r = self._get("/securities/book", {"ticker": ticker})
        r.raise_for_status()
        return r.json()

    def place_order(self, ticker: str, order_type: str, quantity: float, action: str, price: float | None = None):
        params = {"ticker": ticker, "type": order_type, "quantity": quantity, "action": action}
        if price is not None:
            params["price"] = price
        r = self._post("/orders", params=params)
        r.raise_for_status()
        return r.json()

    def cancel_all(self, ticker: str | None = None):
        params = {"ticker": ticker} if ticker else None
        r = self._post("/commands/cancel", params=params)
        r.raise_for_status()
        return r.json()


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return
        time.sleep(poll_s)


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
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    tickers = [s["ticker"] for s in client.get_securities()]
    agg_limit = get_aggregate_limit(client)

    print(f"Connected to {BASE_URL}. Running algo MM bot...")

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            print("Case no longer ACTIVE. Exiting.")
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
