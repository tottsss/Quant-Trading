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
FIXED_ONLY = os.environ.get("RIT_FINAL_FIXED_ONLY", "1").strip() in {"1", "true", "yes", "on"}# Whether to only accept fixed tenders
MIN_GROSS_PNL = float(os.environ.get("RIT_FINAL_MIN_GROSS_PNL", "300"))
MIN_PNL_PER_SHARE = float(os.environ.get("RIT_FINAL_MIN_PNL_PER_SHARE", "0.012"))
GROSS_USAGE_CAP = float(os.environ.get("RIT_FINAL_GROSS_USAGE_CAP", "0.90"))
NET_USAGE_CAP = float(os.environ.get("RIT_FINAL_NET_USAGE_CAP", "0.90"))


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


def get_limits(session):
    r = session.get(f"{BASE_URL}/limits")
    if not r.ok:
        raise ApiException("Failed to fetch limits")
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


def unwind_inventory(session, ticker, inventory):
    if abs(inventory) < 1:
        return

    ob = get_order_book_agg(session, ticker)
    remaining = abs(inventory)

    if inventory < 0:
        # We are short -> buy back using marketable buy limits.
        for ask in ob["asks"]:
            if remaining <= 0:
                break
            q = min(remaining, ask["quantity"], MAX_ORDER_SIZE)
            px = max(0.01, ask["price"] + 0.01)
            submit_limit_order(session, ask["ticker"], q, px, "BUY")
            remaining -= q
            time.sleep(ORDER_DELAY)
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
            remaining -= q
            time.sleep(ORDER_DELAY)
        if remaining > 0:
            submit_market_order(session, ticker, remaining, "SELL")


def _infer_my_action(tender):
    caption = str(tender.get("caption", "")).lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"

    tender_action = str(tender.get("action", "")).upper()
    if tender_action == "BUY":
        return "SELL"
    if tender_action == "SELL":
        return "BUY"
    return ""


def _estimate_exec_price(levels, qty):
    remaining = max(0.0, float(qty))
    used = 0.0
    notional = 0.0

    for level in levels:
        if remaining <= 0:
            break
        level_qty = float(level.get("quantity", 0.0))
        if level_qty <= 0:
            continue
        take = min(remaining, level_qty)
        notional += take * float(level["price"])
        used += take
        remaining -= take

    if used <= 0 or remaining > 0:
        return None, used, remaining
    return notional / used, used, remaining


def _extract_fee_per_share(session, ticker):
    base = _base_symbol(ticker)
    fee = 0.0
    for sec in get_securities(session):
        tk = sec.get("ticker")
        if not tk or _base_symbol(tk) != base:
            continue
        for key in ("trading_fee", "fee", "commission"):
            raw = sec.get(key)
            if isinstance(raw, (int, float)):
                fee = max(fee, float(raw))
    return fee


def _infer_limits(limits_payload):
    gross_vals = []
    net_vals = []
    for row in limits_payload:
        g = row.get("gross_limit")
        n = row.get("net_limit")
        if isinstance(g, (int, float)) and g > 0:
            gross_vals.append(float(g))
        if isinstance(n, (int, float)) and n > 0:
            net_vals.append(float(n))
    gross_limit = min(gross_vals) if gross_vals else 250000.0
    net_limit = min(net_vals) if net_vals else 150000.0
    return gross_limit, net_limit


def _projected_risk_ok(session, ticker, my_action, qty):
    if my_action not in {"BUY", "SELL"}:
        return False, "unknown action"

    securities = get_securities(session)
    positions = {s.get("ticker"): float(s.get("position", 0.0)) for s in securities if s.get("ticker")}

    gross_limit, net_limit = _infer_limits(get_limits(session))

    old_pos = float(positions.get(ticker, 0.0))
    delta = float(qty) if my_action == "BUY" else -float(qty)
    new_pos = old_pos + delta

    curr_gross = sum(abs(float(p)) for p in positions.values())
    new_gross = curr_gross - abs(old_pos) + abs(new_pos)
    curr_net = sum(float(p) for p in positions.values())
    new_net = curr_net + delta

    if new_gross > gross_limit * GROSS_USAGE_CAP:
        return False, f"projected gross {new_gross:.0f}/{gross_limit:.0f}"
    if abs(new_net) > net_limit * NET_USAGE_CAP:
        return False, f"projected net {new_net:.0f}/{net_limit:.0f}"
    return True, "ok"


def _tender_is_open(tender):
    status = str(tender.get("status", "")).upper()
    return not status or status in {"OFFERED", "OPEN", "ACTIVE"}


def _has_unresolved_tender_for_base(session, ticker, exclude_tid=None):
    base = _base_symbol(ticker)
    for t in get_tenders(session):
        tid = t.get("tender_id")
        if exclude_tid is not None and tid == exclude_tid:
            continue
        tk = t.get("ticker")
        if tk and _base_symbol(tk) == base and _tender_is_open(t):
            return True
    return False


