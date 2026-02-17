"""Liquidity Risk bot using WAC/depth routing across related markets.

What it does
- Evaluates tenders with depth-based executable hedge price (WAC).
- Aggregates liquidity across related tickers: BASE, BASE_M, BASE_A.
- Accepts/declines tenders automatically, then hedges in routed market chunks.
- Uses Client REST API (RIT simulator app): http://localhost:9999/v1

Run (PowerShell)
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  python ./ready_bots/liquidity_lorenzourera_tenders.py
"""

from __future__ import annotations

import os
import re
import signal
import time

import requests


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


API_KEY = os.environ.get("RIT_API_KEY") or os.environ.get("API_KEY", "BNWI101Y")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")

POLL_SECS = float(os.environ.get("RIT_POLL_SECS", "0.60"))
BOOK_LEVELS = int(os.environ.get("RIT_BOOK_LEVELS", "50"))
LIQUIDITY_BUFFER = float(os.environ.get("RIT_LIQUIDITY_BUFFER", "0.10"))
MIN_GROSS_PNL = float(os.environ.get("RIT_MIN_GROSS_PNL", "250"))
MIN_PNL_PER_SHARE = float(os.environ.get("RIT_MIN_PNL_PER_SHARE", "0.010"))
ORDER_MIN_SPACING_SECS = float(os.environ.get("RIT_ORDER_MIN_SPACING_SECS", "0.08"))
MAX_ORDER_QTY = float(os.environ.get("RIT_MAX_ORDER_QTY", "10000"))
FIXED_ONLY_MODE = env_flag("RIT_FIXED_ONLY", True)
STOP_NEW_TENDERS_TICKS_LEFT = int(os.environ.get("RIT_STOP_NEW_TENDERS_TICKS_LEFT", "8"))
FORCE_FLATTEN_TICKS_LEFT = int(os.environ.get("RIT_FORCE_FLATTEN_TICKS_LEFT", "4"))
HEDGE_POS_CONFIRM_RETRIES = int(os.environ.get("RIT_HEDGE_POS_CONFIRM_RETRIES", "5"))
HEDGE_POS_CONFIRM_SLEEP_SECS = float(os.environ.get("RIT_HEDGE_POS_CONFIRM_SLEEP_SECS", "0.12"))
MIN_DELTA_TO_HEDGE = float(os.environ.get("RIT_MIN_DELTA_TO_HEDGE", "1"))
TENDER_RESOLVE_RETRIES = int(os.environ.get("RIT_TENDER_RESOLVE_RETRIES", "8"))
TENDER_RESOLVE_SLEEP_SECS = float(os.environ.get("RIT_TENDER_RESOLVE_SLEEP_SECS", "0.12"))
FORCE_HEDGE_FROM_FILL_QTY = env_flag("RIT_FORCE_HEDGE_FROM_FILL_QTY", True)
GROSS_USAGE_CAP = float(os.environ.get("RIT_GROSS_USAGE_CAP", "0.90"))
NET_USAGE_CAP = float(os.environ.get("RIT_NET_USAGE_CAP", "0.90"))
CLEAR_SCREEN = env_flag("RIT_CLEAR_SCREEN", False)

FALLBACK_GROSS_LIMIT = 250000.0
FALLBACK_NET_LIMIT = 150000.0

shutdown = False


def signal_handler(signum, frame):
    del signum, frame
    global shutdown
    shutdown = True


signal.signal(signal.SIGINT, signal_handler)


class ApiException(Exception):
    pass


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

    def get_case(self):
        return self.get("/case")

    def get_limits(self):
        return self.get("/limits")

    def get_tenders(self):
        return self.get("/tenders")

    def get_securities(self):
        return self.get("/securities")

    def get_book(self, ticker: str, limit: int):
        return self.get("/securities/book", params={"ticker": ticker, "limit": limit})

    def accept_tender(self, tender_id: int, price: float | None = None):
        params = {} if price is None else {"price": price}
        return self.post(f"/tenders/{tender_id}", params=params)

    def decline_tender(self, tender_id: int):
        return self.delete(f"/tenders/{tender_id}")

    def place_market_order(self, ticker: str, quantity: float, action: str):
        return self.post(
            "/orders",
            params={
                "ticker": ticker,
                "type": "MARKET",
                "quantity": float(quantity),
                "action": action,
            },
        )


