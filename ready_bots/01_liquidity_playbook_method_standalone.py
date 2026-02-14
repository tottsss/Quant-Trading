"""Liquidity Risk bot based on the notebook playbook method.

Method implemented from `Liquidity_Risk_Case.ipynb`:
- Spread estimator: Roll-style estimate from TAS + top-book fallback.
- Price impact estimator: Kyle lambda approximation from TAS signed flow.
- Tender decision rule:
    accept if edge > kappa * d_star
    where d_star = commission + spread/2 + |lambda|*Q/2
    and kappa adapts to liquidity, volatility, open jobs, and time left.
- Execution engine:
    optimizer-inspired 3 phases with market orders (safe chunked hedging).

Run:
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  python .\\ready_bots\\01_liquidity_playbook_method_standalone.py
"""

from __future__ import annotations

import math
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
TAS_LIMIT = int(os.environ.get("RIT_TAS_LIMIT", "180"))

ORDER_MIN_SPACING_SECS = float(os.environ.get("RIT_ORDER_MIN_SPACING_SECS", "0.07"))
DEFAULT_MAX_ORDER_QTY = 10000.0
HARD_MAX_ORDER_QTY = 10000.0
MAX_ACTIVE_HEDGES = int(os.environ.get("RIT_MAX_ACTIVE_HEDGES", "40"))
MAX_PENDING_HEDGE_QTY = float(os.environ.get("RIT_MAX_PENDING_HEDGE_QTY", "120000"))

FIXED_ONLY_MODE = env_flag("RIT_FIXED_ONLY", True)
ENABLE_AUCTION_BIDS = env_flag("RIT_ENABLE_AUCTION", False)
AUCTION_ALPHA_START = float(os.environ.get("RIT_AUCTION_ALPHA_START", "0.40"))
AUCTION_ALPHA_MIN = 0.05
AUCTION_ALPHA_MAX = 1.50
AUCTION_ALPHA_STEP = 0.05
AUCTION_ALPHA_WIN_WINDOW = int(os.environ.get("RIT_AUCTION_ALPHA_WIN_WINDOW", "20"))
AUCTION_BID_TICKS_LEFT = int(os.environ.get("RIT_AUCTION_BID_TICKS_LEFT", "6"))

STOP_NEW_TENDERS_TICKS_LEFT = int(os.environ.get("RIT_STOP_NEW_TENDERS_TICKS_LEFT", "10"))
FORCE_FLATTEN_TICKS_LEFT = int(os.environ.get("RIT_FORCE_FLATTEN_TICKS_LEFT", "5"))

MIN_EXPECTED_GROSS_PNL = float(os.environ.get("RIT_MIN_EXPECTED_GROSS_PNL", "300"))
MIN_EXPECTED_PNL_PER_SHARE = float(os.environ.get("RIT_MIN_EXPECTED_PNL_PER_SHARE", "0.012"))
ROLL_WINDOW = int(os.environ.get("RIT_ROLL_WINDOW", "120"))
KYLE_BAR_TRADES = int(os.environ.get("RIT_KYLE_BAR_TRADES", "8"))
LAMBDA_FLOOR = 1e-8
HEDGE_ESTIMATE_QTY_CAP = float(os.environ.get("RIT_HEDGE_ESTIMATE_QTY_CAP", "30000"))
MIN_AUCTION_PRICE = 0.01

GROSS_USAGE_CAP = float(os.environ.get("RIT_GROSS_USAGE_CAP", "0.90"))
NET_USAGE_CAP = float(os.environ.get("RIT_NET_USAGE_CAP", "0.90"))
FALLBACK_GROSS_LIMIT = 250000.0
FALLBACK_NET_LIMIT = 150000.0

FINE_WATCH_EVERY_SECS = 5.0


