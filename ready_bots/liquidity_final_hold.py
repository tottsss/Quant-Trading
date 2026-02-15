import os
import signal
import time

import requests


class ApiException(Exception):
    pass


STRATEGY_NAME = "liquidity_final_hold"
STRATEGY_LABELS = [
    "hybrid_vwap_volume_edge",
    "delta_based_tender_hedging",
    "aggressive_limit_unwind",
    "endgame_two_phase_flatten",
]


def _env(name, fallback, default):
    return os.environ.get(name, os.environ.get(fallback, default))


BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "BNWI101Y")
HEADERS = {"X-API-key": API_KEY}
shutdown = False

# Strategy params
MIN_EDGE = float(_env("RIT_SPARSH_MIN_EDGE", "RIT_FINAL_MIN_EDGE", "0.10"))
VOL_FACTOR = float(_env("RIT_SPARSH_VOL_FACTOR", "RIT_FINAL_VOL_FACTOR", "1.2"))
MAX_ATTEMPTS = int(_env("RIT_SPARSH_MAX_ATTEMPTS", "RIT_FINAL_MAX_ATTEMPTS", "10"))
EVAL_DELAY = float(_env("RIT_SPARSH_EVAL_DELAY", "RIT_FINAL_EVAL_DELAY", "1.0"))
ORDER_DELAY = float(_env("RIT_SPARSH_ORDER_DELAY", "RIT_FINAL_ORDER_DELAY", "0.15"))
AFTER_ACCEPT_DELAY = float(_env("RIT_SPARSH_AFTER_ACCEPT_DELAY", "RIT_FINAL_AFTER_ACCEPT_DELAY", "0.6"))
MAX_ORDER_SIZE = float(_env("RIT_SPARSH_MAX_ORDER_SIZE", "RIT_FINAL_MAX_ORDER_SIZE", "10000"))