def clear_screen():
    if not CLEAR_SCREEN:
        return
    os.system("cls" if os.name == "nt" else "clear")


def extract_base_ticker(ticker: str) -> str:
    if ticker.endswith("_M") or ticker.endswith("_A"):
        return ticker[:-2]
    return ticker


def infer_case_ticks_left(case: dict) -> int | None:
    tick = case.get("tick")
    if not isinstance(tick, (int, float)):
        return None
    tick = int(tick)
    for key in ("ticks_per_period", "period_ticks", "total_ticks", "max_ticks"):
        v = case.get(key)
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
    caption = (tender.get("caption") or "").lower()
    if "would you like to sell" in caption:
        return "SELL"
    if "would you like to buy" in caption:
        return "BUY"

    action = (tender.get("action") or "").upper()
    if action == "BUY":
        return "SELL"
    if action == "SELL":
        return "BUY"
    return "BUY"


def related_tickers(tender_ticker: str, valid_tickers: set[str]) -> list[str]:
    base = extract_base_ticker(tender_ticker)
    candidates = [tender_ticker, base, f"{base}_M", f"{base}_A"]
    out: list[str] = []
    for tk in candidates:
        if tk in valid_tickers and tk not in out:
            out.append(tk)
    return out or [tender_ticker]


def parse_book_levels(book: dict, ticker: str, side: str) -> list[dict]:
    out: list[dict] = []
    rows = book.get(side, [])
    for row in rows:
        px = row.get("price")
        qty = row.get("quantity", row.get("qty"))
        filled = row.get("quantity_filled", row.get("qty_filled", 0))
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        if not isinstance(qty, (int, float)) or qty <= 0:
            continue
        if not isinstance(filled, (int, float)):
            filled = 0
        avail = max(0.0, float(qty) - float(filled))
        if avail <= 0:
            continue
        out.append({"ticker": ticker, "price": float(px), "avail": avail})
    return out


def merged_levels_for_related(client: RITClient, tickers: list[str]) -> tuple[list[dict], list[dict]]:
    bids: list[dict] = []
    asks: list[dict] = []
    for tk in tickers:
        try:
            book = client.get_book(tk, BOOK_LEVELS)
        except Exception:
            continue
        bids.extend(parse_book_levels(book, tk, "bids"))
        asks.extend(parse_book_levels(book, tk, "asks"))
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return bids, asks


def route_wac(levels: list[dict], needed_qty: float) -> tuple[float, float, dict[str, float]]:
    if needed_qty <= 0:
        return 0.0, 0.0, {}
    total_qty = 0.0
    total_notional = 0.0
    by_ticker: dict[str, float] = {}
    for lv in levels:
        if total_qty >= needed_qty:
            break
        take = min(lv["avail"], needed_qty - total_qty)
        if take <= 0:
            continue
        total_qty += take
        total_notional += take * lv["price"]
        by_ticker[lv["ticker"]] = by_ticker.get(lv["ticker"], 0.0) + take
    wac = (total_notional / total_qty) if total_qty > 0 else 0.0
    return total_qty, wac, by_ticker


def infer_fee_per_share(sec: dict) -> float:
    if not sec:
        return 0.0
    for key in ("trading_fee", "fee", "commission", "commission_per_share"):
        v = sec.get(key)
        if isinstance(v, (int, float)):
            return max(0.0, float(v))
    return 0.0


def expected_pnl(my_action: str, tender_px: float, hedge_wac: float, qty: float, fee_per_share: float) -> float:
    if my_action == "SELL":
        gross = (tender_px - hedge_wac) * qty
    else:
        gross = (hedge_wac - tender_px) * qty
    return gross - fee_per_share * qty