def evaluate_tender(session, tender):
    ticker = tender.get("ticker")
    tid = tender.get("tender_id")
    raw_price = tender.get("price")
    is_fixed = bool(tender.get("is_fixed_bid"))
    raw_qty = tender.get("quantity")

    if not ticker or tid is None:
        return True, None, 0.0
    if FIXED_ONLY and not is_fixed:
        return decline_tender(session, tender), None, 0.0
    if not isinstance(raw_price, (int, float)):
        return decline_tender(session, tender), None, 0.0
    if not isinstance(raw_qty, (int, float)) or float(raw_qty) <= 0:
        return decline_tender(session, tender), None, 0.0

    tender_price = float(raw_price)
    qty = float(raw_qty)
    attempts = MAX_ATTEMPTS
    queued_hedge_delta = 0.0

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
            return True, None, 0.0
        if not _tender_is_open(live):
            status = str(live.get("status", "")).upper()
            print(f"Tender {tid} status={status}.")
            return True, None, 0.0

        my_action = _infer_my_action(live)
        if my_action not in {"BUY", "SELL"}:
            print(f"Tender {tid} unknown action semantics.")
            continue

        try:
            risk_ok, risk_msg = _projected_risk_ok(session, ticker, my_action, qty)
        except Exception as exc:
            risk_ok, risk_msg = False, f"risk check error: {exc}"
        if not risk_ok:
            print(f"Hold tender {tid}: {risk_msg}")
            continue

        ob = get_order_book_agg(session, ticker)
        fee_per_share = _extract_fee_per_share(session, ticker)
        hedge_sell_px, _, rem_sell = _estimate_exec_price(ob["bids"], qty)
        hedge_buy_px, _, rem_buy = _estimate_exec_price(ob["asks"], qty)

        expected_gross = None
        depth_ok = False
        if my_action == "BUY" and hedge_sell_px is not None:
            expected_gross = (hedge_sell_px - tender_price) * qty - fee_per_share * qty
            depth_ok = rem_sell <= 0
        elif my_action == "SELL" and hedge_buy_px is not None:
            expected_gross = (tender_price - hedge_buy_px) * qty - fee_per_share * qty
            depth_ok = rem_buy <= 0

        min_required = max(MIN_GROSS_PNL, (MIN_EDGE + MIN_PNL_PER_SHARE + fee_per_share) * qty)
        volume_ok = (
            (my_action == "BUY" and ob["bid_volume"] * VOL_FACTOR > ob["ask_volume"])
            or (my_action == "SELL" and ob["ask_volume"] * VOL_FACTOR > ob["bid_volume"])
        )

        if depth_ok and volume_ok and isinstance(expected_gross, (int, float)) and expected_gross >= min_required:
            pre = get_inventory_total(session, ticker)
            accepted = accept_tender(session, tender)
            if accepted:
                time.sleep(AFTER_ACCEPT_DELAY)
                post = get_inventory_total(session, ticker)
                delta = post - pre
                # Position-delta hedging (core anti-fine fix):
                # only unwind the quantity actually added by tender fill.
                if abs(delta) > 0:
                    if _has_unresolved_tender_for_base(session, ticker, exclude_tid=tid):
                        print(f"Queue hedge for {ticker}: unresolved tender still open.")
                        queued_hedge_delta += delta
                    else:
                        unwind_inventory(session, ticker, delta)
            break

        eg = expected_gross if isinstance(expected_gross, (int, float)) else float("nan")
        print(
            f"Evaluating tender {tid} ({i + 1}/{attempts}) "
            f"px={tender_price:.2f} qty={qty:.0f} eg={eg:.2f} min={min_required:.2f} "
            f"vb={ob['vwap_bid']:.2f} va={ob['vwap_ask']:.2f}"
        )

    if not accepted:
        return decline_tender(session, tender), None, 0.0
    return True, ticker, queued_hedge_delta


def add_pending_hedge(pending_hedges, ticker, delta):
    if not ticker or abs(delta) < 1:
        return
    pending_hedges[ticker] = float(pending_hedges.get(ticker, 0.0)) + float(delta)
    if abs(pending_hedges[ticker]) < 1:
        pending_hedges.pop(ticker, None)


def process_pending_hedges(session, pending_hedges):
    for ticker, delta in list(pending_hedges.items()):
        if abs(delta) < 1:
            pending_hedges.pop(ticker, None)
            continue
        try:
            if _has_unresolved_tender_for_base(session, ticker):
                print(f"Hedge hold {ticker}: unresolved tender still open.")
                continue
            unwind_inventory(session, ticker, delta)
            pending_hedges.pop(ticker, None)
        except Exception as exc:
            print(f"Pending hedge error {ticker}: {exc}")


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
    pending_hedges = {}
    with requests.Session() as session:
        session.headers.update(HEADERS)
        while not SHUTDOWN:
            tick, tpp, status = get_tick(session)
            if status != "ACTIVE":
                time.sleep(1.0)
                continue

            process_pending_hedges(session, pending_hedges)

            if tick >= tpp - ENDGAME_TICKS:
                close_positions(session)
                break

            for tender in get_tenders(session):
                tid = tender.get("tender_id")
                if tid in processed:
                    continue
                resolved = False
                try:
                    resolved, hedge_ticker, hedge_delta = evaluate_tender(session, tender)
                    if hedge_ticker and abs(hedge_delta) > 0:
                        add_pending_hedge(pending_hedges, hedge_ticker, hedge_delta)
                except Exception as exc:
                    print(f"Tender error {tid}: {exc}")
                if resolved:
                    processed.add(tid)
            process_pending_hedges(session, pending_hedges)
            time.sleep(1.0)


if __name__ == "__main__":
    main()
