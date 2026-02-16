import os
import signal
import time
import json
from datetime import datetime, timezone
from pathlib import Path
import requests


class ApiException(Exception):
    pass


BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "BNWI101Y")
HEADERS = {"X-API-key": API_KEY}
SHUTDOWN = False


def _env(name, fallback, default):
    return os.environ.get(name, os.environ.get(fallback, default))


def _env_bool(name, fallback, default):
    return _env(name, fallback, default).strip().lower() in {"1", "true", "yes", "on"}


# Strategy params
MIN_EDGE = float(_env("RIT_FINAL_RISKY_MIN_EDGE", "RIT_FINAL_MIN_EDGE", "0.03"))
# Minimum edge for the tender to be accepted


VOL_FACTOR = float(_env("RIT_FINAL_RISKY_VOL_FACTOR", "RIT_FINAL_VOL_FACTOR", "0.70"))
MAX_ATTEMPTS = int(_env("RIT_FINAL_RISKY_MAX_ATTEMPTS", "RIT_FINAL_MAX_ATTEMPTS", "20"))
EVAL_DELAY = float(_env("RIT_FINAL_RISKY_EVAL_DELAY", "RIT_FINAL_EVAL_DELAY", "0.5"))
ORDER_DELAY = float(_env("RIT_FINAL_RISKY_ORDER_DELAY", "RIT_FINAL_ORDER_DELAY", "0.05"))
AFTER_ACCEPT_DELAY = float(_env("RIT_FINAL_RISKY_AFTER_ACCEPT_DELAY", "RIT_FINAL_AFTER_ACCEPT_DELAY", "0.20"))
MAX_ORDER_SIZE = 10000.0# Maximum order size
DEPTH_LEVELS = max(1, int(_env("RIT_FINAL_RISKY_DEPTH_LEVELS", "RIT_DEPTH_LEVELS", "10")))
ENDGAME_TICKS = int(_env("RIT_FINAL_RISKY_ENDGAME_TICKS", "RIT_FINAL_ENDGAME_TICKS", "10"))# Number of ticks to end the game
FIXED_ONLY = _env_bool("RIT_FINAL_RISKY_FIXED_ONLY", "RIT_FINAL_FIXED_ONLY", "1")# Whether to only accept fixed tenders
AGGRESSIVE_MODE = _env_bool("RIT_FINAL_RISKY_AGGRESSIVE", "RIT_FINAL_AGGRESSIVE", "1")
EDGE_FLOOR_RATIO = float(_env("RIT_FINAL_RISKY_EDGE_FLOOR_RATIO", "RIT_FINAL_EDGE_FLOOR_RATIO", "0.15"))
EDGE_DECAY_PER_ATTEMPT = float(_env("RIT_FINAL_RISKY_EDGE_DECAY_PER_ATTEMPT", "RIT_FINAL_EDGE_DECAY_PER_ATTEMPT", "0.020"))
VOL_RELAX_PER_ATTEMPT = float(_env("RIT_FINAL_RISKY_VOL_RELAX_PER_ATTEMPT", "RIT_FINAL_VOL_RELAX_PER_ATTEMPT", "0.12"))
HEDGE_RATIO = float(_env("RIT_FINAL_RISKY_HEDGE_RATIO", "RIT_FINAL_HEDGE_RATIO", "0.25" if AGGRESSIVE_MODE else "0.70"))
HEDGE_RATIO = max(0.0, min(1.0, HEDGE_RATIO))
PORTFOLIO_PRINT_INTERVAL = float(_env("RIT_FINAL_RISKY_PORTFOLIO_PRINT_INTERVAL", "RIT_FINAL_PORTFOLIO_PRINT_INTERVAL", "5.0"))
TAKE_PROFIT_ENABLED = _env_bool("RIT_FINAL_RISKY_TAKE_PROFIT_ENABLED", "RIT_FINAL_TAKE_PROFIT_ENABLED", "1")
TAKE_PROFIT_PER_SHARE = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_PER_SHARE", "RIT_FINAL_TAKE_PROFIT_PER_SHARE", "0.15"))
TAKE_PROFIT_CHUNK_QTY = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_CHUNK_QTY", "RIT_FINAL_TAKE_PROFIT_CHUNK_QTY", "10000"))
TAKE_PROFIT_CHUNK_QTY = max(1.0, TAKE_PROFIT_CHUNK_QTY)
TAKE_PROFIT_COOLDOWN = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_COOLDOWN", "RIT_FINAL_TAKE_PROFIT_COOLDOWN", "2.0"))
STOP_LOSS_ENABLED = _env_bool("RIT_FINAL_RISKY_STOP_LOSS_ENABLED", "RIT_FINAL_STOP_LOSS_ENABLED", "0")
STOP_LOSS_PER_SHARE = float(_env("RIT_FINAL_RISKY_STOP_LOSS_PER_SHARE", "RIT_FINAL_STOP_LOSS_PER_SHARE", "0.30"))
STOP_LOSS_CHUNK_QTY = float(_env("RIT_FINAL_RISKY_STOP_LOSS_CHUNK_QTY", "RIT_FINAL_STOP_LOSS_CHUNK_QTY", "10000"))
STOP_LOSS_CHUNK_QTY = max(1.0, STOP_LOSS_CHUNK_QTY)
STOP_LOSS_COOLDOWN = float(_env("RIT_FINAL_RISKY_STOP_LOSS_COOLDOWN", "RIT_FINAL_STOP_LOSS_COOLDOWN", "2.0"))
BOOK_OUTLIER_BPS = float(_env("RIT_FINAL_RISKY_BOOK_OUTLIER_BPS", "RIT_FINAL_BOOK_OUTLIER_BPS", "60"))
BOOK_OUTLIER_SPREAD_MULT = float(_env("RIT_FINAL_RISKY_BOOK_OUTLIER_SPREAD_MULT", "RIT_FINAL_BOOK_OUTLIER_SPREAD_MULT", "8.0"))
BOOK_MAX_LEVEL_QTY = float(_env("RIT_FINAL_RISKY_BOOK_MAX_LEVEL_QTY", "RIT_FINAL_BOOK_MAX_LEVEL_QTY", "25000"))
BOOK_DECISION_QTY_CAP = float(_env("RIT_FINAL_RISKY_BOOK_DECISION_QTY_CAP", "RIT_FINAL_BOOK_DECISION_QTY_CAP", "15000"))
MOM_TAS_LIMIT = max(10, int(_env("RIT_FINAL_RISKY_MOM_TAS_LIMIT", "RIT_FINAL_MOM_TAS_LIMIT", "60")))
MOM_EMA_FAST = max(2, int(_env("RIT_FINAL_RISKY_MOM_EMA_FAST", "RIT_FINAL_MOM_EMA_FAST", "8")))
MOM_EMA_SLOW = max(MOM_EMA_FAST + 1, int(_env("RIT_FINAL_RISKY_MOM_EMA_SLOW", "RIT_FINAL_MOM_EMA_SLOW", "21")))
MOM_RSI_PERIOD = max(3, int(_env("RIT_FINAL_RISKY_MOM_RSI_PERIOD", "RIT_FINAL_MOM_RSI_PERIOD", "14")))
MOM_IMBALANCE_LEVELS = max(1, int(_env("RIT_FINAL_RISKY_MOM_IMBALANCE_LEVELS", "RIT_FINAL_MOM_IMBALANCE_LEVELS", "5")))
MOM_IMBALANCE_THRESHOLD = float(_env("RIT_FINAL_RISKY_MOM_IMBALANCE_THRESHOLD", "RIT_FINAL_MOM_IMBALANCE_THRESHOLD", "0.15"))
FLATTEN_FAVORABLE_MULT = float(_env("RIT_FINAL_RISKY_FLATTEN_FAVORABLE_MULT", "RIT_FINAL_FLATTEN_FAVORABLE_MULT", "0.70"))
FLATTEN_ADVERSE_MULT = float(_env("RIT_FINAL_RISKY_FLATTEN_ADVERSE_MULT", "RIT_FINAL_FLATTEN_ADVERSE_MULT", "1.40"))
FLATTEN_MIN_TICKET_QTY = float(_env("RIT_FINAL_RISKY_FLATTEN_MIN_TICKET_QTY", "RIT_FINAL_FLATTEN_MIN_TICKET_QTY", "2000"))
FLATTEN_HARD_DEADLINE_TICKS = max(
    1,
    int(_env("RIT_FINAL_RISKY_FLATTEN_HARD_DEADLINE_TICKS", "RIT_FINAL_FLATTEN_HARD_DEADLINE_TICKS", "2")),
)
SAVE_REPORT_ON_EXIT = _env_bool("RIT_FINAL_RISKY_SAVE_REPORT_ON_EXIT", "RIT_FINAL_SAVE_REPORT_ON_EXIT", "1")
REPORT_PREFIX = _env("RIT_FINAL_RISKY_REPORT_PREFIX", "RIT_FINAL_REPORT_PREFIX", "final_risky_report")


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