# From notebook/case summary qualitative labels.
SEC_LABELS = {
    "RITC": ("Low", "Medium"), "COMP": ("Medium", "High"),
    "TRNT": ("High", "Medium"), "MTRL": ("Low", "Low"),
    "BLU": ("High", "High"), "RED": ("Low", "Medium"), "GRN": ("Medium", "Medium"),
    "WDY": ("Medium", "High"), "BZZ": ("High", "Medium"), "BNN": ("Medium", "Medium"),
    "VNS": ("High", "Medium"), "MRS": ("Medium", "High"), "JPTR": ("Low", "Medium"), "STRN": ("High", "Medium"),
}


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
class OrderThrottle:
    min_spacing: float
    next_allowed_at: float = 0.0

    def wait(self):
        now = time.time()
        if now < self.next_allowed_at:
            time.sleep(self.next_allowed_at - now)
            now = time.time()
        self.next_allowed_at = now + self.min_spacing


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
    d_star: float
    kappa: float
    expected_pnl: float
    spread_est: float
    kyle_lam: float


@dataclass
class HedgeJob:
    ticker: str
    action: str
    total_qty: float
    remaining: float
    base_slice: float
    max_order_qty: float
    created_at: float
    deadline_at: float
    next_time: float
    fail_streak: int = 0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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


def infer_limits(payload) -> tuple[float, float]:
    gross_vals = []
    net_vals = []
    if isinstance(payload, dict):
        payload = [payload]
    for row in payload or []:
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


def infer_ticker(tender: dict, valid_tickers: set[str]) -> str | None:
    tk = tender.get("ticker")
    if isinstance(tk, str) and tk in valid_tickers:
        return tk
    caption = str(tender.get("caption") or "")
    m = re.search(r"shares of\\s+([A-Z0-9_\\-]+)", caption, flags=re.IGNORECASE)
    if m:
        c = m.group(1).upper()
        if c in valid_tickers:
            return c
    for c in valid_tickers:
        if c in caption:
            return c
    return None


def infer_my_action(tender: dict) -> str:
    caption = str(tender.get("caption") or "").lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"
    a = str(tender.get("action") or "").upper()
    if a == "BUY":
        return "SELL"
    if a == "SELL":
        return "BUY"
    return "BUY"


def parse_levels(book: dict, side: str) -> list[tuple[float, float]]:
    out = []
    for lv in book.get(side, []):
        px = lv.get("price")
        q = lv.get("quantity", lv.get("qty"))
        if isinstance(px, (int, float)) and isinstance(q, (int, float)) and px > 0 and q > 0:
            out.append((float(px), float(q)))
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


def roll_spread(prices: list[float]) -> float:
    if len(prices) < 4:
        return 0.0
    dps = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mu = sum(dps) / len(dps)
    centered = [x - mu for x in dps]
    if len(centered) < 3:
        return 0.0
    ac = 0.0
    for i in range(1, len(centered)):
        ac += centered[i] * centered[i - 1]
    ac /= max(1, len(centered) - 1)
    if ac >= 0:
        return 0.0
    return 2.0 * math.sqrt(-ac)


def kyle_lambda_from_tas(tas_rows: list[dict], bar_trades: int = 8) -> float:
    prices = []
    qtys = []
    for r in tas_rows:
        p = r.get("price")
        q = r.get("quantity", r.get("qty"))
        if isinstance(p, (int, float)) and isinstance(q, (int, float)) and p > 0 and q > 0:
            prices.append(float(p))
            qtys.append(float(q))
    if len(prices) < max(20, bar_trades * 4):
        return LAMBDA_FLOOR

    # Approximate signed flow by trade-to-trade direction.
    signs = []
    last_sign = 1.0
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        if d > 0:
            s = 1.0
        elif d < 0:
            s = -1.0
        else:
            s = last_sign
        last_sign = s
        signs.append(s)

    signed_qty = [signs[i - 1] * qtys[i] for i in range(1, len(qtys))]
    trade_prices = prices[1:]
    n = len(signed_qty)
    if n < bar_trades * 3:
        return LAMBDA_FLOOR

    bars_x = []
    bars_mid_last = []
    i = 0
    while i < n:
        j = min(n, i + bar_trades)
        bars_x.append(sum(signed_qty[i:j]))
        bars_mid_last.append(trade_prices[j - 1])
        i = j

    if len(bars_mid_last) < 5:
        return LAMBDA_FLOOR

    y = [bars_mid_last[k] - bars_mid_last[k - 1] for k in range(1, len(bars_mid_last))]
    x = bars_x[1:]
    if len(x) < 4:
        return LAMBDA_FLOOR

    x_m = sum(x) / len(x)
    y_m = sum(y) / len(y)
    ss_xx = sum((v - x_m) ** 2 for v in x)
    if ss_xx <= 0:
        return LAMBDA_FLOOR
    cov = sum((x[k] - x_m) * (y[k] - y_m) for k in range(len(x)))
    lam = cov / ss_xx
    return max(abs(lam), LAMBDA_FLOOR)


