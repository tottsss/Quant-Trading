import os
import signal
import time
import requests


class ApiException(Exception):
    pass


BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
HEADERS = {"X-API-key": API_KEY}
shutdown = False


def signal_handler(signum, frame):
    del signum, frame
    global shutdown
    shutdown = True
    print("Shutting down...")


signal.signal(signal.SIGINT, signal_handler)


def get_case(session):
    resp = session.get(f"{BASE_URL}/case")
    if not resp.ok:
        raise ApiException("Failed to fetch case")
    return resp.json()


def get_tick(session):
    case = get_case(session)
    return int(case.get("tick", 0)), int(case.get("ticks_per_period", 600)), str(case.get("status", ""))


def get_tenders(session):
    resp = session.get(f"{BASE_URL}/tenders")
    if not resp.ok:
        raise ApiException("Failed to fetch tenders")
    return resp.json()


def get_securities(session):
    resp = session.get(f"{BASE_URL}/securities")
    if not resp.ok:
        raise ApiException("Failed to fetch securities")
    return resp.json()


def _base_symbol(ticker):
    if ticker.endswith("_M") or ticker.endswith("_A"):
        return ticker[:-2]
    return ticker


def _related_tickers(session, ticker):
    all_tickers = {s.get("ticker") for s in get_securities(session) if s.get("ticker")}
    base = _base_symbol(ticker)
    candidates = [ticker, base, f"{base}_M", f"{base}_A"]
    out = []
    seen = set()
    for tk in candidates:
        if tk in all_tickers and tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out or [ticker]


def get_order_book(session, ticker):
    """Fetch aggregated order book stats across related venues."""
    books = {}
    for tk in _related_tickers(session, ticker):
        r = session.get(f"{BASE_URL}/securities/book", params={"ticker": tk, "limit": 50})
        if r.ok:
            books[tk] = r.json()

    all_bids = []
    all_asks = []
    for tk, book in books.items():
        for b in book.get("bids", []):
            q = b.get("quantity", b.get("qty", 0))
            if b.get("price") is not None and q:
                all_bids.append({"ticker": tk, "price": float(b["price"]), "quantity": float(q)})
        for a in book.get("asks", []):
            q = a.get("quantity", a.get("qty", 0))
            if a.get("price") is not None and q:
                all_asks.append({"ticker": tk, "price": float(a["price"]), "quantity": float(q)})

    all_bids.sort(key=lambda x: x["price"], reverse=True)
    all_asks.sort(key=lambda x: x["price"])

    best_bid = all_bids[0]["price"] if all_bids else None
    best_ask = all_asks[0]["price"] if all_asks else None
    bid_volume = sum(x["quantity"] for x in all_bids)
    ask_volume = sum(x["quantity"] for x in all_asks)

    vwap_bid = 0.0
    if bid_volume > 0:
        vwap_bid = sum(x["price"] * x["quantity"] for x in all_bids) / bid_volume
    vwap_ask = 0.0
    if ask_volume > 0:
        vwap_ask = sum(x["price"] * x["quantity"] for x in all_asks) / ask_volume

    return {
        "books": books,
        "bids": all_bids,
        "asks": all_asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "vwap_bid": vwap_bid,
        "vwap_ask": vwap_ask,
    }


def get_inventory(session, ticker):
    base = _base_symbol(ticker)
    total = 0.0
    for s in get_securities(session):
        tk = s.get("ticker")
        if not tk:
            continue
        if _base_symbol(tk) == base:
            total += float(s.get("position", 0.0))
    return total


def accept_tender(session, tender):
    tender_id = tender["tender_id"]
    ticker = tender.get("ticker")
    price = tender.get("price")
    action = tender.get("action")
    resp = session.post(f"{BASE_URL}/tenders/{tender_id}")
    if not resp.ok:
        raise ApiException(f"Failed to accept tender {tender_id} for {ticker} at {price} ({action})")
    print(f"Accepted Tender {tender_id}: {ticker} {action} @ {price}")
    return resp.json() if resp.content else {}


def decline_tender(session, tender):
    tender_id = tender["tender_id"]
    ticker = tender.get("ticker")
    price = tender.get("price")
    action = tender.get("action")
    resp = session.delete(f"{BASE_URL}/tenders/{tender_id}")
    if not resp.ok:
        raise ApiException(f"Failed to decline tender {tender_id} for {ticker} at {price} ({action})")
    print(f"Declined Tender {tender_id}: {ticker} {action} @ {price}")