def _safe_get_json(session, path, params=None):
    try:
        r = session.get(f"{BASE_URL}{path}", params=params)
        if not r.ok:
            return {
                "_error": f"http_{r.status_code}",
                "_path": path,
                "_params": params or {},
            }
        return r.json()
    except Exception as exc:
        return {
            "_error": str(exc),
            "_path": path,
            "_params": params or {},
        }


def _compute_position_summary(securities):
    if not isinstance(securities, list):
        return {}

    net_position = 0.0
    gross_position = 0.0
    gross_notional = 0.0
    by_ticker = {}
    for s in securities:
        ticker = s.get("ticker")
        if not ticker:
            continue
        pos = float(s.get("position", 0.0))
        if abs(pos) < 1:
            continue
        px = _mark_price(s)
        by_ticker[ticker] = {"position": pos, "mark_price": px}
        net_position += pos
        gross_position += abs(pos)
        if isinstance(px, (int, float)):
            gross_notional += abs(pos * px)
    return {
        "net_position": net_position,
        "gross_position": gross_position,
        "gross_notional": gross_notional,
        "open_positions": by_ticker,
    }


def save_run_report(session, reason, run_error=None):
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    out_path = Path(__file__).resolve().parent / f"{REPORT_PREFIX}_{stamp}.json"

    case_info = _safe_get_json(session, "/case")
    trader_info = _safe_get_json(session, "/trader")
    limits_info = _safe_get_json(session, "/limits")
    securities = _safe_get_json(session, "/securities")
    tenders = _safe_get_json(session, "/tenders")
    orders_all = _safe_get_json(session, "/orders")
    orders_open = _safe_get_json(session, "/orders", params={"status": "OPEN"})
    orders_transacted = _safe_get_json(session, "/orders", params={"status": "TRANSACTED"})
    orders_cancelled = _safe_get_json(session, "/orders", params={"status": "CANCELLED"})

    report = {
        "saved_at_utc": ts.isoformat(),
        "script": str(Path(__file__).resolve()),
        "base_url": BASE_URL,
        "exit_reason": reason,
        "run_error": run_error,
        "config": {
            "MIN_EDGE": MIN_EDGE,
            "VOL_FACTOR": VOL_FACTOR,
            "MAX_ATTEMPTS": MAX_ATTEMPTS,
            "EVAL_DELAY": EVAL_DELAY,
            "ORDER_DELAY": ORDER_DELAY,
            "AFTER_ACCEPT_DELAY": AFTER_ACCEPT_DELAY,
            "MAX_ORDER_SIZE": MAX_ORDER_SIZE,
            "DEPTH_LEVELS": DEPTH_LEVELS,
            "ENDGAME_TICKS": ENDGAME_TICKS,
            "FIXED_ONLY": FIXED_ONLY,
            "AGGRESSIVE_MODE": AGGRESSIVE_MODE,
            "HEDGE_RATIO": HEDGE_RATIO,
            "TAKE_PROFIT_ENABLED": TAKE_PROFIT_ENABLED,
            "TAKE_PROFIT_PER_SHARE": TAKE_PROFIT_PER_SHARE,
            "TAKE_PROFIT_CHUNK_QTY": TAKE_PROFIT_CHUNK_QTY,
            "TAKE_PROFIT_COOLDOWN": TAKE_PROFIT_COOLDOWN,
            "STOP_LOSS_ENABLED": STOP_LOSS_ENABLED,
            "STOP_LOSS_PER_SHARE": STOP_LOSS_PER_SHARE,
            "STOP_LOSS_CHUNK_QTY": STOP_LOSS_CHUNK_QTY,
            "STOP_LOSS_COOLDOWN": STOP_LOSS_COOLDOWN,
            "BOOK_OUTLIER_BPS": BOOK_OUTLIER_BPS,
            "BOOK_OUTLIER_SPREAD_MULT": BOOK_OUTLIER_SPREAD_MULT,
            "BOOK_MAX_LEVEL_QTY": BOOK_MAX_LEVEL_QTY,
            "BOOK_DECISION_QTY_CAP": BOOK_DECISION_QTY_CAP,
            "MOM_TAS_LIMIT": MOM_TAS_LIMIT,
            "MOM_EMA_FAST": MOM_EMA_FAST,
            "MOM_EMA_SLOW": MOM_EMA_SLOW,
            "MOM_RSI_PERIOD": MOM_RSI_PERIOD,
            "MOM_IMBALANCE_LEVELS": MOM_IMBALANCE_LEVELS,
            "MOM_IMBALANCE_THRESHOLD": MOM_IMBALANCE_THRESHOLD,
            "FLATTEN_FAVORABLE_MULT": FLATTEN_FAVORABLE_MULT,
            "FLATTEN_ADVERSE_MULT": FLATTEN_ADVERSE_MULT,
            "FLATTEN_MIN_TICKET_QTY": FLATTEN_MIN_TICKET_QTY,
            "FLATTEN_HARD_DEADLINE_TICKS": FLATTEN_HARD_DEADLINE_TICKS,
        },
        "case": case_info,
        "trader": trader_info,
        "limits": limits_info,
        "securities": securities,
        "position_summary": _compute_position_summary(securities),
        "tenders_active": tenders,
        "orders": {
            "all": orders_all,
            "open": orders_open,
            "transacted": orders_transacted,
            "cancelled": orders_cancelled,
        },
    }

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(f"[REPORT] saved: {out_path}")
    return out_path


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
    fetch_limit = max(20, DEPTH_LEVELS * 3)
    for tk in _related_tickers(session, ticker):
        r = session.get(f"{BASE_URL}/securities/book", params={"ticker": tk, "limit": fetch_limit})
        if r.ok:
            books[tk] = r.json()

    raw_bids = []
    raw_asks = []
    for tk, book in books.items():
        for b in book.get("bids", []):
            q = b.get("quantity", b.get("qty", 0))
            p = b.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                raw_bids.append({"ticker": tk, "price": float(p), "quantity": float(q)})
        for a in book.get("asks", []):
            q = a.get("quantity", a.get("qty", 0))
            p = a.get("price")
            if isinstance(p, (int, float)) and isinstance(q, (int, float)) and q > 0:
                raw_asks.append({"ticker": tk, "price": float(p), "quantity": float(q)})

    raw_bids.sort(key=lambda x: x["price"], reverse=True)
    raw_asks.sort(key=lambda x: x["price"])

    best_bid = raw_bids[0]["price"] if raw_bids else None
    best_ask = raw_asks[0]["price"] if raw_asks else None
    mid = None
    spread = None
    max_dev = None
    if best_bid is not None and best_ask is not None:
        spread = max(0.01, best_ask - best_bid)
        mid = (best_bid + best_ask) / 2.0
        max_dev = max(
            max(0.01, mid) * (BOOK_OUTLIER_BPS / 10000.0),
            spread * BOOK_OUTLIER_SPREAD_MULT,
            0.02,
        )

    def _cap_qty(qty):
        q = float(qty)
        if BOOK_MAX_LEVEL_QTY > 0:
            q = min(q, BOOK_MAX_LEVEL_QTY)
        return q

    bids = []
    for lv in raw_bids:
        if max_dev is not None and lv["price"] < (best_bid - max_dev):
            continue
        q = _cap_qty(lv["quantity"])
        if q <= 0:
            continue
        bids.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})
        if len(bids) >= DEPTH_LEVELS:
            break

    asks = []
    for lv in raw_asks:
        if max_dev is not None and lv["price"] > (best_ask + max_dev):
            continue
        q = _cap_qty(lv["quantity"])
        if q <= 0:
            continue
        asks.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})
        if len(asks) >= DEPTH_LEVELS:
            break

    if not bids and raw_bids:
        for lv in raw_bids[:DEPTH_LEVELS]:
            q = _cap_qty(lv["quantity"])
            if q <= 0:
                continue
            bids.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})
    if not asks and raw_asks:
        for lv in raw_asks[:DEPTH_LEVELS]:
            q = _cap_qty(lv["quantity"])
            if q <= 0:
                continue
            asks.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})

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
        "best_bid": bids[0]["price"] if bids else best_bid,
        "best_ask": asks[0]["price"] if asks else best_ask,
        "mid": mid,
        "spread": spread,
        "book_outlier_max_dev": max_dev,
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