def est_spread_from_tas_and_book(tas_rows: list[dict], book: dict) -> float:
    prices = []
    for r in tas_rows:
        p = r.get("price")
        if isinstance(p, (int, float)) and p > 0:
            prices.append(float(p))
    if len(prices) > ROLL_WINDOW:
        prices = prices[-ROLL_WINDOW:]
    s_roll = roll_spread(prices) if prices else 0.0
    bid, ask = best_bid_ask(book)
    s_top = (ask - bid) if (bid is not None and ask is not None and ask >= bid) else 0.0
    if s_roll > 0 and s_top > 0:
        return 0.7 * s_roll + 0.3 * s_top
    return max(s_roll, s_top, 0.01)


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


def adaptive_kappa(liq_label: str, vol_label: str, n_open: int, time_left: float) -> float:
    k = {"High": 1.3, "Medium": 1.5, "Low": 2.0}.get(liq_label, 1.6)
    if vol_label == "High":
        k += 0.1
    if n_open > 0:
        k += 0.2
    if time_left < 180:
        k += 0.3
    elif time_left < 300:
        k += 0.1
    return k


def expected_pnl_abs_edge(edge: float, d_star: float, qty: float) -> float:
    return (edge - d_star) * qty


def infer_max_order_qty(sec: dict) -> float:
    for key in ("max_trade_size", "max_order_size", "max_trade_qty", "max_order_qty"):
        v = sec.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return min(float(v), HARD_MAX_ORDER_QTY)
    return min(DEFAULT_MAX_ORDER_QTY, HARD_MAX_ORDER_QTY)


def tender_fill_confirmed(resp) -> bool:
    if not isinstance(resp, dict):
        return True
    status = str(resp.get("status") or "").upper()
    if any(x in status for x in ("TRADING_LIMIT", "REJECT", "DECLIN", "ERROR", "CANCEL")):
        return False
    if status and not any(ok in status for ok in ("ACCEPT", "WON", "FILL", "SUCCESS", "COMPLETE")):
        return False
    return True


def unresolved_tender_tickers(tenders: list[dict], processed: set[int], valid_tickers: set[str]) -> set[str]:
    out = set()
    for t in tenders:
        tid = t.get("tender_id")
        if tid in processed:
            continue
        tk = infer_ticker(t, valid_tickers)
        if tk:
            out.add(tk)
    return out


