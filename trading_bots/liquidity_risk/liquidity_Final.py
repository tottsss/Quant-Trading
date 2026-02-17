import os
import signal
import time
import requests


class ApiException(Exception):
    pass


BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "BNWI101Y")
HEADERS = {"X-API-key": API_KEY}
SHUTDOWN = False


def _env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}

# Strategy params
MIN_EDGE = float(os.environ.get("RIT_FINAL_MIN_EDGE", "0.10")) # Minimum edge for the tender to be accepted, default is 0.15
# Minimum edge for the tender to be accepted


VOL_FACTOR = float(os.environ.get("RIT_FINAL_VOL_FACTOR", "1.2")) # Volume factor for the tender to be accepted, default is 1.2
MAX_ATTEMPTS = int(os.environ.get("RIT_FINAL_MAX_ATTEMPTS", "12"))# Maximum number of attempts to evaluate the tender    
EVAL_DELAY = float(os.environ.get("RIT_FINAL_EVAL_DELAY", "1.0"))# Delay between attempts to evaluate the tender
ORDER_DELAY = float(os.environ.get("RIT_FINAL_ORDER_DELAY", "0.15"))# Delay between orders, default is 0.15
AFTER_ACCEPT_DELAY = float(os.environ.get("RIT_FINAL_AFTER_ACCEPT_DELAY", "0.6"))# Delay after accepting the tender, default is 0.6
MAX_ORDER_SIZE = 10000.0# Maximum order size
ENDGAME_TICKS = int(os.environ.get("RIT_FINAL_ENDGAME_TICKS", "8"))# Number of ticks to end the game
FIXED_ONLY = _env_bool("RIT_FINAL_FIXED_ONLY", "1")# Whether to only accept fixed tenders
PORTFOLIO_PRINT_INTERVAL = float(os.environ.get("RIT_FINAL_PORTFOLIO_PRINT_INTERVAL", "5.0"))


def signal_handler(signum, frame):
    del signum, frame
    global SHUTDOWN
    SHUTDOWN = True
    print("Shutting down...")


signal.signal(signal.SIGINT, signal_handler)


def get_case(session):
    r = session.get(f"{BASE_URL}/case")
    if not r.ok:
        raise ApiException("Failed to fetch case")
    return r.json()


def get_tick(session):
    case = get_case(session)
    return int(case.get("tick", 0)), int(case.get("ticks_per_period", 600)), str(case.get("status", ""))


def get_tenders(session):
    r = session.get(f"{BASE_URL}/tenders")
    if not r.ok:
        raise ApiException("Failed to fetch tenders")
    return r.json()


def get_tender_map(session):
    out = {}
    for t in get_tenders(session):
        tid = t.get("tender_id")
        if tid is not None:
            out[tid] = t
    return out


def get_securities(session):
    r = session.get(f"{BASE_URL}/securities")
    if not r.ok:
        raise ApiException("Failed to fetch securities")
    return r.json()


def _base_symbol(ticker):
    if not ticker:
        return ""
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
    return out or ([ticker] if ticker else [])


def get_order_book_agg(session, ticker):
    books = {}
    for tk in _related_tickers(session, ticker):
        r = session.get(f"{BASE_URL}/securities/book", params={"ticker": tk, "limit": 60})
        if r.ok:
            books[tk] = r.json()

    bids = []
    asks = []
    for tk, book in books.items():
        for b in book.get("bids", []):
            q = b.get("quantity", b.get("qty", 0))
            p = b.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                bids.append({"ticker": tk, "price": float(p), "quantity": float(q)})
        for a in book.get("asks", []):
            q = a.get("quantity", a.get("qty", 0))
            p = a.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                asks.append({"ticker": tk, "price": float(p), "quantity": float(q)})

    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])

    bid_vol = sum(x["quantity"] for x in bids)
    ask_vol = sum(x["quantity"] for x in asks)
    vwap_bid = (sum(x["price"] * x["quantity"] for x in bids) / bid_vol) if bid_vol > 0 else 0.0
    vwap_ask = (sum(x["price"] * x["quantity"] for x in asks) / ask_vol) if ask_vol > 0 else 0.0

    return {"books": books, "bids": bids, "asks": asks, "bid_volume": bid_vol, "ask_volume": ask_vol, "vwap_bid": vwap_bid, "vwap_ask": vwap_ask}


def get_inventory_total(session, ticker):
    base = _base_symbol(ticker)
    total = 0.0
    for s in get_securities(session):
        tk = s.get("ticker")
        if tk and _base_symbol(tk) == base:
            total += float(s.get("position", 0.0))
    return total