def _cost_basis(row):
    for key in ("cost", "vwap"):
        px = row.get(key)
        if isinstance(px, (int, float)):
            return float(px)
    return None


def _infer_my_action(tender):
    caption = str(tender.get("caption") or "").lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"

    # API action is often institution side. Invert by default when caption is absent.
    action = str(tender.get("action") or "").upper()
    if action == "BUY":
        return "SELL"
    if action == "SELL":
        return "BUY"
    return action


def _tender_quantity(tender):
    qty = tender.get("quantity")
    if isinstance(qty, (int, float)) and qty > 0:
        return float(qty)
    return 0.0


def _weighted_exec_price(levels, target_qty):
    if not levels:
        return None

    remaining = max(1.0, float(target_qty))
    notional = 0.0
    filled = 0.0
    for lv in levels:
        px = lv.get("price")
        qty = lv.get("quantity")
        if not isinstance(px, (int, float)) or not isinstance(qty, (int, float)):
            continue
        if qty <= 0:
            continue
        take = min(remaining, float(qty))
        notional += float(px) * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    if filled <= 0:
        return None
    return notional / filled


def _get_tas_prices(session, ticker, limit):
    try:
        r = session.get(f"{BASE_URL}/securities/tas", params={"ticker": ticker, "limit": limit})
        if not r.ok:
            return []
        rows = r.json()
    except Exception:
        return []

    prices = []
    for row in rows if isinstance(rows, list) else []:
        px = row.get("price")
        if isinstance(px, (int, float)):
            prices.append(float(px))
    return prices