STOP_NEW_TENDERS_TICKS = int(_env("RIT_SPARSH_STOP_NEW_TENDERS_TICKS", "RIT_FINAL_STOP_NEW_TENDERS_TICKS", "14"))
FORCE_FLATTEN_TICKS = int(_env("RIT_SPARSH_FORCE_FLATTEN_TICKS", "RIT_FINAL_ENDGAME_TICKS", "8"))
FORCE_MARKET_TICKS = int(_env("RIT_SPARSH_FORCE_MARKET_TICKS", "RIT_FINAL_FORCE_MARKET_TICKS", "3"))
FLATTEN_LOOP_DELAY = float(_env("RIT_SPARSH_FLATTEN_LOOP_DELAY", "RIT_FINAL_FLATTEN_LOOP_DELAY", "0.6"))
FIXED_ONLY = _env("RIT_SPARSH_FIXED_ONLY", "RIT_FINAL_FIXED_ONLY", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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


def get_tender_map(session):
    out = {}
    for t in get_tenders(session):
        tid = t.get("tender_id")
        if tid is not None:
            out[tid] = t
    return out


def get_securities(session):
    resp = session.get(f"{BASE_URL}/securities")
    if not resp.ok:
        raise ApiException("Failed to fetch securities")
    return resp.json()


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


def get_order_book(session, ticker):
    books = {}
    for tk in _related_tickers(session, ticker):
        r = session.get(f"{BASE_URL}/securities/book", params={"ticker": tk, "limit": 60})
        if r.ok:
            books[tk] = r.json()

    all_bids = []
    all_asks = []
    for tk, book in books.items():
        for b in book.get("bids", []):
            q = b.get("quantity", b.get("qty", 0))
            p = b.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                all_bids.append({"ticker": tk, "price": float(p), "quantity": float(q)})
        for a in book.get("asks", []):
            q = a.get("quantity", a.get("qty", 0))
            p = a.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                all_asks.append({"ticker": tk, "price": float(p), "quantity": float(q)})

    all_bids.sort(key=lambda x: x["price"], reverse=True)
    all_asks.sort(key=lambda x: x["price"])

    bid_volume = sum(x["quantity"] for x in all_bids)
    ask_volume = sum(x["quantity"] for x in all_asks)
    vwap_bid = (sum(x["price"] * x["quantity"] for x in all_bids) / bid_volume) if bid_volume > 0 else 0.0
    vwap_ask = (sum(x["price"] * x["quantity"] for x in all_asks) / ask_volume) if ask_volume > 0 else 0.0

    return {
        "books": books,
        "bids": all_bids,
        "asks": all_asks,
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
        if tk and _base_symbol(tk) == base:
            total += float(s.get("position", 0.0))
    return total


def get_position_exact(session, ticker):
    for s in get_securities(session):
        if s.get("ticker") == ticker:
            return float(s.get("position", 0.0))
    return 0.0


def accept_tender(session, tender):
    tender_id = tender["tender_id"]
    resp = session.post(f"{BASE_URL}/tenders/{tender_id}")
    if not resp.ok:
        print(f"Accept failed tender {tender_id}: status={resp.status_code}")
        return False
    print(
        f"Accepted Tender {tender_id}: "
        f"{tender.get('ticker')} {tender.get('action')} @ {tender.get('price')}"
    )
    return True


def decline_tender(session, tender):
    tender_id = tender["tender_id"]
    resp = session.delete(f"{BASE_URL}/tenders/{tender_id}")
    if not resp.ok:
        print(f"Decline failed tender {tender_id}: status={resp.status_code}")
        return False
    print(
        f"Declined Tender {tender_id}: "
        f"{tender.get('ticker')} {tender.get('action')} @ {tender.get('price')}"
    )
    return True


def submit_limit_order(session, ticker, quantity, price, action):
    order = {"ticker": ticker, "type": "LIMIT", "quantity": quantity, "action": action, "price": price}
    resp = session.post(f"{BASE_URL}/orders", params=order)
    if not resp.ok:
        raise ApiException(f"LIMIT order failed {ticker} {action} {quantity} @ {price}")
    print(f"Placed {action} LIMIT order: {int(quantity)} @ {price:.2f} on {ticker}")


def submit_market_order(session, ticker, quantity, action):
    quantity = abs(float(quantity))
    while quantity > 0:
        order_size = min(MAX_ORDER_SIZE, quantity)
        order = {"ticker": ticker, "type": "MARKET", "quantity": order_size, "action": action}
        resp = session.post(f"{BASE_URL}/orders", params=order)
        if not resp.ok:
            raise ApiException(f"MARKET order failed {ticker} {action} {order_size}")
        print(f"Placed {action} MARKET order: {int(order_size)} on {ticker}")
        quantity -= order_size
        time.sleep(0.08)


def place_aggressive_limit_orders(session, ticker, quantity, action, allow_market_fallback):
    remaining = abs(float(quantity))
    if remaining < 1:
        return 0.0

    ob = get_order_book(session, ticker)
    levels = ob["asks"] if action == "BUY" else ob["bids"]

    for lv in levels:
        if remaining <= 0:
            break
        q = min(remaining, float(lv["quantity"]), MAX_ORDER_SIZE)
        if q <= 0:
            continue

        if action == "BUY":
            px = max(0.01, float(lv["price"]) + 0.01)
        else:
            px = max(0.01, float(lv["price"]) - 0.01)

        try:
            submit_limit_order(session, lv["ticker"], q, px, action)
        except Exception as exc:
            print(f"Aggressive LIMIT error {ticker} {action} qty={q:.0f}: {exc}")
            continue

        remaining -= q
        time.sleep(ORDER_DELAY)

    if remaining > 0 and allow_market_fallback:
        submit_market_order(session, ticker, remaining, action)
        return 0.0
    return remaining


def unwind_inventory(session, ticker, inventory, force_market):
    if abs(inventory) < 1:
        return
    action = "BUY" if inventory < 0 else "SELL"
    remaining = place_aggressive_limit_orders(
        session,
        ticker,
        abs(inventory),
        action,
        allow_market_fallback=force_market,
    )
    if remaining > 0:
        print(
            f"Partial unwind {ticker}: remaining={remaining:.0f} action={action} "
            f"(force_market={force_market})"
        )


def _action_edge_ok(action, tender_price, vwap_bid, vwap_ask, bid_volume, ask_volume):
    if action == "BUY":
        cond_primary = (tender_price < (vwap_bid + MIN_EDGE)) and (bid_volume * VOL_FACTOR > ask_volume)
        cond_fallback = (tender_price - vwap_ask) >= MIN_EDGE
        return cond_primary or cond_fallback
    if action == "SELL":
        cond_primary = (tender_price > (vwap_ask - MIN_EDGE)) and (ask_volume * VOL_FACTOR > bid_volume)
        cond_fallback = (vwap_bid - tender_price) >= MIN_EDGE
        return cond_primary or cond_fallback
    return False


def evaluate_tender(session, tender):
    ticker = tender.get("ticker")
    raw_price = tender.get("price")
    action = str(tender.get("action", "")).upper()
    is_fixed = bool(tender.get("is_fixed_bid"))
    tender_id = tender.get("tender_id")

    if not ticker or tender_id is None:
        return
    if FIXED_ONLY and not is_fixed:
        decline_tender(session, tender)
        return
    if not isinstance(raw_price, (int, float)):
        decline_tender(session, tender)
        return

    tender_price = float(raw_price)
    attempts = MAX_ATTEMPTS

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
        live = get_tender_map(session).get(tender_id)
        if live is None:
            print(f"Tender {tender_id} no longer available.")
            return
        live_status = str(live.get("status", "")).upper()
        if live_status and live_status not in {"OFFERED", "OPEN", "ACTIVE"}:
            print(f"Tender {tender_id} status={live_status}.")
            return

        ob = get_order_book(session, ticker)
        if _action_edge_ok(
            action,
            tender_price,
            ob["vwap_bid"],
            ob["vwap_ask"],
            ob["bid_volume"],
            ob["ask_volume"],
        ):
            pre = get_inventory(session, ticker)
            accepted = accept_tender(session, tender)
            if accepted:
                time.sleep(AFTER_ACCEPT_DELAY)
                post = get_inventory(session, ticker)
                delta = post - pre
                if abs(delta) >= 1:
                    tick_now, tpp, _ = get_tick(session)
                    ticks_left = max(0, tpp - tick_now)
                    unwind_inventory(
                        session,
                        ticker,
                        delta,
                        force_market=(ticks_left <= STOP_NEW_TENDERS_TICKS),
                    )
            break

        print(
            f"Evaluating tender {tender_id} ({i + 1}/{attempts}) "
            f"px={tender_price:.2f} vb={ob['vwap_bid']:.2f} va={ob['vwap_ask']:.2f}"
        )

    if not accepted:
        decline_tender(session, tender)


def decline_unprocessed_tenders(session, tenders, processed_tenders, reason):
    for tender in tenders:
        tid = tender.get("tender_id")
        if tid in processed_tenders:
            continue
        try:
            decline_tender(session, tender)
        except Exception as exc:
            print(f"Decline error {tid}: {exc}")
        processed_tenders.add(tid)
        print(f"Tender {tid} dropped: {reason}")


def close_positions_step(session, ticks_left):
    force_all_market = ticks_left <= FORCE_MARKET_TICKS
    sec = get_securities(session)

    for s in sec:
        ticker = s.get("ticker")
        pos = float(s.get("position", 0.0))
        if not ticker or abs(pos) < 1:
            continue

        action = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)

        if force_all_market:
            submit_market_order(session, ticker, qty, action)
            continue

        place_aggressive_limit_orders(
            session,
            ticker,
            min(qty, MAX_ORDER_SIZE * 2),
            action,
            allow_market_fallback=False,
        )

        if ticks_left <= FORCE_FLATTEN_TICKS:
            live_pos = get_position_exact(session, ticker)
            if abs(live_pos) >= 1 and (live_pos > 0) == (pos > 0):
                market_qty = min(abs(live_pos), MAX_ORDER_SIZE)
                market_action = "SELL" if live_pos > 0 else "BUY"
                submit_market_order(session, ticker, market_qty, market_action)


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY env var.")

    print(
        f"Running {STRATEGY_NAME} | "
        f"strategies={','.join(STRATEGY_LABELS)}"
    )

    processed_tenders = set()
    with requests.Session() as session:
        session.headers.update(HEADERS)

        while not shutdown:
            tick, tpp, status = get_tick(session)
            if status != "ACTIVE":
                time.sleep(1.0)
                continue

            ticks_left = max(0, tpp - tick)

            tenders = get_tenders(session)
            if ticks_left <= FORCE_FLATTEN_TICKS:
                decline_unprocessed_tenders(
                    session,
                    tenders,
                    processed_tenders,
                    reason=f"force_flatten mode (ticks_left={ticks_left})",
                )
                close_positions_step(session, ticks_left)
                time.sleep(max(0.2, FLATTEN_LOOP_DELAY))
                continue

            if ticks_left <= STOP_NEW_TENDERS_TICKS:
                decline_unprocessed_tenders(
                    session,
                    tenders,
                    processed_tenders,
                    reason=f"stop_new_tenders mode (ticks_left={ticks_left})",
                )
                close_positions_step(session, ticks_left)
                time.sleep(max(0.2, FLATTEN_LOOP_DELAY))
                continue

            for tender in tenders:
                tid = tender.get("tender_id")
                if tid in processed_tenders:
                    continue
                try:
                    evaluate_tender(session, tender)
                except ApiException as exc:
                    print(f"Tender error {tid}: {exc}")
                except Exception as exc:
                    print(f"Unexpected tender error {tid}: {exc}")
                processed_tenders.add(tid)

            time.sleep(1.0)


if __name__ == "__main__":
    main()
