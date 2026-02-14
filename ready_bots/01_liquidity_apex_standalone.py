"""Liquidity Risk APEX bot (standalone).

Design goals
- Evaluate tenders using depth-based executable hedge price (not only top-of-book).
- Use dynamic edge: spread/size/volatility/fee aware.
- Respect gross/net and max-order constraints.
- Hedge quickly with chunked market orders and retry/backoff.
- Keep tenders alive until close to expiry; do not decline too early.

Runtime requirements
- pip install requests
- Set env vars:
  - RIT_API_KEY
  - RIT_BASE_URL (default http://localhost:9999/v1)
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = 0.35
BOOK_LEVELS = 100
TAS_LIMIT = 80
VOL_MIN_POINTS = 12

# Edge model
BASE_EDGE_BPS = 5.0
SIZE_EDGE_BPS_PER_10K = 2.5
MAX_VOL_EDGE_BPS = 14.0
VOL_TO_BPS_MULT = 3000.0  # bps contribution from realized short-horizon log-return stdev
MIN_EDGE_ABS = 0.03

# Tender handling timing
FIXED_DECLINE_TICKS_LEFT = 1
AUCTION_BID_TICKS_LEFT = 6
LOG_HOLD_EVERY_SECS = 3.0

# Hedge controls
TWAP_SLICES = 8
TWAP_INTERVAL_SECS = 0.4
MAX_ACTIVE_HEDGES = 30
DEFAULT_MAX_ORDER_QTY = 10000.0
MIN_AUCTION_PRICE = 0.01

# Estimate only this much depth for very large tenders; add size edge for larger sizes.
HEDGE_ESTIMATE_QTY_CAP = 25000.0

# Risk buffers
GROSS_USAGE_CAP = 0.90
NET_USAGE_CAP = 0.90
FALLBACK_GROSS_LIMIT = 250000.0
FALLBACK_NET_LIMIT = 150000.0


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

    def get_limits(self):
        r = self._get("/limits")
        r.raise_for_status()
        return r.json()

    def get_securities(self, ticker: str | None = None):
        params = {"ticker": ticker} if ticker else None
        r = self._get("/securities", params=params)
        r.raise_for_status()
        return r.json()

    def get_tenders(self):
        r = self._get("/tenders")
        r.raise_for_status()
        return r.json()

    def get_book(self, ticker: str, limit: int | None = None):
        params = {"ticker": ticker}
        if limit is not None:
            params["limit"] = limit
        r = self._get("/securities/book", params=params)
        r.raise_for_status()
        return r.json()

    def get_tas(self, ticker: str, limit: int = 40):
        r = self._get("/securities/tas", {"ticker": ticker, "limit": limit})
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


@dataclass
class TenderDecision:
    ticker: str
    qty: float
    my_action: str
    hedge_action: str
    is_fixed: bool
    fixed_accept: bool
    submit_price: float
    estimate_qty: float
    edge: float
    estimate_px: float


@dataclass
class HedgeJob:
    ticker: str
    action: str
    remaining: float
    slice_qty: float
    next_time: float
    interval: float
    max_order_qty: float
    fail_streak: int = 0


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return case
        time.sleep(poll_s)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def infer_ticker(tender: dict, valid_tickers: set[str]) -> str | None:
    t = tender.get("ticker")
    if t and t in valid_tickers:
        return t

    caption = tender.get("caption") or ""
    m = re.search(r"shares of\s+([A-Z0-9_\-]+)", caption, re.IGNORECASE)
    if m:
        parsed = m.group(1).upper()
        if parsed in valid_tickers:
            return parsed

    for tk in valid_tickers:
        if tk in caption:
            return tk
    return None


def infer_my_action(tender: dict) -> str:
    caption = (tender.get("caption") or "").lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"

    # Fallback: API action appears to be the institution side.
    tender_action = (tender.get("action") or "").upper()
    if tender_action == "BUY":
        return "SELL"
    if tender_action == "SELL":
        return "BUY"
    return "BUY"


def build_candidate_tickers(ticker: str, valid_tickers: set[str]) -> list[str]:
    cands = [ticker]
    if ticker.endswith("_M") or ticker.endswith("_A"):
        base = ticker[:-2]
        cands.extend([base, f"{base}_M", f"{base}_A"])
    else:
        cands.extend([f"{ticker}_M", f"{ticker}_A"])

    out = []
    seen = set()
    for c in cands:
        if c in valid_tickers and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def parse_levels_for_side(book: dict, side: str):
    levels = []
    raw = book.get(side, [])
    for lv in raw:
        px = lv.get("price")
        q = lv.get("quantity")
        if q is None:
            q = lv.get("qty")
        if px is None or q is None:
            continue
        if q <= 0:
            continue
        levels.append((float(px), float(q)))
    return levels


def estimate_exec_price(client: RITClient, candidate_tickers: list[str], exec_action: str, qty: float):
    # exec_action is our market hedge action: BUY consumes asks, SELL consumes bids.
    side = "asks" if exec_action == "BUY" else "bids"
    all_levels = []

    for tk in candidate_tickers:
        try:
            book = client.get_book(tk, limit=BOOK_LEVELS)
        except Exception:
            continue
        levels = parse_levels_for_side(book, side)
        for px, q in levels:
            all_levels.append((px, q, tk))

    if not all_levels:
        return None, 0.0, qty

    reverse = exec_action == "SELL"  # Sell uses highest bids first.
    all_levels.sort(key=lambda x: x[0], reverse=reverse)

    rem = qty
    used = 0.0
    notional = 0.0
    for px, q, _tk in all_levels:
        if rem <= 0:
            break
        take = min(rem, q)
        notional += take * px
        used += take
        rem -= take

    if used <= 0:
        return None, used, rem
    if rem > 0:
        return None, used, rem
    return notional / used, used, rem


def realized_vol_from_tas(client: RITClient, ticker: str) -> float:
    try:
        tas = client.get_tas(ticker, limit=TAS_LIMIT)
    except Exception:
        return 0.0

    prices = []
    for row in tas:
        p = row.get("price")
        if isinstance(p, (int, float)) and p > 0:
            prices.append(float(p))

    if len(prices) < VOL_MIN_POINTS:
        return 0.0

    rets = []
    for i in range(1, len(prices)):
        a, b = prices[i - 1], prices[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))

    if len(rets) < VOL_MIN_POINTS - 1:
        return 0.0

    mean = sum(rets) / len(rets)
    var = sum((r - mean) * (r - mean) for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(max(0.0, var))


def infer_limits(limits_payload) -> tuple[float, float]:
    gross_vals = []
    net_vals = []
    for row in limits_payload:
        g = row.get("gross_limit")
        n = row.get("net_limit")
        if isinstance(g, (int, float)) and g > 0:
            gross_vals.append(float(g))
        if isinstance(n, (int, float)) and n > 0:
            net_vals.append(float(n))

    gross = min(gross_vals) if gross_vals else FALLBACK_GROSS_LIMIT
    net = min(net_vals) if net_vals else FALLBACK_NET_LIMIT
    return gross, net


def projected_risk_ok(positions: dict[str, float], ticker: str, my_action: str, qty: float, gross_limit: float, net_limit: float) -> tuple[bool, str]:
    old = float(positions.get(ticker, 0.0))
    delta = qty if my_action == "BUY" else -qty
    new = old + delta

    curr_gross = sum(abs(float(v)) for v in positions.values())
    new_gross = curr_gross - abs(old) + abs(new)

    curr_net = sum(float(v) for v in positions.values())
    new_net = curr_net + delta

    if new_gross > GROSS_USAGE_CAP * gross_limit:
        return False, f"gross cap breach projected={new_gross:.0f} limit={gross_limit:.0f}"
    if abs(new_net) > NET_USAGE_CAP * net_limit:
        return False, f"net cap breach projected={new_net:.0f} limit={net_limit:.0f}"

    return True, "ok"


def compute_dynamic_edge(mid: float, spread: float, qty: float, vol: float, fee: float) -> float:
    size_bps = SIZE_EDGE_BPS_PER_10K * (qty / 10000.0)
    vol_bps = clamp(vol * VOL_TO_BPS_MULT, 0.0, MAX_VOL_EDGE_BPS)
    spread_bps = (spread / max(1e-6, mid)) * 10000.0 * 0.15

    total_bps = BASE_EDGE_BPS + size_bps + vol_bps + spread_bps
    edge = mid * total_bps / 10000.0
    edge += fee
    return max(MIN_EDGE_ABS, edge)


def evaluate_tender(
    client: RITClient,
    tender: dict,
    positions: dict[str, float],
    security_info: dict[str, dict],
    valid_tickers: set[str],
    gross_limit: float,
    net_limit: float,
):
    qty = float(tender.get("quantity") or 0.0)
    if qty <= 0:
        return None, "invalid qty"

    ticker = infer_ticker(tender, valid_tickers)
    if not ticker:
        return None, "ticker not resolved"

    my_action = infer_my_action(tender)
    hedge_action = "BUY" if my_action == "SELL" else "SELL"

    ok_risk, risk_reason = projected_risk_ok(positions, ticker, my_action, qty, gross_limit, net_limit)
    if not ok_risk:
        return None, risk_reason

    # For execution estimate, combine visible books of related tickers when available.
    cands = build_candidate_tickers(ticker, valid_tickers)
    estimate_qty = min(qty, HEDGE_ESTIMATE_QTY_CAP)
    est_px, used, rem = estimate_exec_price(client, cands, hedge_action, estimate_qty)
    if est_px is None:
        return None, f"insufficient depth (filled={used:.0f}, rem={rem:.0f}, est_qty={estimate_qty:.0f})"

    # Get local top-of-book for spread and mid stats.
    try:
        own_book = client.get_book(ticker, limit=1)
    except Exception:
        own_book = {}
    bids = own_book.get("bids", [])
    asks = own_book.get("asks", [])
    if not bids or not asks:
        return None, "empty top book"
    bid = float(bids[0].get("price", 0.0))
    ask = float(asks[0].get("price", 0.0))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, "invalid top book"

    mid = (bid + ask) / 2.0
    spread = max(0.0, ask - bid)

    sec = security_info.get(ticker, {})
    fee = float(sec.get("trading_fee") or 0.0)
    vol = realized_vol_from_tas(client, ticker)
    edge = compute_dynamic_edge(mid, spread, qty, vol, fee)

    tender_price = tender.get("price")
    is_fixed = bool(tender.get("is_fixed_bid"))

    if my_action == "BUY":
        # We BUY from tender, then SELL in market at est_px.
        fair = est_px - edge
        fixed_accept = tender_price is not None and float(tender_price) <= fair
        submit_price = max(MIN_AUCTION_PRICE, round(fair, 2))
    else:
        # We SELL to tender, then BUY in market at est_px.
        fair = est_px + edge
        fixed_accept = tender_price is not None and float(tender_price) >= fair
        submit_price = max(MIN_AUCTION_PRICE, round(fair, 2))

    return (
        TenderDecision(
            ticker=ticker,
            qty=qty,
            my_action=my_action,
            hedge_action=hedge_action,
            is_fixed=is_fixed,
            fixed_accept=fixed_accept,
            submit_price=submit_price,
            estimate_qty=estimate_qty,
            edge=edge,
            estimate_px=est_px,
        ),
        "ok",
    )


def infer_max_order_qty(sec: dict) -> float:
    for key in ("max_trade_size", "max_order_size", "max_trade_qty", "limit"):
        v = sec.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return DEFAULT_MAX_ORDER_QTY


def schedule_hedge(hedges: list[HedgeJob], ticker: str, action: str, qty: float, max_order_qty: float):
    slices = max(1, TWAP_SLICES)
    slice_qty = max(1.0, qty / slices)
    hedges.append(
        HedgeJob(
            ticker=ticker,
            action=action,
            remaining=qty,
            slice_qty=slice_qty,
            next_time=time.time(),
            interval=TWAP_INTERVAL_SECS,
            max_order_qty=max(1.0, max_order_qty),
        )
    )


def process_hedges(client: RITClient, hedges: list[HedgeJob]) -> list[HedgeJob]:
    now = time.time()
    still = []
    for h in hedges:
        if h.remaining <= 0:
            continue
        if now < h.next_time:
            still.append(h)
            continue

        qty = min(h.slice_qty, h.remaining, h.max_order_qty)
        try:
            client.place_order(h.ticker, "MARKET", qty, h.action)
            print(f"HEDGE {h.action} {h.ticker} qty={qty:.0f} rem_before={h.remaining:.0f}")
            h.remaining -= qty
            h.fail_streak = 0
            h.next_time = now + h.interval
            if h.remaining > 0:
                still.append(h)
        except Exception as exc:
            h.fail_streak += 1
            # Adaptive backoff: reduce cap after repeated failures.
            h.max_order_qty = max(250.0, min(h.max_order_qty, qty / 2.0))
            h.next_time = now + h.interval * min(4.0, 1.0 + h.fail_streak * 0.5)
            print(
                f"HEDGE ERROR {h.ticker} action={h.action} qty={qty:.0f} "
                f"fail_streak={h.fail_streak} new_cap={h.max_order_qty:.0f}: {exc}"
            )
            still.append(h)
    return still


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY environment variable before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    processed_tenders = set()
    last_hold_log_at = {}
    hedges: list[HedgeJob] = []

    print(f"Connected to {BASE_URL}. Running Liquidity APEX bot...")

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            print("Case no longer ACTIVE. Exiting.")
            break

        current_tick = int(case.get("tick", 0))

        # Refresh market/risk state each loop (simple and robust).
        securities = client.get_securities()
        valid_tickers = {s["ticker"] for s in securities if s.get("ticker")}
        sec_by_ticker = {s["ticker"]: s for s in securities if s.get("ticker")}
        positions = {s["ticker"]: float(s.get("position") or 0.0) for s in securities if s.get("ticker")}
        max_qty_by_ticker = {tk: infer_max_order_qty(sec_by_ticker.get(tk, {})) for tk in valid_tickers}

        try:
            gross_limit, net_limit = infer_limits(client.get_limits())
        except Exception:
            gross_limit, net_limit = FALLBACK_GROSS_LIMIT, FALLBACK_NET_LIMIT

        hedges = process_hedges(client, hedges)

        try:
            tenders = client.get_tenders()
        except Exception as exc:
            print(f"TENDER FETCH ERROR: {exc}")
            time.sleep(POLL_SECS)
            continue

        for tender in tenders:
            tid = tender.get("tender_id")
            if tid in processed_tenders:
                continue

            decision, reason = evaluate_tender(
                client,
                tender,
                positions,
                sec_by_ticker,
                valid_tickers,
                gross_limit,
                net_limit,
            )

            expires = tender.get("expires")
            ticks_left = None
            if isinstance(expires, (int, float)):
                ticks_left = int(expires) - current_tick

            if decision is None:
                if ticks_left is not None and ticks_left <= FIXED_DECLINE_TICKS_LEFT and tender.get("is_fixed_bid"):
                    try:
                        client.decline_tender(tid)
                        processed_tenders.add(tid)
                        print(f"DECLINE fixed tender {tid}: {reason}")
                    except Exception as exc:
                        print(f"DECLINE ERROR {tid}: {exc}")
                else:
                    now = time.time()
                    if now - last_hold_log_at.get(tid, 0.0) >= LOG_HOLD_EVERY_SECS:
                        print(f"HOLD tender {tid}: {reason}")
                        last_hold_log_at[tid] = now
                continue

            if len(hedges) >= MAX_ACTIVE_HEDGES:
                print(f"HOLD tender {tid}: hedge queue full ({len(hedges)})")
                continue

            try:
                if decision.is_fixed:
                    if not decision.fixed_accept:
                        if ticks_left is not None and ticks_left <= FIXED_DECLINE_TICKS_LEFT:
                            client.decline_tender(tid)
                            processed_tenders.add(tid)
                            print(
                                f"DECLINE fixed tender {tid} ticker={decision.ticker} qty={decision.qty:.0f} "
                                f"edge={decision.edge:.4f} est_px={decision.estimate_px:.4f}"
                            )
                        else:
                            now = time.time()
                            if now - last_hold_log_at.get(tid, 0.0) >= LOG_HOLD_EVERY_SECS:
                                print(
                                    f"HOLD fixed tender {tid} ticker={decision.ticker} qty={decision.qty:.0f} "
                                    f"fair_submit={decision.submit_price:.2f}"
                                )
                                last_hold_log_at[tid] = now
                        continue

                    client.accept_tender(tid)
                    processed_tenders.add(tid)
                    print(
                        f"ACCEPT fixed tender {tid} ticker={decision.ticker} qty={decision.qty:.0f} "
                        f"my_action={decision.my_action} hedge={decision.hedge_action}"
                    )
                else:
                    if ticks_left is not None and ticks_left > AUCTION_BID_TICKS_LEFT:
                        now = time.time()
                        if now - last_hold_log_at.get(tid, 0.0) >= LOG_HOLD_EVERY_SECS:
                            print(
                                f"HOLD auction {tid} ticker={decision.ticker} ticks_left={ticks_left} "
                                f"target_price={decision.submit_price:.2f}"
                            )
                            last_hold_log_at[tid] = now
                        continue

                    if decision.submit_price < MIN_AUCTION_PRICE:
                        client.decline_tender(tid)
                        processed_tenders.add(tid)
                        print(f"DECLINE auction {tid}: invalid submit_price={decision.submit_price:.2f}")
                        continue

                    client.accept_tender(tid, price=decision.submit_price)
                    processed_tenders.add(tid)
                    print(
                        f"BID auction tender {tid} ticker={decision.ticker} qty={decision.qty:.0f} "
                        f"price={decision.submit_price:.2f} my_action={decision.my_action}"
                    )

                max_order_qty = max_qty_by_ticker.get(decision.ticker, DEFAULT_MAX_ORDER_QTY)
                schedule_hedge(hedges, decision.ticker, decision.hedge_action, decision.qty, max_order_qty)
            except Exception as exc:
                print(f"TENDER ACTION ERROR {tid}: {exc}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