def _ema(values, period):
    if not values:
        return None
    alpha = 2.0 / (period + 1.0)
    out = float(values[0])
    for v in values[1:]:
        out = (alpha * float(v)) + ((1.0 - alpha) * out)
    return out


def _rsi(values, period):
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = float(values[i]) - float(values[i - 1])
        if delta > 0:
            gains += delta
        elif delta < 0:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _order_book_imbalance(ob, levels):
    bids = ob.get("bids", [])[:levels]
    asks = ob.get("asks", [])[:levels]
    bid_qty = sum(float(x.get("quantity", 0.0)) for x in bids)
    ask_qty = sum(float(x.get("quantity", 0.0)) for x in asks)
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total


def _flatten_regime(session, ticker, unwind_action, ob):
    prices = _get_tas_prices(session, ticker, MOM_TAS_LIMIT)
    ema_fast = _ema(prices, MOM_EMA_FAST) if prices else None
    ema_slow = _ema(prices, MOM_EMA_SLOW) if prices else None
    rsi = _rsi(prices, MOM_RSI_PERIOD) if prices else None
    imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)

    favorable = 0
    adverse = 0

    if ema_fast is not None and ema_slow is not None:
        trend_up = ema_fast >= ema_slow
        if unwind_action == "SELL":
            favorable += 1 if trend_up else 0
            adverse += 0 if trend_up else 1
        else:
            favorable += 0 if trend_up else 1
            adverse += 1 if trend_up else 0

    if rsi is not None:
        if unwind_action == "SELL":
            if rsi >= 52:
                favorable += 1
            elif rsi <= 48:
                adverse += 1
        else:
            if rsi <= 48:
                favorable += 1
            elif rsi >= 52:
                adverse += 1

    if unwind_action == "SELL":
        if imbalance >= MOM_IMBALANCE_THRESHOLD:
            favorable += 1
        elif imbalance <= -MOM_IMBALANCE_THRESHOLD:
            adverse += 1
    else:
        if imbalance <= -MOM_IMBALANCE_THRESHOLD:
            favorable += 1
        elif imbalance >= MOM_IMBALANCE_THRESHOLD:
            adverse += 1

    if adverse >= 2 and adverse > favorable:
        state = "ADVERSE"
    elif favorable >= 2 and favorable >= adverse:
        state = "FAVORABLE"
    else:
        state = "NEUTRAL"

    return {
        "state": state,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": rsi,
        "imbalance": imbalance,
    }