def evaluate_tender_playbook(
    tender: dict,
    positions: dict[str, float],
    sec_by_ticker: dict[str, dict],
    valid_tickers: set[str],
    tas_by_ticker: dict[str, list[dict]],
    book_by_ticker: dict[str, dict],
    open_jobs_by_ticker: dict[str, int],
    gross_limit: float,
    net_limit: float,
) -> tuple[TenderPlan | None, str]:
    tid = int(tender.get("tender_id") or -1)
    ticker = infer_ticker(tender, valid_tickers)
    if not ticker:
        return None, "ticker unresolved"

    qty_raw = tender.get("quantity")
    qty = abs(float(qty_raw)) if isinstance(qty_raw, (int, float)) else 0.0
    if qty <= 0:
        return None, "invalid qty"

    my_action = infer_my_action(tender)
    hedge_action = "BUY" if my_action == "SELL" else "SELL"

    is_fixed = bool(tender.get("is_fixed_bid"))
    if not is_fixed and (FIXED_ONLY_MODE or not ENABLE_AUCTION_BIDS):
        return None, "auction disabled"

    ok_risk, rr = projected_risk_ok(positions, ticker, my_action, qty, gross_limit, net_limit)
    if not ok_risk:
        return None, rr

    book = book_by_ticker.get(ticker)
    if not isinstance(book, dict):
        return None, "book unavailable"

    tas = tas_by_ticker.get(ticker, [])
    spread_est = est_spread_from_tas_and_book(tas, book)
    kyle_lam = kyle_lambda_from_tas(tas, bar_trades=KYLE_BAR_TRADES)

    bid, ask = best_bid_ask(book)
    if bid is None or ask is None or ask < bid:
        return None, "invalid top book"
    mid = (bid + ask) / 2.0

    tender_price = tender.get("price")
    if is_fixed and not isinstance(tender_price, (int, float)):
        return None, "fixed missing price"

    label_vol, label_liq = SEC_LABELS.get(ticker, ("Medium", "Medium"))
    expires = tender.get("expires")
    t_left = 300.0
    if isinstance(expires, (int, float)):
        # use tender-level ticks left when available
        t_left = max(0.0, float(expires))
    kappa = adaptive_kappa(label_liq, label_vol, open_jobs_by_ticker.get(ticker, 0), t_left)

    sec = sec_by_ticker.get(ticker, {})
    commission = float(sec.get("trading_fee") or sec.get("fee") or 0.0)
    d_star = commission + spread_est / 2.0 + abs(kyle_lam) * qty / 2.0

    edge = abs(float(tender_price) - mid) if isinstance(tender_price, (int, float)) else 0.0
    expected = expected_pnl_abs_edge(edge, d_star, qty)
    pps = expected / max(1.0, qty)

    fixed_accept = edge > (kappa * d_star) and expected >= MIN_EXPECTED_GROSS_PNL and pps >= MIN_EXPECTED_PNL_PER_SHARE

    # Auction price based on notebook formula around break-even + alpha margin.
    # We quote in direction of our tender action.
    alpha = AUCTION_ALPHA_START
    if my_action == "SELL":
        submit = mid + d_star * (1.0 + alpha)
    else:
        submit = mid - d_star * (1.0 + alpha)
    submit = max(MIN_AUCTION_PRICE, round(submit, 2))

    return TenderPlan(
        tender_id=tid,
        ticker=ticker,
        qty=qty,
        my_action=my_action,
        hedge_action=hedge_action,
        is_fixed=is_fixed,
        fixed_accept=fixed_accept,
        submit_price=submit,
        edge=edge,
        d_star=d_star,
        kappa=kappa,
        expected_pnl=expected,
        spread_est=spread_est,
        kyle_lam=kyle_lam,
    ), "ok"


def schedule_hedge(hedges: list[HedgeJob], ticker: str, action: str, qty: float, max_order_qty: float):
    base_slice = max(500.0, qty / max(1, 10))
    now = time.time()
    hedges.append(
        HedgeJob(
            ticker=ticker,
            action=action,
            total_qty=qty,
            remaining=qty,
            base_slice=base_slice,
            max_order_qty=max(1.0, max_order_qty),
            created_at=now,
            deadline_at=now + 60.0,
            next_time=now,
        )
    )