def infer_limits(limits_payload) -> tuple[float, float]:
    gross_limit = None
    net_limit = None

    if isinstance(limits_payload, dict):
        if isinstance(limits_payload.get("gross_limit"), (int, float)):
            gross_limit = float(limits_payload["gross_limit"])
        if isinstance(limits_payload.get("net_limit"), (int, float)):
            net_limit = float(limits_payload["net_limit"])

    if isinstance(limits_payload, list):
        gross_values: list[float] = []
        net_values: list[float] = []
        for row in limits_payload:
            if not isinstance(row, dict):
                continue
            g = row.get("gross_limit")
            n = row.get("net_limit")
            if isinstance(g, (int, float)):
                gross_values.append(float(g))
            if isinstance(n, (int, float)):
                net_values.append(float(n))
        if gross_values:
            gross_limit = min(gross_values)
        if net_values:
            net_limit = min(net_values)

    if gross_limit is None:
        gross_limit = FALLBACK_GROSS_LIMIT
    if net_limit is None:
        net_limit = FALLBACK_NET_LIMIT
    return gross_limit, net_limit


def projected_risk_ok(
    positions: dict[str, float],
    ticker: str,
    my_action: str,
    qty: float,
    gross_limit: float,
    net_limit: float,
) -> tuple[bool, str]:
    old_pos = positions.get(ticker, 0.0)
    delta = qty if my_action == "BUY" else -qty
    new_pos = old_pos + delta
    current_gross = sum(abs(v) for v in positions.values())
    projected_gross = current_gross - abs(old_pos) + abs(new_pos)
    current_net = sum(positions.values())
    projected_net = current_net + delta

    if projected_gross > GROSS_USAGE_CAP * gross_limit:
        return False, f"gross_usage>{GROSS_USAGE_CAP:.2f}"
    if abs(projected_net) > NET_USAGE_CAP * net_limit:
        return False, f"net_usage>{NET_USAGE_CAP:.2f}"
    return True, "ok"


def tender_fill_confirmed(resp: dict) -> bool:
    status = str(resp.get("status") or "").upper()
    if any(word in status for word in ("REJECT", "DECLIN", "TRADING_LIMIT", "ERROR", "CANCEL")):
        return False
    if not status:
        return False
    return any(word in status for word in ("ACCEPT", "WON", "FILL", "SUCCESS", "COMPLETE"))


def place_market_chunks(client: RITClient, ticker: str, qty: float, action: str):
    remaining = float(qty)
    while remaining > 0:
        chunk = min(remaining, MAX_ORDER_QTY)
        client.place_market_order(ticker, chunk, action)
        remaining -= chunk
        time.sleep(ORDER_MIN_SPACING_SECS)


def hedge_route(client: RITClient, hedge_action: str, by_ticker_qty: dict[str, float]):
    for tk, q in sorted(by_ticker_qty.items()):
        if q <= 0:
            continue
        place_market_chunks(client, tk, q, hedge_action)


def sum_positions_for_tickers(positions: dict[str, float], tickers: list[str]) -> float:
    total = 0.0
    for tk in tickers:
        total += float(positions.get(tk, 0.0))
    return total


def fetch_related_position_delta(
    client: RITClient,
    related: list[str],
    pre_related_pos: float,
) -> tuple[float, dict[str, float]]:
    last_positions: dict[str, float] = {}
    delta = 0.0

    for _ in range(max(1, HEDGE_POS_CONFIRM_RETRIES)):
        try:
            secs_now = client.get_securities()
            last_positions = {
                s["ticker"]: float(s.get("position") or 0.0)
                for s in secs_now
                if s.get("ticker")
            }
            post_related_pos = sum_positions_for_tickers(last_positions, related)
            delta = post_related_pos - pre_related_pos
            if abs(delta) >= MIN_DELTA_TO_HEDGE:
                return delta, last_positions
        except Exception:
            pass
        time.sleep(HEDGE_POS_CONFIRM_SLEEP_SECS)

    return delta, last_positions