def _is_unresolved_tender(tender):
    status = str(tender.get("status", "")).upper()
    if not status:
        return True
    return status in {"OFFERED", "OPEN", "ACTIVE", "PENDING"}


def _unresolved_tender_bases(session):
    unresolved_bases = set()
    for t in get_tenders(session):
        tk = t.get("ticker")
        if not tk:
            continue
        if _is_unresolved_tender(t):
            unresolved_bases.add(_base_symbol(tk))
    return unresolved_bases


def maybe_take_profit_cover(session, last_tp_by_ticker):
    if not TAKE_PROFIT_ENABLED or TAKE_PROFIT_PER_SHARE <= 0:
        return

    unresolved_bases = _unresolved_tender_bases(session)

    now = time.monotonic()
    for s in get_securities(session):
        ticker = s.get("ticker")
        if not ticker:
            continue
        if _base_symbol(ticker) in unresolved_bases:
            continue
        pos = float(s.get("position", 0.0))
        if abs(pos) < 1:
            continue
        cost = _cost_basis(s)
        if cost is None:
            continue
        if now - last_tp_by_ticker.get(ticker, 0.0) < TAKE_PROFIT_COOLDOWN:
            continue

        ob = get_order_book_agg(session, ticker)
        if pos < 0:
            if not ob["asks"]:
                continue
            exec_px = ob["asks"][0]["price"]
            edge = cost - exec_px
            action = "BUY"
        else:
            if not ob["bids"]:
                continue
            exec_px = ob["bids"][0]["price"]
            edge = exec_px - cost
            action = "SELL"

        if edge < TAKE_PROFIT_PER_SHARE:
            continue

        cover_qty = min(abs(pos), TAKE_PROFIT_CHUNK_QTY)
        if cover_qty < 1:
            continue

        submit_market_order(session, ticker, cover_qty, action)
        last_tp_by_ticker[ticker] = now
        print(
            f"[TAKE_PROFIT] {ticker} {action} {cover_qty:.0f} "
            f"edge={edge:.3f} cost={cost:.2f} exec={exec_px:.2f}"
        )