def process_hedges(
    client: RITClient,
    hedges: list[HedgeJob],
    throttle: OrderThrottle,
    blocked_tickers: set[str] | None = None,
) -> list[HedgeJob]:
    blocked_tickers = blocked_tickers or set()
    out = []
    now = time.time()

    for h in hedges:
        if h.remaining <= 0:
            continue
        if h.ticker in blocked_tickers:
            h.next_time = now + max(POLL_SECS, 0.25)
            out.append(h)
            continue
        if now < h.next_time:
            out.append(h)
            continue

        frac_done = 1.0 - (h.remaining / max(1.0, h.total_qty))
        total_dur = max(1.0, h.deadline_at - h.created_at)
        time_left = max(0.0, h.deadline_at - now)

        if frac_done >= 0.85 or time_left <= total_dur * 0.15:
            # Phase 3 aggressive
            q = min(h.remaining, h.max_order_qty)
            next_gap = 0.15
        elif frac_done >= 0.60 or time_left <= total_dur * 0.40:
            # Phase 2 normal
            q = min(h.remaining, h.max_order_qty, max(1000.0, h.base_slice))
            next_gap = 0.30
        else:
            # Phase 1 gentle
            q = min(h.remaining, h.max_order_qty, max(500.0, h.base_slice * 0.7))
            next_gap = 0.45

        q = max(1.0, q)
        try:
            throttle.wait()
            client.post("/orders", {"ticker": h.ticker, "type": "MARKET", "action": h.action, "quantity": float(q)})
            print(f"HEDGE {h.action} {h.ticker} qty={q:.0f} rem_before={h.remaining:.0f}")
            h.remaining -= q
            h.fail_streak = 0
            h.next_time = now + next_gap
            if h.remaining > 0:
                out.append(h)
        except Exception as exc:
            h.fail_streak += 1
            h.max_order_qty = max(250.0, min(h.max_order_qty, q / 2.0))
            h.next_time = now + min(1.5, 0.30 * (1 + h.fail_streak))
            print(
                f"HEDGE ERROR {h.ticker} action={h.action} qty={q:.0f} "
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
            print(f"FLATTEN ERROR {tk} {action} qty={qty:.0f}: {exc}")


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


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY)
    processed_tenders: set[int] = set()
    hedges: list[HedgeJob] = []
    throttle = OrderThrottle(min_spacing=ORDER_MIN_SPACING_SECS)
    auction_alpha = AUCTION_ALPHA_START
    auction_recent_wins: list[bool] = []
    last_fine_watch = 0.0

    print(
        f"Connected to {BASE_URL} | playbook method | fixed_only={FIXED_ONLY_MODE} "
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
            gross_limit, net_limit = infer_limits(client.get("/limits"))
        except Exception:
            gross_limit, net_limit = FALLBACK_GROSS_LIMIT, FALLBACK_NET_LIMIT

        now = time.time()
        if now - last_fine_watch >= FINE_WATCH_EVERY_SECS:
            last_fine_watch = now
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

        # Endgame mode: decline open tenders and flatten positions.
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

        # Fetch data snapshots once per loop.
        tas_by_ticker: dict[str, list[dict]] = {}
        book_by_ticker: dict[str, dict] = {}
        for tk in valid_tickers:
            try:
                tas_by_ticker[tk] = client.get("/securities/tas", {"ticker": tk, "limit": TAS_LIMIT})
            except Exception:
                tas_by_ticker[tk] = []
            try:
                book_by_ticker[tk] = client.get("/securities/book", {"ticker": tk, "limit": BOOK_LEVELS})
            except Exception:
                pass

        open_jobs_by_ticker = {}
        for h in hedges:
            if h.remaining <= 0:
                continue
            open_jobs_by_ticker[h.ticker] = open_jobs_by_ticker.get(h.ticker, 0) + 1

        for tender in tenders:
            tid = tender.get("tender_id")
            if tid in processed_tenders:
                continue

            if case_ticks_left is not None and case_ticks_left <= STOP_NEW_TENDERS_TICKS_LEFT:
                try:
                    client.delete(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                    print(f"DECLINE tender {tid}: endgame risk-off")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                continue

            pending_qty = sum(h.remaining for h in hedges if h.remaining > 0)
            if pending_qty > MAX_PENDING_HEDGE_QTY:
                print(f"HOLD tender {tid}: pending hedge qty too high ({pending_qty:.0f})")
                continue
            if len([h for h in hedges if h.remaining > 0]) >= MAX_ACTIVE_HEDGES:
                print(f"HOLD tender {tid}: hedge queue full")
                continue

            plan, reason = evaluate_tender_playbook(
                tender,
                positions,
                sec_by_ticker,
                valid_tickers,
                tas_by_ticker,
                book_by_ticker,
                open_jobs_by_ticker,
                gross_limit,
                net_limit,
            )
            if plan is None:
                try:
                    client.delete(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                    print(f"DECLINE tender {tid}: {reason}")
                except Exception as exc:
                    print(f"DECLINE ERROR {tid}: {exc}")
                continue

            expires = tender.get("expires")
            ticks_left = None
            if isinstance(expires, (int, float)):
                ticks_left = int(expires) - current_tick

            try:
                if plan.is_fixed:
                    if not plan.fixed_accept:
                        client.delete(f"/tenders/{tid}")
                        processed_tenders.add(tid)
                        print(
                            f"DECLINE fixed {tid} {plan.ticker} qty={plan.qty:.0f} "
                            f"edge={plan.edge:.4f} d*={plan.d_star:.4f} kappa={plan.kappa:.2f}"
                        )
                        continue

                    resp = client.post(f"/tenders/{tid}")
                    processed_tenders.add(tid)
                    won = tender_fill_confirmed(resp)
                    if not won:
                        print(f"SKIP HEDGE fixed {tid}: not filled ({resp})")
                        continue
                    print(
                        f"ACCEPT fixed {tid} {plan.ticker} qty={plan.qty:.0f} "
                        f"edge={plan.edge:.4f} d*={plan.d_star:.4f} k={plan.kappa:.2f} "
                        f"lam={plan.kyle_lam:.8f} spr={plan.spread_est:.4f} epnl={plan.expected_pnl:.2f}"
                    )
                else:
                    if ticks_left is not None and ticks_left > AUCTION_BID_TICKS_LEFT:
                        # keep automation simple and safe: skip early auction windows.
                        client.delete(f"/tenders/{tid}")
                        processed_tenders.add(tid)
                        print(f"DECLINE auction {tid}: too early (ticks_left={ticks_left})")
                        continue

                    d_star = max(1e-6, plan.d_star)
                    if plan.my_action == "SELL":
                        submit = plan.submit_price + auction_alpha * d_star
                    else:
                        submit = max(MIN_AUCTION_PRICE, plan.submit_price - auction_alpha * d_star)
                    submit = max(MIN_AUCTION_PRICE, round(submit, 2))

                    resp = client.post(f"/tenders/{tid}", {"price": submit})
                    processed_tenders.add(tid)
                    won = tender_fill_confirmed(resp)
                    auction_recent_wins.append(won)
                    if len(auction_recent_wins) > AUCTION_ALPHA_WIN_WINDOW:
                        auction_recent_wins.pop(0)
                    wr = sum(1 for x in auction_recent_wins if x) / max(1, len(auction_recent_wins))
                    if wr > 0.70:
                        auction_alpha = min(AUCTION_ALPHA_MAX, auction_alpha + AUCTION_ALPHA_STEP)
                    elif wr < 0.30:
                        auction_alpha = max(AUCTION_ALPHA_MIN, auction_alpha - AUCTION_ALPHA_STEP)

                    if not won:
                        print(f"SKIP HEDGE auction {tid}: not filled ({resp})")
                        continue
                    print(
                        f"BID auction {tid} {plan.ticker} qty={plan.qty:.0f} submit={submit:.2f} "
                        f"alpha={auction_alpha:.2f}"
                    )

                schedule_hedge(
                    hedges,
                    plan.ticker,
                    plan.hedge_action,
                    plan.qty,
                    max_qty_by_ticker.get(plan.ticker, DEFAULT_MAX_ORDER_QTY),
                )
            except Exception as exc:
                print(f"TENDER ACTION ERROR {tid}: {exc}")

        blocked = unresolved_tender_tickers(tenders, processed_tenders, valid_tickers)
        hedges = process_hedges(client, hedges, throttle, blocked_tickers=blocked)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
