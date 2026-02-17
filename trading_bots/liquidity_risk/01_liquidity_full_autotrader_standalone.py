"""Liquidity Risk full auto-trader (tender-driven, market-order hedging).

What this bot does:
- Automatically evaluates tenders and ACCEPT/DECLINEs them.
- Uses market-order hedges in rate-limited chunks (<= max order size).
- Avoids front-running by not trading a ticker while unresolved tenders exist on it.
- Stops opening new risk near the end and force-flattens inventory.
- Prints fine-watch metrics from /trader and /limits when available.

Important:
- In Liquidity case, "zero fines" can never be guaranteed.
- This strategy is built to minimize fine risk while remaining fully automated.

Run (PowerShell):
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  python .\ready_bots\01_liquidity_full_autotrader_standalone.py
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


POLL_SECS = float(os.environ.get("RIT_POLL_SECS", "0.30"))
BOOK_LEVELS = int(os.environ.get("RIT_BOOK_LEVELS", "80"))
TAS_LIMIT = int(os.environ.get("RIT_TAS_LIMIT", "80"))

ORDER_MIN_SPACING_SECS = float(os.environ.get("RIT_ORDER_MIN_SPACING", "0.07"))
DEFAULT_MAX_ORDER_QTY = 10000.0
HARD_MAX_ORDER_QTY = 10000.0
TWAP_SLICES = int(os.environ.get("RIT_TWAP_SLICES", "10"))
TWAP_INTERVAL_SECS = float(os.environ.get("RIT_TWAP_INTERVAL", "0.30"))
MAX_ACTIVE_HEDGES = int(os.environ.get("RIT_MAX_ACTIVE_HEDGES", "40"))
MAX_PENDING_HEDGE_QTY = float(os.environ.get("RIT_MAX_PENDING_HEDGE_QTY", "120000"))

FIXED_ONLY_MODE = env_flag("RIT_FIXED_ONLY", True)
ENABLE_AUCTION_BIDS = env_flag("RIT_ENABLE_AUCTION", False)
AUCTION_BID_TICKS_LEFT = int(os.environ.get("RIT_AUCTION_BID_TICKS_LEFT", "6"))

STOP_NEW_TENDERS_TICKS_LEFT = int(os.environ.get("RIT_STOP_NEW_TENDERS_TICKS_LEFT", "10"))
FORCE_FLATTEN_TICKS_LEFT = int(os.environ.get("RIT_FORCE_FLATTEN_TICKS_LEFT", "6"))

MIN_EXPECTED_GROSS_PNL = float(os.environ.get("RIT_MIN_GROSS_PNL", "320"))
MIN_EXPECTED_PNL_PER_SHARE = float(os.environ.get("RIT_MIN_PNL_PER_SHARE", "0.012"))
HEDGE_ESTIMATE_QTY_CAP = float(os.environ.get("RIT_HEDGE_EST_QTY_CAP", "30000"))
BASE_EDGE_BPS = float(os.environ.get("RIT_BASE_EDGE_BPS", "6.0"))
SIZE_EDGE_BPS_PER_10K = float(os.environ.get("RIT_SIZE_EDGE_BPS_PER_10K", "2.3"))
MIN_EDGE_ABS = float(os.environ.get("RIT_MIN_EDGE_ABS", "0.03"))
MIN_AUCTION_PRICE = 0.01

GROSS_USAGE_CAP = float(os.environ.get("RIT_GROSS_USAGE_CAP", "0.88"))
NET_USAGE_CAP = float(os.environ.get("RIT_NET_USAGE_CAP", "0.88"))
FALLBACK_GROSS_LIMIT = 250000.0
FALLBACK_NET_LIMIT = 150000.0

FINE_WATCH_EVERY_SECS = 5.0


@dataclass
class TenderPlan:
    tender_id: int
    ticker: str
    qty: float
    my_action: str
    hedge_action: str
    is_fixed: bool
    fixed_accept: bool
    submit_price: float
    edge: float
    est_hedge_px: float
    expected_pnl: float
    reason: str


@dataclass
class HedgeJob:
    ticker: str
    action: str
    remaining: float
    slice_qty: float
    max_order_qty: float
    next_time: float
    interval: float
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


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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


def infer_case_ticks_left(case: dict) -> int | None:
    tick = case.get("tick")
    if not isinstance(tick, (int, float)):
        return None
    tick_i = int(tick)
    for k in ("ticks_per_period", "period_ticks", "total_ticks", "max_ticks"):
        v = case.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return max(0, int(v) - tick_i)
    return None


def parse_levels(book: dict, side: str) -> list[tuple[float, float]]:
    out = []
    for lv in book.get(side, []):
        px = lv.get("price")
        qty = lv.get("quantity", lv.get("qty"))
        if isinstance(px, (int, float)) and isinstance(qty, (int, float)) and px > 0 and qty > 0:
            out.append((float(px), float(qty)))
    return out


def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = parse_levels(book, "bids")
    asks = parse_levels(book, "asks")
    if not bids or not asks:
        return None, None
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return bids[0][0], asks[0][0]


def depth_vwap_for_action(book: dict, action: str, qty: float) -> float | None:
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


def infer_ticker(tender: dict, valid_tickers: set[str]) -> str | None:
    tk = tender.get("ticker")
    if isinstance(tk, str) and tk in valid_tickers:
        return tk
    cap = str(tender.get("caption") or "")
    m = re.search(r"shares of\s+([A-Z0-9_\-]+)", cap, flags=re.IGNORECASE)
    if m:
        c = m.group(1).upper()
        if c in valid_tickers:
            return c
    for c in valid_tickers:
        if c in cap:
            return c
    return None


def infer_my_action(tender: dict) -> str:
    cap = str(tender.get("caption") or "").lower()
    if "would you like to sell" in cap:
        return "SELL"
    if "would you like to buy" in cap:
        return "BUY"
    a = str(tender.get("action") or "").upper()
    if a == "BUY":
        return "SELL"
    if a == "SELL":
        return "BUY"
    return "BUY"


def expected_pnl(my_action: str, tender_price: float, hedge_px: float, qty: float, fee_per_share: float) -> float:
    if my_action == "BUY":
        gross = (hedge_px - tender_price) * qty
    else:
        gross = (tender_price - hedge_px) * qty
    return gross - fee_per_share * qty


def infer_limits(limits_payload) -> tuple[float, float]:
    gross_vals = []
    net_vals = []
    if isinstance(limits_payload, dict):
        limits_payload = [limits_payload]
    for row in (limits_payload or []):
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


def projected_risk_ok(
    positions: dict[str, float], ticker: str, my_action: str, qty: float, gross_limit: float, net_limit: float
) -> tuple[bool, str]:
    old = float(positions.get(ticker, 0.0))
    delta = qty if my_action == "BUY" else -qty
    new = old + delta

    curr_gross = sum(abs(float(v)) for v in positions.values())
    new_gross = curr_gross - abs(old) + abs(new)

    curr_net = sum(float(v) for v in positions.values())
    new_net = curr_net + delta

    if new_gross > GROSS_USAGE_CAP * gross_limit:
        return False, f"gross cap projected={new_gross:.0f} limit={gross_limit:.0f}"
    if abs(new_net) > NET_USAGE_CAP * net_limit:
        return False, f"net cap projected={new_net:.0f} limit={net_limit:.0f}"
    return True, "ok"


def infer_max_order_qty(sec: dict) -> float:
    for k in ("max_trade_size", "max_order_size", "max_trade_qty", "max_order_qty"):
        v = sec.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return min(float(v), HARD_MAX_ORDER_QTY)
    return min(DEFAULT_MAX_ORDER_QTY, HARD_MAX_ORDER_QTY)


def compute_edge(mid: float, spread: float, qty: float, fee_per_share: float) -> float:
    size_bps = SIZE_EDGE_BPS_PER_10K * (qty / 10000.0)
    total_bps = BASE_EDGE_BPS + size_bps
    edge = mid * total_bps / 10000.0
    edge += 0.35 * spread
    edge += fee_per_share
    return max(MIN_EDGE_ABS, edge)


def tender_fill_confirmed(resp) -> bool:
    if not isinstance(resp, dict):
        return True
    status = str(resp.get("status") or "").upper()
    if any(x in status for x in ("TRADING_LIMIT", "REJECT", "DECLIN", "ERROR", "CANCEL")):
        return False
    if status and not any(ok in status for ok in ("ACCEPT", "WON", "FILL", "SUCCESS", "COMPLETE")):
        return False
    return True


def unresolved_tender_tickers(tenders: list[dict], processed_tenders: set[int], valid_tickers: set[str]) -> set[str]:
    out = set()
    for t in tenders:
        tid = t.get("tender_id")
        if tid in processed_tenders:
            continue
        tk = infer_ticker(t, valid_tickers)
        if tk:
            out.add(tk)
    return out


def evaluate_tender(
    tender: dict,
    positions: dict[str, float],
    sec_by_ticker: dict[str, dict],
    valid_tickers: set[str],
    book_by_ticker: dict[str, dict],
    gross_limit: float,
    net_limit: float,
) -> tuple[TenderPlan | None, str]:
    tid = int(tender.get("tender_id") or -1)
    qty_raw = tender.get("quantity")
    qty = abs(float(qty_raw)) if isinstance(qty_raw, (int, float)) else 0.0
    if qty <= 0:
        return None, "invalid qty"

    ticker = infer_ticker(tender, valid_tickers)
    if not ticker:
        return None, "ticker unresolved"

    is_fixed = bool(tender.get("is_fixed_bid"))
    if not is_fixed and (FIXED_ONLY_MODE or not ENABLE_AUCTION_BIDS):
        return None, "auction disabled"

    tender_price = tender.get("price")
    if is_fixed and not isinstance(tender_price, (int, float)):
        return None, "fixed tender missing price"

    my_action = infer_my_action(tender)
    hedge_action = "BUY" if my_action == "SELL" else "SELL"

    ok_risk, reason = projected_risk_ok(positions, ticker, my_action, qty, gross_limit, net_limit)
    if not ok_risk:
        return None, reason

    book = book_by_ticker.get(ticker)
    if not isinstance(book, dict):
        return None, "book unavailable"

    bid, ask = best_bid_ask(book)
    if bid is None or ask is None or ask < bid:
        return None, "bad top of book"
    mid = (bid + ask) / 2.0
    spread = ask - bid

    est_qty = min(qty, HEDGE_ESTIMATE_QTY_CAP)
    est_hedge_px = depth_vwap_for_action(book, hedge_action, est_qty)
    if est_hedge_px is None:
        return None, "insufficient hedge depth"

    sec = sec_by_ticker.get(ticker, {})
    fee_per_share = float(sec.get("trading_fee") or sec.get("fee") or 0.0)
    edge = compute_edge(mid, spread, qty, fee_per_share)

    submit_price = MIN_AUCTION_PRICE
    fixed_accept = False
    exp_pnl = 0.0
    if is_fixed:
        tp = float(tender_price)
        exp_pnl = expected_pnl(my_action, tp, est_hedge_px, qty, fee_per_share)
        pps = exp_pnl / max(1.0, qty)
        if my_action == "BUY":
            fair = est_hedge_px - edge
            fixed_accept = tp <= fair
        else:
            fair = est_hedge_px + edge
            fixed_accept = tp >= fair
        if exp_pnl < MIN_EXPECTED_GROSS_PNL or pps < MIN_EXPECTED_PNL_PER_SHARE:
            fixed_accept = False
        submit_price = round(max(MIN_AUCTION_PRICE, fair), 2)
    else:
        # Auction mode (optional): submit a conservative fair price.
        if my_action == "BUY":
            fair = est_hedge_px - edge
        else:
            fair = est_hedge_px + edge
        submit_price = round(max(MIN_AUCTION_PRICE, fair), 2)

    plan = TenderPlan(
        tender_id=tid,
        ticker=ticker,
        qty=qty,
        my_action=my_action,
        hedge_action=hedge_action,
        is_fixed=is_fixed,
        fixed_accept=fixed_accept,
        submit_price=submit_price,
        edge=edge,
        est_hedge_px=est_hedge_px,
        expected_pnl=exp_pnl,
        reason="ok",
    )
    return plan, "ok"


def schedule_hedge(hedges: list[HedgeJob], ticker: str, action: str, qty: float, max_order_qty: float):
    slices = max(1, TWAP_SLICES)
    hedges.append(
        HedgeJob(
            ticker=ticker,
            action=action,
            remaining=qty,
            slice_qty=max(1.0, qty / slices),
            max_order_qty=max(1.0, max_order_qty),
            next_time=time.time(),
            interval=max(0.05, TWAP_INTERVAL_SECS),
        )
    )


def process_hedges(
    client: RITClient,
    hedges: list[HedgeJob],
    throttle: OrderThrottle,
    blocked_tickers: set[str] | None = None,
) -> list[HedgeJob]:
    blocked_tickers = blocked_tickers or set()
    now = time.time()
    out = []
    for h in hedges:
        if h.remaining <= 0:
            continue
        if h.ticker in blocked_tickers:
            h.next_time = now + max(POLL_SECS, h.interval)
            out.append(h)
            continue
        if now < h.next_time:
            out.append(h)
            continue

        qty = min(h.remaining, h.slice_qty, h.max_order_qty)
        try:
            throttle.wait()
            client.post(
                "/orders",
                {"ticker": h.ticker, "type": "MARKET", "action": h.action, "quantity": float(qty)},
            )
            print(f"HEDGE {h.action} {h.ticker} qty={qty:.0f} rem_before={h.remaining:.0f}")
            h.remaining -= qty
            h.fail_streak = 0
            h.next_time = now + h.interval
            if h.remaining > 0:
                out.append(h)
        except Exception as exc:
            h.fail_streak += 1
            h.max_order_qty = max(250.0, min(h.max_order_qty, qty / 2.0))
            h.next_time = now + h.interval * min(4.0, 1.0 + 0.5 * h.fail_streak)
            print(
                f"HEDGE ERROR {h.ticker} action={h.action} qty={qty:.0f} "
                f"fail_streak={h.fail_streak} new_cap={h.max_order_qty:.0f}: {exc}"
            )
            out.append(h)
    return out


def flatten_positions_step(client: RITClient, positions: dict[str, float], sec_by_ticker: dict[str, dict], throttle: OrderThrottle):
    for tk, pos in positions.items():
        if abs(pos) < 1:
            continue
        action = "SELL" if pos > 0 else "BUY"
        qty = min(abs(pos), infer_max_order_qty(sec_by_ticker.get(tk, {})))
        try:
            throttle.wait()
            client.post("/orders", {"ticker": tk, "type": "MARKET", "action": action, "quantity": float(qty)})
            print(f"FLATTEN {action} {tk} qty={qty:.0f} from_pos={pos:.0f}")
        except Exception as exc:
            print(f"FLATTEN ERROR {tk} action={action} qty={qty:.0f}: {exc}")


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY)
    processed_tenders: set[int] = set()
    hedges: list[HedgeJob] = []
    throttle = OrderThrottle(min_spacing=ORDER_MIN_SPACING_SECS)
    last_fine_watch_at = 0.0

    print(
        f"Connected to {BASE_URL} | full auto trader | fixed_only={FIXED_ONLY_MODE} "
        f"auction={ENABLE_AUCTION_BIDS}"
    )

    while True:
        case = client.get("/case")
        if case.get("status") != "ACTIVE":
            print("Case not ACTIVE. Waiting...")
            time.sleep(POLL_SECS)
            continue

        current_tick = int(case.get("tick", 0))
        case_ticks_left = infer_case_ticks_left(case)

        securities = client.get("/securities")
        sec_by_ticker = {s["ticker"]: s for s in securities if s.get("ticker")}
        valid_tickers = set(sec_by_ticker.keys())
        positions = {tk: float(sec.get("position") or 0.0) for tk, sec in sec_by_ticker.items()}
        max_qty_by_ticker = {tk: infer_max_order_qty(sec) for tk, sec in sec_by_ticker.items()}

        try:
            limits_payload = client.get("/limits")
            gross_limit, net_limit = infer_limits(limits_payload)
        except Exception:
            gross_limit, net_limit = FALLBACK_GROSS_LIMIT, FALLBACK_NET_LIMIT

        # Fine watch
        now = time.time()
        if now - last_fine_watch_at >= FINE_WATCH_EVERY_SECS:
            last_fine_watch_at = now
            fine_guess = 0.0
            try:
                fine_guess += sum_fine_fields(client.get("/trader"))
            except Exception:
                pass
            try:
                fine_guess += sum_fine_fields(client.get("/limits"))
            except Exception:
                pass
            print(f"FINE WATCH guessed_total={fine_guess:.2f}")

        if case_ticks_left is not None and case_ticks_left <= FORCE_FLATTEN_TICKS_LEFT:
            try:
                tenders_now = client.get("/tenders")
            except Exception:
                tenders_now = []
            for t in tenders_now:
                tid = t.get("tender_id")
                if tid in processed_tenders:
                    continue
                try:
                    client.delete(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                except Exception:
                    pass

            print(f"ENDGAME flatten mode: case_ticks_left={case_ticks_left}")
            flatten_positions_step(client, positions, sec_by_ticker, throttle)
            hedges = process_hedges(client, hedges, throttle, blocked_tickers=set())
            time.sleep(POLL_SECS)
            continue

        try:
            tenders = client.get("/tenders")
        except Exception as exc:
            print(f"TENDER FETCH ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        # Preload books once per loop for speed/consistency.
        book_by_ticker = {}
        for tk in valid_tickers:
            try:
                book_by_ticker[tk] = client.get("/securities/book", {"ticker": tk, "limit": BOOK_LEVELS})
            except Exception:
                continue

        for t in tenders:
            tid = t.get("tender_id")
            if tid in processed_tenders:
                continue

            if case_ticks_left is not None and case_ticks_left <= STOP_NEW_TENDERS_TICKS_LEFT:
                try:
                    client.delete(f"/tenders/{tid}")
                    print(f"DECLINE tender {tid}: endgame")
                    processed_tenders.add(tid)
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                continue

            pending_hedge_qty = sum(h.remaining for h in hedges)
            if pending_hedge_qty > MAX_PENDING_HEDGE_QTY:
                print(f"HOLD tender {tid}: pending hedge qty too high ({pending_hedge_qty:.0f})")
                continue

            plan, reason = evaluate_tender(
                t, positions, sec_by_ticker, valid_tickers, book_by_ticker, gross_limit, net_limit
            )
            if plan is None:
                # Immediate decline for disabled/invalid/non-profitable offers.
                try:
                    client.delete(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                    print(f"DECLINE tender {tid}: {reason}")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                continue

            if len(hedges) >= MAX_ACTIVE_HEDGES:
                print(f"HOLD tender {tid}: hedge queue full ({len(hedges)})")
                continue

            expires = t.get("expires")
            ticks_left = None
            if isinstance(expires, (int, float)):
                ticks_left = int(expires) - current_tick

            try:
                if plan.is_fixed:
                    if not plan.fixed_accept:
                        client.delete(f"/tenders/{tid}")
                        processed_tenders.add(tid)
                        print(
                            f"DECLINE fixed tender {tid} {plan.ticker} qty={plan.qty:.0f} "
                            f"est_pnl={plan.expected_pnl:.2f}"
                        )
                        continue

                    resp = client.post(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                    if not tender_fill_confirmed(resp):
                        print(f"SKIP HEDGE fixed tender {tid}: status not confirmed filled ({resp})")
                        continue
                    print(
                        f"ACCEPT fixed tender {tid} {plan.ticker} qty={plan.qty:.0f} "
                        f"my_action={plan.my_action} hedge={plan.hedge_action} est_pnl={plan.expected_pnl:.2f}"
                    )
                else:
                    if ticks_left is not None and ticks_left > AUCTION_BID_TICKS_LEFT:
                        # Avoid keeping long unresolved auction windows in auto mode.
                        client.delete(f"/tenders/{tid}")
                        processed_tenders.add(tid)
                        print(f"DECLINE auction {tid}: too early (ticks_left={ticks_left})")
                        continue

                    if plan.submit_price < MIN_AUCTION_PRICE:
                        client.delete(f"/tenders/{tid}")
                        processed_tenders.add(tid)
                        print(f"DECLINE auction {tid}: invalid submit_price={plan.submit_price:.2f}")
                        continue

                    resp = client.post(f"/tenders/{tid}", {"price": plan.submit_price})
                    processed_tenders.add(tid)
                    if not tender_fill_confirmed(resp):
                        print(f"SKIP HEDGE auction {tid}: status not confirmed filled ({resp})")
                        continue
                    print(
                        f"BID auction tender {tid} {plan.ticker} qty={plan.qty:.0f} "
                        f"price={plan.submit_price:.2f} my_action={plan.my_action}"
                    )

                schedule_hedge(
                    hedges, plan.ticker, plan.hedge_action, plan.qty, max_qty_by_ticker.get(plan.ticker, DEFAULT_MAX_ORDER_QTY)
                )
            except Exception as exc:
                print(f"TENDER ACTION ERROR {tid}: {exc}")

        # Critical anti-front-running control:
        # if a ticker still has unresolved tenders, do not trade that ticker yet.
        blocked = unresolved_tender_tickers(tenders, processed_tenders, valid_tickers)
        hedges = process_hedges(client, hedges, throttle, blocked_tickers=blocked)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