def maybe_stop_loss_cut(session, last_sl_by_ticker):
    if not STOP_LOSS_ENABLED or STOP_LOSS_PER_SHARE <= 0:
        return

    unresolved_bases = _unresolved_tender_bases(session)

    now = time.monotonic()
    for s in get_securities(session):
        ticker = s.get("ticker")
        if not ticker:
            continue
        if _base_symbol(ticker) in unresolved_bases:
            continue
        pos = float(s.get("position", 0.0))
        if abs(pos) < 1:
            continue
        cost = _cost_basis(s)
        if cost is None:
            continue
        if now - last_sl_by_ticker.get(ticker, 0.0) < STOP_LOSS_COOLDOWN:
            continue

        ob = get_order_book_agg(session, ticker)
        if pos < 0:
            if not ob["asks"]:
                continue
            exec_px = ob["asks"][0]["price"]
            loss = exec_px - cost
            action = "BUY"
        else:
            if not ob["bids"]:
                continue
            exec_px = ob["bids"][0]["price"]
            loss = cost - exec_px
            action = "SELL"

        if loss < STOP_LOSS_PER_SHARE:
            continue

        cut_qty = min(abs(pos), STOP_LOSS_CHUNK_QTY)
        if cut_qty < 1:
            continue

        submit_market_order(session, ticker, cut_qty, action)
        last_sl_by_ticker[ticker] = now
        print(
            f"[STOP_LOSS] {ticker} {action} {cut_qty:.0f} "
            f"loss={loss:.3f} cost={cost:.2f} exec={exec_px:.2f}"
        )


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


