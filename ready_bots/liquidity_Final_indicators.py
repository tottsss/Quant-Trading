import os
import signal
import time
import requests


class ApiException(Exception):
    pass


BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
HEADERS = {"X-API-key": API_KEY}
SHUTDOWN = False

# Core strategy params
MIN_EDGE = float(os.environ.get("RIT_FINAL_MIN_EDGE", "0.12"))
VOL_FACTOR = float(os.environ.get("RIT_FINAL_VOL_FACTOR", "1.15"))
MAX_ATTEMPTS = int(os.environ.get("RIT_FINAL_MAX_ATTEMPTS", "8"))
EVAL_DELAY = float(os.environ.get("RIT_FINAL_EVAL_DELAY", "0.8"))
ORDER_DELAY = float(os.environ.get("RIT_FINAL_ORDER_DELAY", "0.12"))
AFTER_ACCEPT_DELAY = float(os.environ.get("RIT_FINAL_AFTER_ACCEPT_DELAY", "0.5"))
ENDGAME_TICKS = int(os.environ.get("RIT_FINAL_ENDGAME_TICKS", "8"))
FIXED_ONLY = os.environ.get("RIT_FINAL_FIXED_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
MAX_ORDER_SIZE = 10000.0

# Indicator params (hedge execution only, no standalone speculative trades)
EMA_FAST = int(os.environ.get("RIT_IND_EMA_FAST", "8"))
EMA_SLOW = int(os.environ.get("RIT_IND_EMA_SLOW", "21"))
RSI_PERIOD = int(os.environ.get("RIT_IND_RSI_PERIOD", "14"))
VOL_PERIOD = int(os.environ.get("RIT_IND_VOL_PERIOD", "20"))
HISTORY_LIMIT = int(os.environ.get("RIT_IND_HISTORY_LIMIT", "80"))


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
            out.append(tk)
            seen.add(tk)
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

    return {
        "books": books,
        "bids": bids,
        "asks": asks,
        "bid_volume": bid_vol,
        "ask_volume": ask_vol,
        "vwap_bid": vwap_bid,
        "vwap_ask": vwap_ask,
    }


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
        time.sleep(0.06)


def _extract_history_points(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("history") or payload.get("data") or payload.get("rows") or []
    else:
        rows = []

    closes = []
    vols = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        px = row.get("close")
        if not isinstance(px, (int, float)):
            px = row.get("last")
        if not isinstance(px, (int, float)):
            px = row.get("price")
        vol = row.get("volume", row.get("qty", 0))

        if isinstance(px, (int, float)):
            closes.append(float(px))
            vols.append(float(vol) if isinstance(vol, (int, float)) else 0.0)
    return closes, vols


def _ema(values, period):
    if len(values) < period or period <= 1:
        return values[-1] if values else 0.0
    k = 2.0 / (period + 1.0)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = (v * k) + (ema_val * (1.0 - k))
    return ema_val


def _rsi(values, period):
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses <= 1e-9:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def get_indicator_state(session, ticker):
    tk = ticker
    r = session.get(f"{BASE_URL}/securities/history", params={"ticker": tk, "limit": HISTORY_LIMIT})
    closes = []
    vols = []
    if r.ok:
        closes, vols = _extract_history_points(r.json())

    if len(closes) < max(EMA_SLOW + 3, RSI_PERIOD + 3):
        return {"ema_fast": 0.0, "ema_slow": 0.0, "rsi": 50.0, "vol_ratio": 1.0, "ready": False}

    ema_fast = _ema(closes, EMA_FAST)
    ema_slow = _ema(closes, EMA_SLOW)
    rsi_val = _rsi(closes, RSI_PERIOD)

    recent_vol = vols[-1] if vols else 0.0
    window = vols[-VOL_PERIOD:] if len(vols) >= VOL_PERIOD else vols
    avg_vol = (sum(window) / len(window)) if window else 0.0
    vol_ratio = (recent_vol / avg_vol) if avg_vol > 1e-9 else 1.0

    return {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": rsi_val,
        "vol_ratio": vol_ratio,
        "ready": True,
    }


def hedge_aggression(inventory, ind):
    # >1.0 = hedge faster/more aggressively; <1.0 = slightly more passive.
    if not ind["ready"]:
        return 1.0

    score = 1.0
    trend_up = ind["ema_fast"] > ind["ema_slow"]
    rsi_val = ind["rsi"]
    vol_ratio = ind["vol_ratio"]

    if inventory > 0:  # need SELL
        if not trend_up:
            score += 0.20
        if rsi_val < 45:
            score += 0.10
        if rsi_val > 65:
            score -= 0.08
    else:  # need BUY
        if trend_up:
            score += 0.20
        if rsi_val > 55:
            score += 0.10
        if rsi_val < 35:
            score -= 0.08

    if vol_ratio < 0.8:
        score += 0.12
    elif vol_ratio > 1.4:
        score -= 0.07

    return max(0.70, min(1.45, score))


def unwind_inventory_indicator(session, ticker, inventory):
    if abs(inventory) < 1:
        return

    ob = get_order_book_agg(session, ticker)
    ind = get_indicator_state(session, ticker)
    aggr = hedge_aggression(inventory, ind)

    remaining = abs(float(inventory))
    market_frac = 0.0
    if aggr >= 1.20:
        market_frac = 0.65
    elif aggr >= 1.05:
        market_frac = 0.35
    elif aggr >= 0.90:
        market_frac = 0.15

    price_offset = 0.0
    if aggr >= 1.20:
        price_offset = 0.02
    elif aggr >= 1.05:
        price_offset = 0.01

    print(
        "HEDGE-IND "
        f"agg={aggr:.2f} rsi={ind['rsi']:.1f} "
        f"ema_fast={ind['ema_fast']:.3f} ema_slow={ind['ema_slow']:.3f} volr={ind['vol_ratio']:.2f}"
    )

    if inventory > 0:
        side = "SELL"
        levels = ob["bids"]
    else:
        side = "BUY"
        levels = ob["asks"]

    if remaining <= 0:
        return

    market_qty = min(remaining, round(remaining * market_frac))
    if market_qty > 0:
        best_tk = levels[0]["ticker"] if levels else ticker
        submit_market_order(session, best_tk, market_qty, side)
        remaining -= market_qty

    for lvl in levels:
        if remaining <= 0:
            break
        q = min(remaining, lvl["quantity"], MAX_ORDER_SIZE)
        if side == "SELL":
            px = max(0.01, lvl["price"] - price_offset)
        else:
            px = max(0.01, lvl["price"] + price_offset)
        submit_limit_order(session, lvl["ticker"], q, px, side)
        remaining -= q
        delay = ORDER_DELAY * (1.2 if aggr < 1.0 else 1.0)
        time.sleep(delay)

    if remaining > 0:
        submit_market_order(session, ticker, remaining, side)


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

    expires = tender.get("expires")
    try:
        tick_now, _, _ = get_tick(session)
        if isinstance(expires, (int, float)):
            ticks_left = max(0, int(expires) - int(tick_now))
            by_time = max(1, int(ticks_left / max(EVAL_DELAY, 0.05)) - 1)
            attempts = max(1, min(attempts, by_time))
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
                if abs(delta) > 0:
                    unwind_inventory_indicator(session, ticker, delta)
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

            for tender in get_tenders(session):
                tid = tender.get("tender_id")
                if tid in processed:
                    continue
                try:
                    evaluate_tender(session, tender)
                except Exception as exc:
                    print(f"Tender error {tid}: {exc}")
                processed.add(tid)

            time.sleep(0.8)


if __name__ == "__main__":
    main()