def accept_tender(session, tender):
    tid = tender["tender_id"]
    r = session.post(f"{BASE_URL}/tenders/{tid}")
    if not r.ok:
        print(f"Accept failed tender {tid}: status={r.status_code}")
        return False
    print(f"Accepted Tender {tid}: {tender.get('ticker')} {tender.get('action')} @ {tender.get('price')}")
    return True


def decline_tender(session, tender):
    tid = tender["tender_id"]
    r = session.delete(f"{BASE_URL}/tenders/{tid}")
    if not r.ok:
        print(f"Decline failed tender {tid}: status={r.status_code}")
        return False
    print(f"Declined Tender {tid}: {tender.get('ticker')} {tender.get('action')} @ {tender.get('price')}")
    return True


def submit_limit_order(session, ticker, quantity, price, action):
    order = {"ticker": ticker, "type": "LIMIT", "quantity": quantity, "action": action, "price": price}
    r = session.post(f"{BASE_URL}/orders", params=order)
    if not r.ok:
        raise ApiException(f"LIMIT order failed {ticker} {action} {quantity} @ {price}")
    print(f"Placed {action} LIMIT order: {int(quantity)} @ {price:.2f} on {ticker}")


def submit_market_order(session, ticker, quantity, action):
    qty = abs(float(quantity))
    while qty > 0:
        chunk = min(MAX_ORDER_SIZE, qty)
        order = {"ticker": ticker, "type": "MARKET", "quantity": chunk, "action": action}
        r = session.post(f"{BASE_URL}/orders", params=order)
        if not r.ok:
            raise ApiException(f"MARKET order failed {ticker} {action} {chunk}")
        print(f"Placed {action} MARKET order: {int(chunk)} on {ticker}")
        qty -= chunk
        time.sleep(0.08)


def _mark_price(row):
    for key in ("last", "close", "price"):
        px = row.get(key)
        if isinstance(px, (int, float)):
            return float(px)
    return None


def log_portfolio(session, tick, tpp):
    sec = get_securities(session)
    lines = []
    net_pos = 0.0
    gross_pos = 0.0
    gross_notional = 0.0

    for s in sec:
        ticker = s.get("ticker")
        if not ticker:
            continue
        pos = float(s.get("position", 0.0))
        if abs(pos) < 1:
            continue
        px = _mark_price(s)
        net_pos += pos
        gross_pos += abs(pos)
        if px is not None:
            gross_notional += abs(pos * px)
            lines.append(f"{ticker}:{int(pos)}@{px:.2f}")
        else:
            lines.append(f"{ticker}:{int(pos)}")

    if not lines:
        print(f"[PORTFOLIO] tick={tick}/{tpp} flat")
        return

    print(
        f"[PORTFOLIO] tick={tick}/{tpp} net={net_pos:.0f} "
        f"gross={gross_pos:.0f} gross_notional={gross_notional:.2f}"
    )
    print("[PORTFOLIO] " + " | ".join(lines))


def unwind_inventory(session, ticker, inventory):
    if abs(inventory) < 1:
        return

    ob = get_order_book_agg(session, ticker)
    remaining = abs(inventory)
    pos_before = get_inventory_total(session, ticker)

    if inventory < 0:
        # We are short -> buy back using marketable buy limits.
        for ask in ob["asks"]:
            if remaining <= 0:
                break
            q = min(remaining, ask["quantity"], MAX_ORDER_SIZE)
            px = max(0.01, ask["price"] + 0.01)
            submit_limit_order(session, ask["ticker"], q, px, "BUY")
            time.sleep(ORDER_DELAY)
            pos_after = get_inventory_total(session, ticker)
            filled = max(0.0, pos_after - pos_before)
            pos_before = pos_after
            remaining = max(0.0, remaining - filled)
        if remaining > 0:
            submit_market_order(session, ticker, remaining, "BUY")
    else:
        # We are long -> sell using marketable sell limits.
        for bid in ob["bids"]:
            if remaining <= 0:
                break
            q = min(remaining, bid["quantity"], MAX_ORDER_SIZE)
            px = max(0.01, bid["price"] - 0.01)
            submit_limit_order(session, bid["ticker"], q, px, "SELL")
            time.sleep(ORDER_DELAY)
            pos_after = get_inventory_total(session, ticker)
            filled = max(0.0, pos_before - pos_after)
            pos_before = pos_after
            remaining = max(0.0, remaining - filled)
        if remaining > 0:
            submit_market_order(session, ticker, remaining, "SELL")