def _action_edge_ok(my_action, tender_price, ob, tender_qty, attempt_idx):
    """
    Decide tender acceptance from executable economics:
    BUY tender -> hedge by selling into bids.
    SELL tender -> hedge by buying from asks.
    """
    eff_edge = MIN_EDGE
    if AGGRESSIVE_MODE:
        eff_edge = max(MIN_EDGE * EDGE_FLOOR_RATIO, MIN_EDGE - (EDGE_DECAY_PER_ATTEMPT * attempt_idx))

    decision_qty = max(1.0, min(float(tender_qty), BOOK_DECISION_QTY_CAP))
    imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)

    if my_action == "BUY":
        edge_top = None
        if ob.get("best_bid") is not None:
            edge_top = float(ob["best_bid"]) - tender_price

        edge_exec = None
        exec_px = _weighted_exec_price(ob.get("bids", []), decision_qty)
        if exec_px is not None:
            edge_exec = exec_px - tender_price

        edge = max([e for e in (edge_top, edge_exec) if e is not None], default=None)
        return (edge is not None and edge >= eff_edge), edge, eff_edge, imbalance, edge_top, edge_exec

    if my_action == "SELL":
        edge_top = None
        if ob.get("best_ask") is not None:
            edge_top = tender_price - float(ob["best_ask"])

        edge_exec = None
        exec_px = _weighted_exec_price(ob.get("asks", []), decision_qty)
        if exec_px is not None:
            edge_exec = tender_price - exec_px

        edge = max([e for e in (edge_top, edge_exec) if e is not None], default=None)
        return (edge is not None and edge >= eff_edge), edge, eff_edge, imbalance, edge_top, edge_exec

    return False, None, eff_edge, imbalance, None, None