def extract_filled_qty(resp: dict) -> float | None:
    if not isinstance(resp, dict):
        return None
    for key in ("quantity_filled", "filled_qty", "filled_quantity", "quantity", "qty"):
        v = resp.get(key)
        if isinstance(v, (int, float)):
            return max(0.0, float(v))
    return None


def find_tender_by_id(tenders: list[dict], tender_id: int) -> dict | None:
    for t in tenders:
        if t.get("tender_id") == tender_id:
            return t
    return None


def wait_tender_not_open(client: RITClient, tender_id: int) -> tuple[bool, dict | None]:
    last_seen: dict | None = None
    for _ in range(max(1, TENDER_RESOLVE_RETRIES)):
        try:
            tenders = client.get_tenders()
            t = find_tender_by_id(tenders, tender_id)
            last_seen = t
            if t is None:
                return True, None
            if not is_open_tender(t):
                return True, t
        except Exception:
            pass
        time.sleep(TENDER_RESOLVE_SLEEP_SECS)
    return False, last_seen


def decline_open_tenders_same_base(
    client: RITClient,
    tenders: list[dict],
    base_ticker: str,
    keep_tender_id: int,
) -> set[int]:
    declined: set[int] = set()
    for t in tenders:
        tid = t.get("tender_id")
        if not isinstance(tid, int) or tid == keep_tender_id:
            continue
        tk = t.get("ticker")
        if not isinstance(tk, str):
            continue
        if extract_base_ticker(tk) != base_ticker:
            continue
        if not is_open_tender(t):
            continue
        try:
            client.decline_tender(tid)
            declined.add(tid)
            print(f"[DECLINE] id={tid} {tk} reason=clear_same_base_before_hedge")
        except Exception as e:
            print(f"[WARN] decline failed id={tid}: {e}")
    return declined


def flatten_all_positions(client: RITClient, securities: list[dict]):
    for sec in securities:
        ticker = sec.get("ticker")
        if not ticker:
            continue
        pos = float(sec.get("position") or 0.0)
        if abs(pos) < 1:
            continue
        action = "SELL" if pos > 0 else "BUY"
        qty = abs(pos)
        try:
            place_market_chunks(client, ticker, qty, action)
            print(f"[FLATTEN] {action} {int(qty)} {ticker}")
        except Exception as e:
            print(f"[WARN] flatten failed for {ticker}: {e}")