def _action_edge_ok(action, tender_price, vwap_bid, vwap_ask, bid_volume, ask_volume):
    """
    Hybrid accept rule:
    1) Primary rule: same as sparsh-style method that already worked for you.
    2) Fallback economic edge check: protect against action semantic mismatch in some feeds.
    """
    if action == "BUY":
        # Sparsh-style condition
        cond_primary = (tender_price < (vwap_bid + MIN_EDGE)) and (bid_volume * VOL_FACTOR > ask_volume)
        # Economic fallback (if action semantics are inverted in this server feed)
        cond_fallback = (tender_price - vwap_ask) >= MIN_EDGE
        return cond_primary or cond_fallback
    if action == "SELL":
        # Sparsh-style condition
        cond_primary = (tender_price > (vwap_ask - MIN_EDGE)) and (ask_volume * VOL_FACTOR > bid_volume)
        # Economic fallback
        cond_fallback = (vwap_bid - tender_price) >= MIN_EDGE
        return cond_primary or cond_fallback
    return False


def evaluate_tender(session, tender):
    ticker = tender.get("ticker")
    tid = tender.get("tender_id")
    raw_price = tender.get("price")
    action = str(tender.get("action", "")).upper()
    is_fixed = bool(tender.get("is_fixed_bid"))

    if not ticker or tid is None:
        return
    if FIXED_ONLY and not is_fixed:
        decline_tender(session, tender)
        return
    if not isinstance(raw_price, (int, float)):
        decline_tender(session, tender)
        return

    tender_price = float(raw_price)
    attempts = MAX_ATTEMPTS

    # Bound attempts by remaining tender window.
    expires = tender.get("expires")
    try:
        tick_now, _, _ = get_tick(session)
        if isinstance(expires, (int, float)):
            ticks_left = max(0, int(expires) - int(tick_now))
            attempts = max(1, min(attempts, ticks_left - 1))
    except Exception:
        pass

    accepted = False
    for i in range(attempts):
        time.sleep(EVAL_DELAY)
        live = get_tender_map(session).get(tid)
        if live is None:
            print(f"Tender {tid} unavailable.")
            return
        status = str(live.get("status", "")).upper()
        if status and status not in {"OFFERED", "OPEN", "ACTIVE"}:
            print(f"Tender {tid} status={status}.")
            return

        ob = get_order_book_agg(session, ticker)
        if _action_edge_ok(action, tender_price, ob["vwap_bid"], ob["vwap_ask"], ob["bid_volume"], ob["ask_volume"]):
            pre = get_inventory_total(session, ticker)
            accepted = accept_tender(session, tender)
            if accepted:
                time.sleep(AFTER_ACCEPT_DELAY)
                post = get_inventory_total(session, ticker)
                delta = post - pre
                # Position-delta hedging (core anti-fine fix):
                # only unwind the quantity actually added by tender fill.
                if abs(delta) > 0:
                    unwind_inventory(session, ticker, delta)
            break
        print(
            f"Evaluating tender {tid} ({i + 1}/{attempts}) "
            f"px={tender_price:.2f} vb={ob['vwap_bid']:.2f} va={ob['vwap_ask']:.2f}"
        )

    if not accepted:
        decline_tender(session, tender)


def close_positions(session):
    print("Closing all positions before trading ends.")
    for s in get_securities(session):
        ticker = s.get("ticker")
        pos = float(s.get("position", 0.0))
        if ticker and abs(pos) >= 1:
            action = "SELL" if pos > 0 else "BUY"
            submit_market_order(session, ticker, abs(pos), action)


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY env var.")

    processed = set()
    next_portfolio_print = 0.0
    with requests.Session() as session:
        session.headers.update(HEADERS)
        while not SHUTDOWN:
            tick, tpp, status = get_tick(session)
            if status != "ACTIVE":
                time.sleep(1.0)
                continue

            if tick >= tpp - ENDGAME_TICKS:
                close_positions(session)
                break

            if PORTFOLIO_PRINT_INTERVAL > 0:
                now = time.monotonic()
                if now >= next_portfolio_print:
                    try:
                        log_portfolio(session, tick, tpp)
                    except Exception as exc:
                        print(f"Portfolio log error: {exc}")
                    next_portfolio_print = now + PORTFOLIO_PRINT_INTERVAL

            for tender in get_tenders(session):
                tid = tender.get("tender_id")
                if tid in processed:
                    continue
                try:
                    evaluate_tender(session, tender)
                except Exception as exc:
                    print(f"Tender error {tid}: {exc}")
                    continue
                processed.add(tid)
            time.sleep(1.0)


if __name__ == "__main__":
    main()
