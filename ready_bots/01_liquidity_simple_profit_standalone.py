"""Simple profitable Liquidity Risk bot (standalone).

Strategy:
- Trade fixed tenders only (decline auctions/winner-take-all).
- Accept only if depth-adjusted hedge price gives clear expected profit.
- Hedge accepted tenders immediately with chunked market orders.
- Avoid front-running: do not trade a ticker while any tender on that ticker is unresolved.
- Near end of sub-heat: decline new tenders and flatten inventory.

Run (PowerShell):
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  python .\01_liquidity_simple_profit_standalone.py
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")

POLL_SECS = 0.30
BOOK_LEVELS = 60
MAX_ORDER_QTY = 10000.0
ORDER_SPACING_SECS = 0.07  # keeps us safely under 20 orders/sec
STOP_NEW_TENDERS_TICKS_LEFT = 8
FLATTEN_TICKS_LEFT = 4

# Profit filters (safe defaults)
MIN_EDGE_PER_SHARE = float(os.environ.get("RIT_SIMPLE_MIN_EDGE", "0.03"))
MIN_GROSS_PNL = float(os.environ.get("RIT_SIMPLE_MIN_PNL", "300"))


class RITClient:
    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers.update({"X-API-key": api_key})

    def get(self, path: str, params: dict | None = None):
        r = self.s.get(BASE_URL + path, params=params, timeout=3.0)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, params: dict | None = None):
        r = self.s.post(BASE_URL + path, params=params, timeout=3.0)
        r.raise_for_status()
        return r.json()

    def delete(self, path: str):
        r = self.s.delete(BASE_URL + path, timeout=3.0)
        r.raise_for_status()
        return r.json()


@dataclass
class HedgeJob:
    ticker: str
    action: str
    remaining: float
    chunk: float
    next_time: float


def infer_case_ticks_left(case: dict) -> int | None:
    tick = case.get("tick")
    if not isinstance(tick, (int, float)):
        return None
    tick = int(tick)
    for k in ("ticks_per_period", "period_ticks", "total_ticks", "max_ticks"):
        v = case.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return max(0, int(v) - tick)
    return None


def infer_ticker(tender: dict, valid_tickers: set[str]) -> str | None:
    ticker = tender.get("ticker")
    if ticker in valid_tickers:
        return ticker
    cap = tender.get("caption") or ""
    m = re.search(r"shares of\s+([A-Z0-9_\-]+)", cap, flags=re.IGNORECASE)
    if m:
        tk = m.group(1).upper()
        if tk in valid_tickers:
            return tk
    for tk in valid_tickers:
        if tk in cap:
            return tk
    return None


def infer_my_action(tender: dict) -> str:
    cap = (tender.get("caption") or "").lower()
    if "would you like to sell" in cap:
        return "SELL"
    if "would you like to buy" in cap:
        return "BUY"

    # API action is institution side -> invert for our side.
    a = (tender.get("action") or "").upper()
    if a == "BUY":
        return "SELL"
    if a == "SELL":
        return "BUY"
    return "BUY"


def parse_levels(book: dict, side: str) -> list[tuple[float, float]]:
    out = []
    for lv in book.get(side, []):
        px = lv.get("price")
        qty = lv.get("quantity", lv.get("qty"))
        if isinstance(px, (int, float)) and isinstance(qty, (int, float)) and px > 0 and qty > 0:
            out.append((float(px), float(qty)))
    return out


def depth_avg_price(book: dict, hedge_action: str, qty: float) -> float | None:
    # hedge_action BUY consumes asks; SELL consumes bids.
    side = "asks" if hedge_action == "BUY" else "bids"
    levels = parse_levels(book, side)
    if not levels:
        return None
    if hedge_action == "SELL":
        levels.sort(key=lambda x: x[0], reverse=True)
    else:
        levels.sort(key=lambda x: x[0])

    rem = qty
    notional = 0.0
    used = 0.0
    for px, q in levels:
        if rem <= 0:
            break
        take = min(rem, q)
        rem -= take
        used += take
        notional += take * px
    if rem > 0 or used <= 0:
        return None
    return notional / used


def expected_profit(tender_price: float, my_action: str, est_hedge_px: float, qty: float, fee: float) -> float:
    # fee is per-share trading fee for hedge orders; tender itself has no commission.
    if my_action == "BUY":
        # buy from tender, sell in market
        gross = (est_hedge_px - tender_price) * qty
    else:
        # sell to tender, buy in market
        gross = (tender_price - est_hedge_px) * qty
    return gross - fee * qty


def tender_fill_confirmed(resp: dict) -> bool:
    status = str(resp.get("status") or "").upper()
    if any(x in status for x in ("TRADING_LIMIT", "REJECT", "DECLIN", "ERROR", "CANCEL")):
        return False
    if status and not any(x in status for x in ("ACCEPT", "WON", "FILL", "SUCCESS", "COMPLETE")):
        return False
    return True


def place_market(client: RITClient, ticker: str, qty: float, action: str):
    return client.post(
        "/orders",
        params={"ticker": ticker, "type": "MARKET", "quantity": float(qty), "action": action},
    )


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY first.")

    client = RITClient(API_KEY)
    processed: set[int] = set()
    hedges: list[HedgeJob] = []
    next_order_time = 0.0

    print(f"Connected to {BASE_URL}. Running simple liquidity bot.")

    while True:
        case = client.get("/case")
        if case.get("status") != "ACTIVE":
            print("Case inactive. Exiting.")
            return

        case_ticks_left = infer_case_ticks_left(case)
        now = time.time()

        securities = client.get("/securities")
        sec_by_ticker = {s["ticker"]: s for s in securities if s.get("ticker")}
        positions = {s["ticker"]: float(s.get("position") or 0.0) for s in securities if s.get("ticker")}
        valid_tickers = set(sec_by_ticker.keys())

        tenders = client.get("/tenders")

        # Endgame: decline everything and flatten.
        if case_ticks_left is not None and case_ticks_left <= FLATTEN_TICKS_LEFT:
            for t in tenders:
                tid = t.get("tender_id")
                if tid not in processed:
                    try:
                        client.delete(f"/tenders/{tid}")
                    except Exception:
                        pass
                    processed.add(tid)

            for ticker, pos in positions.items():
                if abs(pos) < 1:
                    continue
                action = "SELL" if pos > 0 else "BUY"
                qty = min(abs(pos), MAX_ORDER_QTY)
                if time.time() < next_order_time:
                    time.sleep(next_order_time - time.time())
                try:
                    place_market(client, ticker, qty, action)
                    print(f"FLATTEN {action} {ticker} qty={qty:.0f} pos={pos:.0f}")
                except Exception as exc:
                    print(f"FLATTEN ERROR {ticker}: {exc}")
                next_order_time = time.time() + ORDER_SPACING_SECS

            time.sleep(POLL_SECS)
            continue

        unresolved_by_ticker: set[str] = set()
        for t in tenders:
            tid = t.get("tender_id")
            if tid in processed:
                continue
            tk = infer_ticker(t, valid_tickers)
            if tk:
                unresolved_by_ticker.add(tk)

        for t in tenders:
            tid = t.get("tender_id")
            if tid in processed:
                continue

            tk = infer_ticker(t, valid_tickers)
            if not tk:
                try:
                    client.delete(f"/tenders/{tid}")
                    processed.add(tid)
                except Exception:
                    pass
                continue

            if case_ticks_left is not None and case_ticks_left <= STOP_NEW_TENDERS_TICKS_LEFT:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(f"DECLINE tender {tid}: endgame")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                processed.add(tid)
                continue

            is_fixed = bool(t.get("is_fixed_bid"))
            if not is_fixed:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(f"DECLINE tender {tid}: non-fixed")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                processed.add(tid)
                continue

            qty = float(t.get("quantity") or 0.0)
            tender_price = t.get("price")
            if qty <= 0 or tender_price is None:
                try:
                    client.delete(f"/tenders/{tid}")
                except Exception:
                    pass
                processed.add(tid)
                continue

            my_action = infer_my_action(t)
            hedge_action = "BUY" if my_action == "SELL" else "SELL"

            try:
                book = client.get("/securities/book", params={"ticker": tk, "limit": BOOK_LEVELS})
            except Exception as exc:
                print(f"HOLD tender {tid}: no book {exc}")
                continue

            est_qty = min(qty, 25000.0)
            est_px = depth_avg_price(book, hedge_action, est_qty)
            if est_px is None:
                print(f"HOLD tender {tid}: insufficient depth")
                continue

            fee = float(sec_by_ticker.get(tk, {}).get("trading_fee") or sec_by_ticker.get(tk, {}).get("fee") or 0.0)
            est_pnl = expected_profit(float(tender_price), my_action, est_px, qty, fee)
            pps = est_pnl / max(1.0, qty)

            if est_pnl < MIN_GROSS_PNL or pps < MIN_EDGE_PER_SHARE:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(f"DECLINE tender {tid}: est_pnl={est_pnl:.2f} pps={pps:.4f}")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                processed.add(tid)
                continue

            try:
                resp = client.post(f"/tenders/{tid}")
                processed.add(tid)
            except Exception as exc:
                print(f"ACCEPT ERROR {tid}: {exc}")
                continue

            if not tender_fill_confirmed(resp):
                print(f"SKIP HEDGE {tid}: unfilled tender response {resp}")
                continue

            print(
                f"ACCEPT tender {tid} {tk} qty={qty:.0f} my_action={my_action} "
                f"hedge={hedge_action} est_pnl={est_pnl:.2f}"
            )

            chunk = min(MAX_ORDER_QTY, float(sec_by_ticker.get(tk, {}).get("max_trade_size") or MAX_ORDER_QTY))
            hedges.append(HedgeJob(ticker=tk, action=hedge_action, remaining=qty, chunk=max(1.0, chunk), next_time=now))

        # Execute hedges only for tickers that are currently not unresolved (avoid front-running fines).
        active_hedges: list[HedgeJob] = []
        for h in hedges:
            if h.remaining <= 0:
                continue
            if h.ticker in unresolved_by_ticker:
                active_hedges.append(h)
                continue
            if time.time() < h.next_time:
                active_hedges.append(h)
                continue
            if time.time() < next_order_time:
                active_hedges.append(h)
                continue

            qty = min(h.remaining, h.chunk)
            try:
                place_market(client, h.ticker, qty, h.action)
                print(f"HEDGE {h.action} {h.ticker} qty={qty:.0f} rem_before={h.remaining:.0f}")
                h.remaining -= qty
                next_order_time = time.time() + ORDER_SPACING_SECS
            except Exception as exc:
                print(f"HEDGE ERROR {h.ticker}: {exc}")
                h.next_time = time.time() + 0.5

            if h.remaining > 0:
                active_hedges.append(h)

        hedges = active_hedges
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