def evaluate_tender(session, tender):
    ticker = tender.get("ticker")
    tid = tender.get("tender_id")
    raw_price = tender.get("price")
    my_action = _infer_my_action(tender) or str(tender.get("action", "")).upper()
    is_fixed = bool(tender.get("is_fixed_bid"))
    tender_qty = _tender_quantity(tender)

    if not ticker or tid is None:
        return
    if not is_fixed:
        if not FIXED_ONLY:
            print(f"Tender {tid} non-fixed; auction pricing disabled for this bot. Declining.")
        decline_tender(session, tender)
        return
    if not isinstance(raw_price, (int, float)):
        decline_tender(session, tender)
        return
    if tender_qty <= 0:
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
        live_price = live.get("price")
        if isinstance(live_price, (int, float)):
            tender_price = float(live_price)
        live_qty = _tender_quantity(live)
        if live_qty > 0:
            tender_qty = live_qty
        live_action = _infer_my_action(live) or my_action

        ob = get_order_book_agg(session, ticker)
        edge_ok, edge, eff_edge, imbalance, edge_top, edge_exec = _action_edge_ok(
            live_action,
            tender_price,
            ob,
            tender_qty,
            i,
        )
        if edge_ok:
            pre = get_inventory_total(session, ticker)
            accepted = accept_tender(session, live)
            if accepted:
                time.sleep(AFTER_ACCEPT_DELAY)
                post = get_inventory_total(session, ticker)
                delta = post - pre
                # Position-delta hedging (core anti-fine fix):
                # only unwind the quantity actually added by tender fill.
                if abs(delta) > 0:
                    hedge_delta = delta * HEDGE_RATIO
                    unwind_inventory(session, ticker, hedge_delta)
                    retained = delta - hedge_delta
                    if abs(retained) >= 1:
                        print(
                            f"Holding risk on {ticker}: retained={retained:.0f} "
                            f"(hedge_ratio={HEDGE_RATIO:.2f})"
                        )
                else:
                    print(f"Tender {tid} accepted but no fill delta (likely lost auction or reserve miss).")
            break
        print(
            f"Evaluating tender {tid} ({i + 1}/{attempts}) "
            f"px={tender_price:.2f} qty={tender_qty:.0f} side={live_action} "
            f"edge={edge if edge is not None else 'N/A'} req={eff_edge:.3f} "
            f"top={edge_top if edge_top is not None else 'N/A'} "
            f"exec={edge_exec if edge_exec is not None else 'N/A'} "
            f"imb={imbalance:.2f}"
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


def _ticket_qty_for_state(abs_pos, ticks_to_end, state):
    base = abs_pos / max(1.0, float(ticks_to_end))
    base = max(1.0, base)
    ticket = max(base, min(FLATTEN_MIN_TICKET_QTY, abs_pos))

    if state == "FAVORABLE":
        ticket *= FLATTEN_FAVORABLE_MULT
    elif state == "ADVERSE":
        ticket *= FLATTEN_ADVERSE_MULT

    ticket = max(1.0, ticket)
    ticket = min(abs_pos, MAX_ORDER_SIZE, ticket)
    return ticket


def flatten_positions_ticketed(session, tick, tpp):
    ticks_to_end = max(0, int(tpp) - int(tick))
    any_pos = False
    for s in get_securities(session):
        ticker = s.get("ticker")
        pos = float(s.get("position", 0.0))
        if not ticker or abs(pos) < 1:
            continue

        any_pos = True
        unwind_action = "SELL" if pos > 0 else "BUY"
        abs_pos = abs(pos)
        ob = get_order_book_agg(session, ticker)
        regime = _flatten_regime(session, ticker, unwind_action, ob)

        ticket_qty = _ticket_qty_for_state(abs_pos, ticks_to_end, regime["state"])
        if ticks_to_end <= FLATTEN_HARD_DEADLINE_TICKS + 1:
            ticket_qty = min(abs_pos, MAX_ORDER_SIZE)

        submit_market_order(session, ticker, ticket_qty, unwind_action)
        ema_fast = regime["ema_fast"]
        ema_slow = regime["ema_slow"]
        rsi = regime["rsi"]
        ema_fast_s = f"{ema_fast:.3f}" if isinstance(ema_fast, (int, float)) else "N/A"
        ema_slow_s = f"{ema_slow:.3f}" if isinstance(ema_slow, (int, float)) else "N/A"
        rsi_s = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "N/A"
        print(
            f"[ENDGAME] ticker={ticker} action={unwind_action} "
            f"ticket={ticket_qty:.0f}/{abs_pos:.0f} state={regime['state']} "
            f"ema_f={ema_fast_s} ema_s={ema_slow_s} rsi={rsi_s} "
            f"imb={regime['imbalance']:.2f} ticks_to_end={ticks_to_end}"
        )

    return not any_pos


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY env var.")

    processed = set()
    next_portfolio_print = 0.0
    last_tp_by_ticker = {}
    last_sl_by_ticker = {}
    exit_reason = "shutdown_or_manual_stop"
    run_error = None
    with requests.Session() as session:
        session.headers.update(HEADERS)
        try:
            while not SHUTDOWN:
                tick, tpp, status = get_tick(session)
                if status != "ACTIVE":
                    time.sleep(1.0)
                    continue

                if tick >= tpp - ENDGAME_TICKS:
                    try:
                        flat_now = flatten_positions_ticketed(session, tick, tpp)
                    except Exception as exc:
                        print(f"Endgame ticketed flatten error: {exc}")
                        flat_now = False

                    if tick >= tpp - FLATTEN_HARD_DEADLINE_TICKS:
                        close_positions(session)
                        exit_reason = "endgame_hard_deadline_flatten"
                        break

                    if flat_now:
                        exit_reason = "endgame_ticketed_flatten"
                        break

                    time.sleep(1.0)
                    continue

                try:
                    maybe_stop_loss_cut(session, last_sl_by_ticker)
                except Exception as exc:
                    print(f"Stop-loss error: {exc}")

                try:
                    maybe_take_profit_cover(session, last_tp_by_ticker)
                except Exception as exc:
                    print(f"Take-profit error: {exc}")

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
        except Exception as exc:
            run_error = str(exc)
            exit_reason = "fatal_error"
            print(f"Fatal error: {exc}")
        finally:
            if SAVE_REPORT_ON_EXIT:
                try:
                    save_run_report(session, exit_reason, run_error=run_error)
                except Exception as exc:
                    print(f"Report save error: {exc}")


if __name__ == "__main__":
    main()