def is_open_tender(t: dict) -> bool:
    status = str(t.get("status") or "").upper()
    if not status:
        return True
    return status in {"OPEN", "OFFERED", "ACTIVE", "PENDING"}


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    print(f"Connected config: BASE_URL={BASE_URL}")
    print("Running Liquidity WAC/depth router...")

    client = RITClient(API_KEY)
    processed_tender_ids: set[int] = set()

    while not shutdown:
        try:
            case = client.get_case()
        except Exception as e:
            print(f"[WARN] /case failed: {e}")
            time.sleep(1.0)
            continue

        status = str(case.get("status") or "")
        tick = case.get("tick")
        ticks_left = infer_case_ticks_left(case)

        if status != "ACTIVE":
            time.sleep(0.8)
            continue

        clear_screen()
        print(f"tick={tick} ticks_left={ticks_left}")

        try:
            securities = client.get_securities()
            sec_by_ticker = {s["ticker"]: s for s in securities if s.get("ticker")}
            valid_tickers = set(sec_by_ticker.keys())
            positions = {s["ticker"]: float(s.get("position") or 0.0) for s in securities if s.get("ticker")}
        except Exception as e:
            print(f"[WARN] /securities failed: {e}")
            time.sleep(POLL_SECS)
            continue

        try:
            limits_payload = client.get_limits()
        except Exception:
            limits_payload = []
        gross_limit, net_limit = infer_limits(limits_payload)

        try:
            tenders = client.get_tenders()
        except Exception as e:
            print(f"[WARN] /tenders failed: {e}")
            time.sleep(POLL_SECS)
            continue

        if ticks_left is not None and ticks_left <= FORCE_FLATTEN_TICKS_LEFT:
            for t in tenders:
                tid = t.get("tender_id")
                if tid in processed_tender_ids:
                    continue
                try:
                    client.decline_tender(int(tid))
                except Exception:
                    pass
                processed_tender_ids.add(int(tid))
            flatten_all_positions(client, securities)
            time.sleep(POLL_SECS)
            continue

        for t in tenders:
            tid = t.get("tender_id")
            if not isinstance(tid, int):
                continue
            if tid in processed_tender_ids:
                continue
            if not is_open_tender(t):
                processed_tender_ids.add(tid)
                continue

            if ticks_left is not None and ticks_left <= STOP_NEW_TENDERS_TICKS_LEFT:
                try:
                    client.decline_tender(tid)
                    print(f"[DECLINE] id={tid} reason=endgame")
                except Exception as e:
                    print(f"[WARN] decline failed id={tid}: {e}")
                processed_tender_ids.add(tid)
                continue

            ticker = infer_ticker(t, valid_tickers)
            if not ticker:
                processed_tender_ids.add(tid)
                continue

            tender_px = t.get("price")
            qty = float(t.get("quantity") or 0.0)
            if not isinstance(tender_px, (int, float)) or qty <= 0:
                try:
                    client.decline_tender(tid)
                except Exception:
                    pass
                processed_tender_ids.add(tid)
                continue
            tender_px = float(tender_px)

            is_fixed = bool(t.get("is_fixed_bid"))
            if FIXED_ONLY_MODE and not is_fixed:
                try:
                    client.decline_tender(tid)
                    print(f"[DECLINE] id={tid} {ticker} reason=auction_disabled")
                except Exception as e:
                    print(f"[WARN] decline failed id={tid}: {e}")
                processed_tender_ids.add(tid)
                continue

            my_action = infer_my_action(t)
            hedge_action = "BUY" if my_action == "SELL" else "SELL"
            rel = related_tickers(ticker, valid_tickers)
            pre_related_pos = sum_positions_for_tickers(positions, rel)
            bids, asks = merged_levels_for_related(client, rel)
            levels = asks if hedge_action == "BUY" else bids

            need_buffered = qty * (1.0 + LIQUIDITY_BUFFER)
            available_buffered, wac_buffered, _ = route_wac(levels, need_buffered)
            available_exact, wac_exact, route_exact = route_wac(levels, qty)
            if available_exact < qty:
                try:
                    client.decline_tender(tid)
                    print(
                        f"[DECLINE] id={tid} {ticker} reason=insufficient_liquidity "
                        f"avail={int(available_exact)}/{int(qty)}"
                    )
                except Exception as e:
                    print(f"[WARN] decline failed id={tid}: {e}")
                processed_tender_ids.add(tid)
                continue

            fee = infer_fee_per_share(sec_by_ticker.get(ticker, {}))
            pnl = expected_pnl(my_action, tender_px, wac_exact, qty, fee)
            pps = pnl / qty if qty > 0 else 0.0
            risk_ok, risk_reason = projected_risk_ok(
                positions=positions,
                ticker=ticker,
                my_action=my_action,
                qty=qty,
                gross_limit=gross_limit,
                net_limit=net_limit,
            )

            print("------------- Tender -------------")
            print(
                f"id={tid} ticker={ticker} my_action={my_action} "
                f"qty={int(qty)} px={tender_px:.2f} fixed={is_fixed}"
            )
            print(
                f"wac={wac_exact:.4f} wac_buffered={wac_buffered:.4f} "
                f"avail_buffered={int(available_buffered)}/{int(need_buffered)}"
            )
            print(f"exp_pnl={pnl:.2f} pps={pps:.4f} fee={fee:.4f} risk={risk_reason}")
            print(f"route={ {k: int(v) for k, v in route_exact.items()} }")

            should_accept = (
                available_buffered >= need_buffered
                and pnl >= MIN_GROSS_PNL
                and pps >= MIN_PNL_PER_SHARE
                and risk_ok
            )

            if not should_accept:
                try:
                    client.decline_tender(tid)
                    print(f"[DECLINE] id={tid} {ticker}")
                except Exception as e:
                    print(f"[WARN] decline failed id={tid}: {e}")
                processed_tender_ids.add(tid)
                continue

            submit_price = None
            if not is_fixed:
                if my_action == "SELL":
                    submit_price = round(wac_exact + fee + MIN_PNL_PER_SHARE, 2)
                else:
                    submit_price = round(max(0.01, wac_exact - fee - MIN_PNL_PER_SHARE), 2)

            try:
                resp = client.accept_tender(tid, price=submit_price)
            except Exception as e:
                print(f"[WARN] accept failed id={tid}: {e}")
                processed_tender_ids.add(tid)
                continue

            if not tender_fill_confirmed(resp):
                print(f"[INFO] accept response not filled id={tid}; skip hedge")
                processed_tender_ids.add(tid)
                continue

            resolved, t_after = wait_tender_not_open(client, tid)
            if not resolved:
                print(f"[INFO] tender id={tid} still open after accept; skip hedge")
                processed_tender_ids.add(tid)
                continue

            try:
                fresh_tenders = client.get_tenders()
            except Exception:
                fresh_tenders = []
            base_ticker = extract_base_ticker(ticker)
            dropped = decline_open_tenders_same_base(
                client=client,
                tenders=fresh_tenders,
                base_ticker=base_ticker,
                keep_tender_id=tid,
            )
            processed_tender_ids.update(dropped)

            delta, latest_positions = fetch_related_position_delta(client, rel, pre_related_pos)
            if latest_positions:
                positions.update(latest_positions)

            if abs(delta) < MIN_DELTA_TO_HEDGE:
                fallback_qty = None
                if FORCE_HEDGE_FROM_FILL_QTY:
                    fallback_qty = extract_filled_qty(resp)
                    if fallback_qty is None and isinstance(t_after, dict):
                        fallback_qty = extract_filled_qty(t_after)
                if fallback_qty is None or fallback_qty < MIN_DELTA_TO_HEDGE:
                    print(
                        f"[INFO] id={tid} accepted but no detectable position delta "
                        f"(delta={delta:.2f}); skip hedge"
                    )
                    processed_tender_ids.add(tid)
                    continue
                delta = fallback_qty if my_action == "BUY" else -fallback_qty
                print(
                    f"[INFO] id={tid} using fallback hedge qty from fill fields: "
                    f"qty={fallback_qty:.2f} delta={delta:.2f}"
                )

            actual_hedge_action = "SELL" if delta > 0 else "BUY"
            actual_hedge_qty = abs(delta)
            bids_now, asks_now = merged_levels_for_related(client, rel)
            levels_now = asks_now if actual_hedge_action == "BUY" else bids_now
            available_now, wac_now, route_now = route_wac(levels_now, actual_hedge_qty)

            print(
                f"[ACCEPT] id={tid} {ticker} delta={delta:.2f} "
                f"hedge={actual_hedge_action} qty={actual_hedge_qty:.2f} "
                f"wac={wac_now:.4f}"
            )

            try:
                if available_now > 0:
                    hedge_route(client, actual_hedge_action, route_now)
                remaining = max(0.0, actual_hedge_qty - available_now)
                if remaining > 0:
                    # Safety fallback to avoid leaving inventory risk unhedged.
                    place_market_chunks(client, ticker, remaining, actual_hedge_action)
                    print(
                        f"[HEDGE-FALLBACK] {actual_hedge_action} {remaining:.0f} {ticker} "
                        f"(routed={available_now:.0f}/{actual_hedge_qty:.0f})"
                    )
            except Exception as e:
                print(f"[WARN] hedge failed id={tid}: {e}")

            processed_tender_ids.add(tid)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