def submit_limit_order(session, ticker, quantity, price, action):
    order = {"ticker": ticker, "type": "LIMIT", "quantity": quantity, "action": action, "price": price}
    resp = session.post(f"{BASE_URL}/orders", params=order)
    if not resp.ok:
        raise ApiException(f"Failed to place LIMIT order for {ticker} at {price}")
    print(f"Placed {action} LIMIT order: {int(quantity)} @ {price:.2f} on {ticker}")


def submit_market_order(session, ticker, quantity, action):
    quantity = float(quantity)
    while quantity > 0:
        order_size = min(10000.0, quantity)
        order = {"ticker": ticker, "type": "MARKET", "quantity": order_size, "action": action, "price": 0}
        resp = session.post(f"{BASE_URL}/orders", params=order)
        if not resp.ok:
            raise ApiException(f"Failed to place MARKET order for {ticker}")
        print(f"Placed {action} MARKET order: {int(order_size)} on {ticker}")
        quantity -= order_size
        time.sleep(0.08)


def place_aggressive_limit_orders(session, ticker, inventory, order_delay):
    if inventory == 0:
        return

    ob = get_order_book(session, ticker)
    remaining = abs(inventory)

    if inventory < 0:  # Need BUYs to cover short
        for ask in ob["asks"]:
            if remaining <= 0:
                break
            px = max(0.01, ask["price"] + 0.01)  # marketable buy limit
            q = min(remaining, ask["quantity"], 10000.0)
            submit_limit_order(session, ask["ticker"], q, px, "BUY")
            remaining -= q
            time.sleep(order_delay)
    else:  # Need SELLs to unwind long
        for bid in ob["bids"]:
            if remaining <= 0:
                break
            px = max(0.01, bid["price"] - 0.01)  # marketable sell limit
            q = min(remaining, bid["quantity"], 10000.0)
            submit_limit_order(session, bid["ticker"], q, px, "SELL")
            remaining -= q
            time.sleep(order_delay)

    # Safety fallback for any leftover
    if remaining > 0:
        action = "BUY" if inventory < 0 else "SELL"
        submit_market_order(session, ticker, remaining, action)


def evaluate_tender(session, tender):
    """Evaluate tender using the same style method, adapted to current API/tickers."""
    ticker = tender.get("ticker")
    tender_price = float(tender.get("price", 0.0))
    action = str(tender.get("action", "")).upper()  # from source method

    attempts = 0
    max_attempts = 13
    threshold = 0.15
    evaluation_delay = 2.0
    after_accept_delay = 1.5
    order_delay = 0.2
    accepted = False

    while attempts < max_attempts:
        time.sleep(evaluation_delay)

        ob = get_order_book(session, ticker)
        bid_volume = ob["bid_volume"]
        ask_volume = ob["ask_volume"]
        vwap_bid = ob["vwap_bid"]
        vwap_ask = ob["vwap_ask"]

        if action == "BUY" and tender_price < vwap_bid + threshold and bid_volume * 1.2 > ask_volume:
            accept_tender(session, tender)
            accepted = True
            break
        elif action == "SELL" and tender_price > vwap_ask - threshold and ask_volume * 1.2 > bid_volume:
            accept_tender(session, tender)
            accepted = True
            break

        attempts += 1
        print(f"Evaluating tender {tender['tender_id']} ({attempts}/{max_attempts})")

    if not accepted:
        decline_tender(session, tender)
        return

    time.sleep(after_accept_delay)
    inv = get_inventory(session, ticker)
    place_aggressive_limit_orders(session, ticker, inv, order_delay)


def close_positions(session):
    print("Closing all positions before trading ends.")
    sec = get_securities(session)
    for s in sec:
        ticker = s.get("ticker")
        pos = float(s.get("position", 0.0))
        if not ticker or abs(pos) < 1:
            continue
        action = "SELL" if pos > 0 else "BUY"
        submit_market_order(session, ticker, abs(pos), action)


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY env var.")

    processed_tenders = set()
    with requests.Session() as session:
        session.headers.update(HEADERS)

        while not shutdown:
            tick, tpp, status = get_tick(session)
            if status != "ACTIVE":
                time.sleep(1.0)
                continue

            if tick >= tpp - 2:
                close_positions(session)
                break

            tenders = get_tenders(session)
            for tender in tenders:
                tid = tender.get("tender_id")
                if tid in processed_tenders:
                    continue
                evaluate_tender(session, tender)
                processed_tenders.add(tid)

            time.sleep(1.0)


if __name__ == "__main__":
    main()
