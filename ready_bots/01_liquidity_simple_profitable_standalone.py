"""Simple, profit-focused Liquidity Risk bot (standalone).

What it does
- Trades fixed tenders only (declines auctions/winner-take-all).
- Accepts only when depth-adjusted hedge economics are clearly positive.
- Enforces gross/net usage buffers before accepting.
- Avoids front-running by pausing hedge trading while unresolved tenders exist on that ticker family.
- Stops opening new tender risk near end and force-flattens inventory.

Run (PowerShell)
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  python .\ready_bots\01_liquidity_simple_profitable_standalone.py
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")

POLL_SECS = float(os.environ.get("RIT_POLL_SECS", "0.30"))
BOOK_LEVELS = int(os.environ.get("RIT_BOOK_LEVELS", "70"))
ORDER_MIN_SPACING_SECS = float(os.environ.get("RIT_ORDER_MIN_SPACING", "0.07"))

DEFAULT_MAX_ORDER_QTY = 10000.0
HARD_MAX_ORDER_QTY = 10000.0
HEDGE_ESTIMATE_QTY_CAP = float(os.environ.get("RIT_HEDGE_EST_QTY_CAP", "30000"))

STOP_NEW_TENDERS_TICKS_LEFT = int(os.environ.get("RIT_STOP_NEW_TENDERS_TICKS_LEFT", "8"))
FORCE_FLATTEN_TICKS_LEFT = int(os.environ.get("RIT_FORCE_FLATTEN_TICKS_LEFT", "4"))

MIN_EXPECTED_GROSS_PNL = float(os.environ.get("RIT_MIN_GROSS_PNL", "320"))
MIN_PPS_BASE = float(os.environ.get("RIT_MIN_PNL_PER_SHARE", "0.02"))
MIN_EDGE_ABS = float(os.environ.get("RIT_MIN_EDGE_ABS", "0.03"))
SPREAD_EDGE_MULT = float(os.environ.get("RIT_SPREAD_EDGE_MULT", "0.25"))
SIZE_EDGE_BPS_PER_10K = float(os.environ.get("RIT_SIZE_EDGE_BPS_PER_10K", "1.8"))

GROSS_USAGE_CAP = float(os.environ.get("RIT_GROSS_USAGE_CAP", "0.88"))
NET_USAGE_CAP = float(os.environ.get("RIT_NET_USAGE_CAP", "0.88"))
FALLBACK_GROSS_LIMIT = 250000.0
FALLBACK_NET_LIMIT = 150000.0

FINE_WATCH_EVERY_SECS = float(os.environ.get("RIT_FINE_WATCH_EVERY_SECS", "5"))


@dataclass
class HedgeJob:
    ticker: str
    action: str
    remaining: float
    chunk_qty: float
    next_time: float
    fail_streak: int = 0


@dataclass
class OrderThrottle:
    min_spacing: float
    next_allowed_at: float = 0.0

    def wait(self):
        now = time.time()
        if now < self.next_allowed_at:
            time.sleep(self.next_allowed_at - now)
            now = time.time()
        self.next_allowed_at = now + self.min_spacing


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


def infer_case_ticks_left(case: dict) -> int | None:
    tick = case.get("tick")
    if not isinstance(tick, (int, float)):
        return None
    tick_i = int(tick)
    for k in ("ticks_per_period", "period_ticks", "total_ticks", "max_ticks"):
        total = case.get(k)
        if isinstance(total, (int, float)) and total > 0:
            return max(0, int(total) - tick_i)
    return None


def base_ticker(ticker: str) -> str:
    if ticker.endswith("_M") or ticker.endswith("_A"):
        return ticker[:-2]
    return ticker


def infer_ticker(tender: dict, valid_tickers: set[str]) -> str | None:
    tk = tender.get("ticker")
    if isinstance(tk, str) and tk in valid_tickers:
        return tk

    caption = str(tender.get("caption") or "")
    m = re.search(r"shares of\s+([A-Z0-9_\-]+)", caption, flags=re.IGNORECASE)
    if m:
        candidate = m.group(1).upper()
        if candidate in valid_tickers:
            return candidate

    for candidate in valid_tickers:
        if candidate in caption:
            return candidate
    return None


def infer_my_action(tender: dict) -> str:
    caption = str(tender.get("caption") or "").lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"
    # API action is institution side; invert for our side.
    action = str(tender.get("action") or "").upper()
    if action == "BUY":
        return "SELL"
    if action == "SELL":
        return "BUY"
    return "BUY"


def parse_levels(book: dict, side: str) -> list[tuple[float, float]]:
    levels = []
    for lv in book.get(side, []):
        px = lv.get("price")
        qty = lv.get("quantity", lv.get("qty"))
        if isinstance(px, (int, float)) and isinstance(qty, (int, float)) and px > 0 and qty > 0:
            levels.append((float(px), float(qty)))
    return levels


def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = parse_levels(book, "bids")
    asks = parse_levels(book, "asks")
    if not bids or not asks:
        return None, None
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return bids[0][0], asks[0][0]


def depth_vwap(book: dict, action: str, qty: float) -> float | None:
    side = "asks" if action == "BUY" else "bids"
    levels = parse_levels(book, side)
    if not levels:
        return None
    levels.sort(key=lambda x: x[0], reverse=(action == "SELL"))
    rem = qty
    used = 0.0
    notional = 0.0
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


def expected_pnl(my_action: str, tender_px: float, hedge_px: float, qty: float, fee_per_share: float) -> float:
    if my_action == "BUY":
        gross = (hedge_px - tender_px) * qty
    else:
        gross = (tender_px - hedge_px) * qty
    return gross - fee_per_share * qty


def tender_fill_confirmed(resp: dict) -> bool:
    status = str(resp.get("status") or "").upper()
    if any(bad in status for bad in ("TRADING_LIMIT", "REJECT", "DECLIN", "ERROR", "CANCEL")):
        return False
    if status and not any(ok in status for ok in ("ACCEPT", "WON", "FILL", "SUCCESS", "COMPLETE")):
        return False
    return True


def infer_limits(limits_payload) -> tuple[float, float]:
    gross_vals = []
    net_vals = []
    rows = limits_payload if isinstance(limits_payload, list) else [limits_payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        g = row.get("gross_limit")
        n = row.get("net_limit")
        if isinstance(g, (int, float)) and g > 0:
            gross_vals.append(float(g))
        if isinstance(n, (int, float)) and n > 0:
            net_vals.append(float(n))
    gross = min(gross_vals) if gross_vals else FALLBACK_GROSS_LIMIT
    net = min(net_vals) if net_vals else FALLBACK_NET_LIMIT
    return gross, net


def projected_usage_after_accept(positions: dict[str, float], ticker: str, delta_qty: float) -> tuple[float, float]:
    gross_now = sum(abs(v) for v in positions.values())
    net_now_signed = sum(v for v in positions.values())
    old_pos = float(positions.get(ticker, 0.0))
    new_pos = old_pos + delta_qty
    gross_projected = gross_now - abs(old_pos) + abs(new_pos)
    net_projected_abs = abs(net_now_signed + delta_qty)
    return gross_projected, net_projected_abs


def dynamic_pps_threshold(mid: float, spread: float, qty: float) -> float:
    size_edge_bps = SIZE_EDGE_BPS_PER_10K * (qty / 10000.0)
    size_edge_abs = mid * size_edge_bps / 10000.0
    spread_edge_abs = spread * SPREAD_EDGE_MULT
    return max(MIN_PPS_BASE, MIN_EDGE_ABS, size_edge_abs, spread_edge_abs)


def sum_fine_fields(obj) -> float:
    total = 0.0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "fine" in str(k).lower() and isinstance(v, (int, float)):
                total += float(v)
            total += sum_fine_fields(v)
    elif isinstance(obj, list):
        for v in obj:
            total += sum_fine_fields(v)
    return total


def place_market(client: RITClient, ticker: str, qty: float, action: str):
    return client.post(
        "/orders",
        params={"ticker": ticker, "type": "MARKET", "quantity": float(qty), "action": action},
    )


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY first.")

    client = RITClient(API_KEY)
    throttle = OrderThrottle(min_spacing=ORDER_MIN_SPACING_SECS)
    processed: set[int] = set()
    hedges: list[HedgeJob] = []
    last_fine_watch = 0.0
    last_fine_total = None

    print(f"Connected to {BASE_URL}. Running simple profitable liquidity bot.")

    while True:
        try:
            case = client.get("/case")
        except Exception as exc:
            print(f"CASE ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        if case.get("status") != "ACTIVE":
            print("Case inactive. Exiting.")
            return

        ticks_left = infer_case_ticks_left(case)
        now = time.time()

        try:
            securities = client.get("/securities")
        except Exception as exc:
            print(f"SECURITIES ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        sec_by_ticker = {s["ticker"]: s for s in securities if s.get("ticker")}
        valid_tickers = set(sec_by_ticker.keys())
        positions = {tk: float(sec_by_ticker[tk].get("position") or 0.0) for tk in valid_tickers}
        fee_by_ticker = {
            tk: float(sec_by_ticker[tk].get("trading_fee") or sec_by_ticker[tk].get("fee") or 0.0)
            for tk in valid_tickers
        }
        max_order_by_ticker = {
            tk: max(
                1.0,
                min(
                    HARD_MAX_ORDER_QTY,
                    float(sec_by_ticker[tk].get("max_trade_size") or DEFAULT_MAX_ORDER_QTY),
                ),
            )
            for tk in valid_tickers
        }

        limits_payload = None
        try:
            limits_payload = client.get("/limits")
        except Exception:
            pass
        gross_limit, net_limit = infer_limits(limits_payload)
        gross_cap = gross_limit * GROSS_USAGE_CAP
        net_cap = net_limit * NET_USAGE_CAP

        try:
            tenders = client.get("/tenders")
        except Exception as exc:
            print(f"TENDERS ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        # Lightweight fine monitor to catch jumps while tuning thresholds.
        if now - last_fine_watch >= FINE_WATCH_EVERY_SECS:
            fine_total = 0.0
            fine_total += sum_fine_fields(limits_payload)
            try:
                fine_total += sum_fine_fields(client.get("/trader"))
            except Exception:
                pass
            if last_fine_total is None or abs(fine_total - last_fine_total) > 1e-6:
                print(f"FINE WATCH total={fine_total:.2f}")
                last_fine_total = fine_total
            last_fine_watch = now

        # Endgame: decline all unresolved tenders and flatten.
        if ticks_left is not None and ticks_left <= FORCE_FLATTEN_TICKS_LEFT:
            for t in tenders:
                tid = t.get("tender_id")
                if tid in processed:
                    continue
                try:
                    client.delete(f"/tenders/{tid}")
                except Exception:
                    pass
                processed.add(tid)

            for tk, pos in positions.items():
                if abs(pos) < 1:
                    continue
                action = "SELL" if pos > 0 else "BUY"
                qty = min(abs(pos), max_order_by_ticker.get(tk, DEFAULT_MAX_ORDER_QTY))
                try:
                    throttle.wait()
                    place_market(client, tk, qty, action)
                    print(f"FLATTEN {action} {tk} qty={qty:.0f} pos={pos:.0f}")
                except Exception as exc:
                    print(f"FLATTEN ERROR {tk}: {exc}")

            time.sleep(POLL_SECS)
            continue

        unresolved_base_tickers: set[str] = set()
        for t in tenders:
            tid = t.get("tender_id")
            if tid in processed:
                continue
            tk = infer_ticker(t, valid_tickers)
            if tk:
                unresolved_base_tickers.add(base_ticker(tk))

        for t in tenders:
            tid = t.get("tender_id")
            if tid in processed:
                continue

            tk = infer_ticker(t, valid_tickers)
            if not tk:
                try:
                    client.delete(f"/tenders/{tid}")
                except Exception:
                    pass
                processed.add(tid)
                continue

            if ticks_left is not None and ticks_left <= STOP_NEW_TENDERS_TICKS_LEFT:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(f"DECLINE tender {tid}: endgame")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                processed.add(tid)
                continue

            if not bool(t.get("is_fixed_bid")):
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
            tender_price_f = float(tender_price)

            try:
                book = client.get("/securities/book", params={"ticker": tk, "limit": BOOK_LEVELS})
            except Exception as exc:
                print(f"HOLD tender {tid}: no book ({exc})")
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                print(f"HOLD tender {tid}: no top-of-book")
                continue

            estimate_qty = min(qty, HEDGE_ESTIMATE_QTY_CAP)
            hedge_px = depth_vwap(book, hedge_action, estimate_qty)
            if hedge_px is None:
                print(f"HOLD tender {tid}: insufficient depth")
                continue

            fee = fee_by_ticker.get(tk, 0.0)
            est_pnl = expected_pnl(my_action, tender_price_f, hedge_px, qty, fee)
            pps = est_pnl / max(1.0, qty)
            mid = (bid + ask) / 2.0
            spread = max(0.0, ask - bid)
            pps_threshold = dynamic_pps_threshold(mid, spread, qty)

            delta_qty = qty if my_action == "BUY" else -qty
            gross_projected, net_projected_abs = projected_usage_after_accept(positions, tk, delta_qty)
            if gross_projected > gross_cap or net_projected_abs > net_cap:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(
                        f"DECLINE tender {tid}: projected limits "
                        f"gross={gross_projected:.0f}/{gross_cap:.0f} net={net_projected_abs:.0f}/{net_cap:.0f}"
                    )
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                processed.add(tid)
                continue

            if est_pnl < MIN_EXPECTED_GROSS_PNL or pps < pps_threshold:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(
                        f"DECLINE tender {tid}: est_pnl={est_pnl:.2f} "
                        f"pps={pps:.4f} need={pps_threshold:.4f}"
                    )
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

            positions[tk] = positions.get(tk, 0.0) + delta_qty
            chunk_qty = min(qty, max_order_by_ticker.get(tk, DEFAULT_MAX_ORDER_QTY))
            hedges.append(
                HedgeJob(
                    ticker=tk,
                    action=hedge_action,
                    remaining=qty,
                    chunk_qty=max(1.0, chunk_qty),
                    next_time=now,
                )
            )
            print(
                f"ACCEPT tender {tid} {tk} qty={qty:.0f} my_action={my_action} "
                f"hedge={hedge_action} est_pnl={est_pnl:.2f} pps={pps:.4f}"
            )

        active_hedges: list[HedgeJob] = []
        for h in hedges:
            if h.remaining <= 0:
                continue

            # Pause hedges when any tender in this ticker family remains unresolved.
            if base_ticker(h.ticker) in unresolved_base_tickers:
                active_hedges.append(h)
                continue

            now_h = time.time()
            if now_h < h.next_time:
                active_hedges.append(h)
                continue

            qty = min(h.remaining, h.chunk_qty)
            try:
                throttle.wait()
                place_market(client, h.ticker, qty, h.action)
                h.remaining -= qty
                h.fail_streak = 0
                h.next_time = time.time() + 0.01
                print(f"HEDGE {h.action} {h.ticker} qty={qty:.0f} rem={h.remaining:.0f}")
            except Exception as exc:
                h.fail_streak += 1
                h.next_time = time.time() + min(2.0, 0.25 * h.fail_streak)
                print(f"HEDGE ERROR {h.ticker}: {exc}")

            if h.remaining > 0:
                active_hedges.append(h)

        hedges = active_hedges
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
