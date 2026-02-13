"""Liquidity Risk: depth-adjusted acceptance with TWAP hedge execution (standalone).

Standalone version:
- No local project imports.
- Requires only: requests

Run:
1) pip install requests
2) set RIT_API_KEY and (optionally) RIT_BASE_URL
3) python depth_twap_hybrid_standalone.py
"""

import os
import time
import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.5
EDGE_BPS = 5
MIN_EDGE = 0.03
MAX_LEVELS = 5

# TWAP hedge controls
TWAP_SLICES = 5
TWAP_INTERVAL_SECS = 1.0
MAX_ACTIVE_HEDGES = 20


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

    def _delete(self, path: str):
        return self.session.delete(self.base_url + path, timeout=self.timeout)

    def get_case(self):
        r = self._get("/case")
        r.raise_for_status()
        return r.json()

    def get_securities(self):
        r = self._get("/securities")
        r.raise_for_status()
        return r.json()

    def get_tenders(self):
        r = self._get("/tenders")
        r.raise_for_status()
        return r.json()

    def get_book(self, ticker: str):
        r = self._get("/securities/book", {"ticker": ticker})
        r.raise_for_status()
        return r.json()

    def accept_tender(self, tender_id: int, price: float | None = None):
        params = {} if price is None else {"price": price}
        r = self._post(f"/tenders/{tender_id}", params=params)
        r.raise_for_status()
        return r.json()

    def decline_tender(self, tender_id: int):
        r = self._delete(f"/tenders/{tender_id}")
        r.raise_for_status()
        return r.json()

    def place_order(self, ticker: str, order_type: str, quantity: float, action: str, price: float | None = None):
        params = {"ticker": ticker, "type": order_type, "quantity": quantity, "action": action}
        if price is not None:
            params["price"] = price
        r = self._post("/orders", params=params)
        r.raise_for_status()
        return r.json()


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return case
        time.sleep(poll_s)


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
    used = 0.0
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
    if used <= 0 or remaining > 0:
        return None
    return notional / used


def schedule_twap(hedges: list[dict], ticker: str, action: str, qty: float):
    slices = max(1, TWAP_SLICES)
    slice_qty = max(1.0, qty / slices)
    hedges.append(
        {
            "ticker": ticker,
            "action": action,
            "remaining": qty,
            "slice_qty": slice_qty,
            "next_time": time.time(),
            "interval": TWAP_INTERVAL_SECS,
        }
    )


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
            print(f"HEDGE {h['action']} {h['ticker']} qty={qty}")
            h["remaining"] -= qty
            h["next_time"] = now + h["interval"]
            if h["remaining"] > 0:
                still.append(h)
        except Exception as exc:
            print(f"HEDGE ERROR {h['ticker']}: {exc}")
            h["next_time"] = now + h["interval"]
            still.append(h)
    return still


def evaluate_tender(client: RITClient, tender: dict, tickers: list[str]):
    action = (tender.get("action") or "").upper()
    qty = float(tender.get("quantity") or 0)
    is_fixed = bool(tender.get("is_fixed_bid"))
    price = tender.get("price")
    ticker = infer_ticker(tender, tickers)

    if not ticker or qty <= 0:
        return None

    book = client.get_book(ticker)
    bid, ask = best_bid_ask(book)
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    edge = max(MIN_EDGE, mid * EDGE_BPS / 10000.0)

    if action == "BUY":
        hedge_action = "SELL"
        hedge_price = estimate_fill_price(book.get("bids", []), qty, MAX_LEVELS)
        if hedge_price is None:
            return None
        limit_price = round(hedge_price - edge, 2)
        fixed_accept = price is not None and price <= limit_price
    elif action == "SELL":
        hedge_action = "BUY"
        hedge_price = estimate_fill_price(book.get("asks", []), qty, MAX_LEVELS)
        if hedge_price is None:
            return None
        limit_price = round(hedge_price + edge, 2)
        fixed_accept = price is not None and price >= limit_price
    else:
        return None

    return {
        "ticker": ticker,
        "qty": qty,
        "is_fixed": is_fixed,
        "fixed_accept": fixed_accept,
        "submit_price": limit_price,
        "hedge_action": hedge_action,
        "action": action,
    }


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY environment variable before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)
    tickers = [s["ticker"] for s in client.get_securities()]
    seen = set()
    hedges = []

    print(f"Connected to {BASE_URL}. Monitoring tenders...")

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            print("Case no longer ACTIVE. Exiting.")
            break

        hedges = process_hedges(client, hedges)

        try:
            tenders = client.get_tenders()
        except Exception as exc:
            print(f"TENDER FETCH ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        for tender in tenders:
            tid = tender.get("tender_id")
            if tid in seen:
                continue

            decision = evaluate_tender(client, tender, tickers)
            if decision is None:
                continue

            # Backpressure: avoid accepting new risk if too many hedge jobs are queued.
            if len(hedges) >= MAX_ACTIVE_HEDGES:
                print(f"SKIP tender {tid}: hedge queue full")
                continue

            try:
                if decision["is_fixed"]:
                    if not decision["fixed_accept"]:
                        client.decline_tender(tid)
                        seen.add(tid)
                        print(f"DECLINE fixed tender {tid}")
                        continue
                    client.accept_tender(tid)
                    print(f"ACCEPT fixed tender {tid}")
                else:
                    client.accept_tender(tid, price=decision["submit_price"])
                    print(f"BID auction tender {tid} price={decision['submit_price']}")

                seen.add(tid)
                schedule_twap(hedges, decision["ticker"], decision["hedge_action"], decision["qty"])
            except Exception as exc:
                print(f"TENDER ACTION ERROR {tid}: {exc}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
