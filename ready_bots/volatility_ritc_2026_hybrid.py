"""RITC 2026 volatility bot hybrid.

Strategy blend:
- Prices options off the latest news-implied vol (fallback to realized vol).
- Runs a realized-vs-implied ATM straddle overlay.
- Hedges portfolio delta in RTM.
"""

from collections import deque
import math
import os
import re
import time

import requests


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _envf(name: str, default: str) -> float:
    return float(_env(name, default))


def _envi(name: str, default: str) -> int:
    return int(_env(name, default))


API_KEY = _env("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = _env("RIT_BASE_URL", "http://localhost:9999/v1")

POLL_SECS = _envf("RIT_VOL2026_POLL_SECS", "0.5")
ORDER_COOLDOWN_SECS = _envf("RIT_VOL2026_ORDER_COOLDOWN_SECS", "1.0")
PRINT_INTERVAL_SECS = _envf("RIT_VOL2026_PRINT_INTERVAL_SECS", "3.0")

PRICE_THRESHOLD = _envf("RIT_VOL2026_PRICE_THRESHOLD", "0.05")
ORDER_QTY = _envi("RIT_VOL2026_ORDER_QTY", "10")
MAX_POS_PER_OPTION = _envi("RIT_VOL2026_MAX_POS_PER_OPTION", "200")

REALIZED_WINDOW = _envi("RIT_VOL2026_REALIZED_WINDOW", "120")
TICKS_PER_YEAR = _envf("RIT_VOL2026_TICKS_PER_YEAR", "15120")
FALLBACK_SIGMA = _envf("RIT_VOL2026_FALLBACK_SIGMA", "0.20")

STRADDLE_QTY = _envi("RIT_VOL2026_STRADDLE_QTY", "5")
STRADDLE_EDGE_THRESHOLD = _envf("RIT_VOL2026_STRADDLE_EDGE_THRESHOLD", "0.03")
STRADDLE_COOLDOWN_SECS = _envf("RIT_VOL2026_STRADDLE_COOLDOWN_SECS", "2.0")

ASSUMED_T_YEARS = _envf("RIT_VOL2026_ASSUMED_T_YEARS", "0.0833333333")
RISK_FREE = _envf("RIT_VOL2026_RISK_FREE", "0.0")

RTM_HEDGE_QTY = _envi("RIT_VOL2026_RTM_HEDGE_QTY", "1000")
HEDGE_TRIGGER = _envf("RIT_VOL2026_HEDGE_TRIGGER", "0.8")
TARGET_DELTA_FRAC = _envf("RIT_VOL2026_TARGET_DELTA_FRAC", "0.35")
SAFE_OPEN_DELTA_FRAC = _envf("RIT_VOL2026_SAFE_OPEN_DELTA_FRAC", "0.60")
MAX_HEDGE_STEPS = _envi("RIT_VOL2026_MAX_HEDGE_STEPS", "4")
ENDGAME_BUFFER_TICKS = _envi("RIT_VOL2026_ENDGAME_BUFFER_TICKS", "20")

OPTION_MAX_TRADE_SIZE = _envi("RIT_VOL2026_OPTION_MAX_TRADE_SIZE", "100")
RTM_MAX_TRADE_SIZE = _envi("RIT_VOL2026_RTM_MAX_TRADE_SIZE", "10000")
OPTION_CONTRACT_MULTIPLIER = 100
OPTION_GROSS_LIMIT = _envi("RIT_VOL2026_OPTION_GROSS_LIMIT", "2500")
OPTION_NET_LIMIT = _envi("RIT_VOL2026_OPTION_NET_LIMIT", "1000")

OPTION_TICKERS = [f"RTM1C{k}" for k in range(45, 55)] + [f"RTM1P{k}" for k in range(45, 55)]


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

    def get_case(self):
        r = self._get("/case")
        r.raise_for_status()
        return r.json()

    def get_news(self, since: int | None = None):
        params = {"since": since} if since is not None else None
        r = self._get("/news", params=params)
        r.raise_for_status()
        return r.json()

    def get_securities(self):
        r = self._get("/securities")
        r.raise_for_status()
        return r.json()

    def get_book(self, ticker: str):
        r = self._get("/securities/book", {"ticker": ticker})
        r.raise_for_status()
        return r.json()

    def get_limits(self):
        r = self._get("/limits")
        r.raise_for_status()
        return r.json()

    def place_order(self, ticker: str, order_type: str, quantity: int, action: str, price: float | None = None):
        params = {"ticker": ticker, "type": order_type, "quantity": quantity, "action": action}
        if price is not None:
            params["price"] = price
        r = self._post("/orders", params=params)
        r.raise_for_status()
        return r.json()


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return
        time.sleep(poll_s)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        if call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    if call:
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def implied_vol(S: float, K: float, T: float, r: float, price: float, call: bool) -> float | None:
    intrinsic = max(0.0, S - K) if call else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return 0.0

    low, high = 1e-4, 5.0
    for _ in range(60):
        mid = (low + high) / 2.0
        val = bs_price(S, K, T, r, mid, call=call)
        if abs(val - price) < 1e-4:
            return mid
        if val > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def best_bid_ask(client: RITClient, ticker: str):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def parse_vol_from_news(news_items):
    vol = None
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"between\s+(\d+(?:\.\d+)?)%\s+and\s+(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
        if m:
            vol = (float(m.group(1)) + float(m.group(2))) / 200.0
            continue
        m = re.search(r"volatility[^\d]*(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
        if m:
            vol = float(m.group(1)) / 100.0
    return vol


def parse_delta_limit(news_items, current_limit: int) -> int:
    limit = current_limit
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"delta limit[^\d]*(\d+)", text, re.IGNORECASE)
        if m:
            limit = int(m.group(1))
    return limit


def parse_penalty_rate(news_items, current_rate: float) -> float:
    rate = current_rate
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"penalty rate[^\d]*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if m:
            rate = float(m.group(1))
            continue
        m = re.search(r"penalty[^\d$]*(?:\$)?(\d+(?:\.\d+)?)\s*(?:per\s*second|/sec|sec)", text, re.IGNORECASE)
        if m:
            rate = float(m.group(1))
    return rate


def option_specs(ticker: str):
    m = re.match(r"RTM1([CP])(\d+)$", ticker)
    if not m:
        return None, None
    return m.group(1) == "C", float(m.group(2))


def choose_atm_strike(S: float) -> int:
    return min(range(45, 55), key=lambda k: abs(S - k))


def compute_portfolio_delta(S: float, positions: dict, sigma: float) -> float:
    delta = positions.get("RTM", 0)
    for t in OPTION_TICKERS:
        pos = positions.get(t, 0)
        if pos == 0:
            continue
        call, K = option_specs(t)
        if call is None:
            continue
        delta += bs_delta(S, K, ASSUMED_T_YEARS, RISK_FREE, sigma, call=call) * pos * 100.0
    return delta


def compute_realized_vol(log_returns: deque[float]) -> float | None:
    if len(log_returns) < max(10, REALIZED_WINDOW // 3):
        return None
    mean_r = sum(log_returns) / len(log_returns)
    var_r = sum((r - mean_r) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
    return math.sqrt(max(0.0, var_r)) * math.sqrt(TICKS_PER_YEAR)


def can_open_more(pos: int, direction: str) -> bool:
    if direction == "BUY":
        return pos < MAX_POS_PER_OPTION
    return pos > -MAX_POS_PER_OPTION


def summarize_option_positions(positions: dict[str, int]) -> tuple[int, int]:
    option_positions = [positions.get(t, 0) for t in OPTION_TICKERS]
    gross = sum(abs(p) for p in option_positions)
    net = sum(option_positions)
    return gross, net


def can_open_option_trade(positions: dict[str, int], ticker: str, direction: str, qty: int) -> bool:
    pos = positions.get(ticker, 0)
    projected = pos + qty if direction == "BUY" else pos - qty
    if abs(projected) > MAX_POS_PER_OPTION:
        return False

    gross, net = summarize_option_positions(positions)
    projected_gross = gross - abs(pos) + abs(projected)
    projected_net = net - pos + projected

    if projected_gross > OPTION_GROSS_LIMIT:
        return False
    if abs(projected_net) > OPTION_NET_LIMIT:
        return False
    return True


def option_delta_change(S: float, sigma: float, ticker: str, action: str, qty: int) -> float:
    call, strike = option_specs(ticker)
    if call is None:
        return 0.0
    d = bs_delta(S, strike, ASSUMED_T_YEARS, RISK_FREE, sigma, call=call)
    sign = 1.0 if action == "BUY" else -1.0
    return sign * d * qty * OPTION_CONTRACT_MULTIPLIER


def place_limit_chunks(client: RITClient, ticker: str, action: str, qty: int, price: float):
    remaining = int(max(0, qty))
    while remaining > 0:
        chunk = min(OPTION_MAX_TRADE_SIZE, remaining)
        client.place_order(ticker, "LIMIT", chunk, action, price=price)
        remaining -= chunk


def place_rtm_market_chunks(client: RITClient, action: str, qty: int):
    remaining = int(max(0, qty))
    while remaining > 0:
        chunk = min(RTM_MAX_TRADE_SIZE, remaining)
        client.place_order("RTM", "MARKET", chunk, action)
        remaining -= chunk


def is_endgame(case: dict) -> bool:
    tick = case.get("tick")
    tpp = case.get("ticks_per_period")
    if not isinstance(tick, (int, float)) or not isinstance(tpp, (int, float)):
        return False
    return int(tick) >= int(tpp) - ENDGAME_BUFFER_TICKS


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    last_news_id = 0
    news_vol = None
    delta_limit = 10000
    penalty_rate = 0.0
    last_order_ts: dict[str, float] = {}
    last_straddle_ts = 0.0
    last_status_print = 0.0

    rtm_returns: deque[float] = deque(maxlen=REALIZED_WINDOW)
    last_rtm_mid = None

    print(f"Connected to {BASE_URL}. Running volatility hybrid bot...")

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            print("Case no longer ACTIVE. Exiting.")
            break

        news = client.get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            news_vol = parse_vol_from_news(news) or news_vol
            delta_limit = parse_delta_limit(news, delta_limit)
            penalty_rate = parse_penalty_rate(news, penalty_rate)

        try:
            limits_snapshot = client.get_limits()
            if isinstance(limits_snapshot, list):
                for item in limits_snapshot:
                    if isinstance(item, dict):
                        val = item.get("net_limit") or item.get("delta_limit")
                        if isinstance(val, (int, float)) and val > 0:
                            delta_limit = int(val)
        except Exception:
            pass

        rtm_bid, rtm_ask = best_bid_ask(client, "RTM")
        if rtm_bid is None or rtm_ask is None:
            time.sleep(POLL_SECS)
            continue
        S = (rtm_bid + rtm_ask) / 2.0

        if last_rtm_mid is not None and S > 0 and last_rtm_mid > 0:
            rtm_returns.append(math.log(S / last_rtm_mid))
        last_rtm_mid = S

        realized_vol = compute_realized_vol(rtm_returns)
        sigma = news_vol if news_vol is not None else (realized_vol if realized_vol is not None else FALLBACK_SIGMA)

        positions = {s["ticker"]: int(s.get("position", 0)) for s in client.get_securities()}
        delta = compute_portfolio_delta(S, positions, sigma)
        near_end = is_endgame(case)
        open_limit = max(100.0, SAFE_OPEN_DELTA_FRAC * abs(delta_limit))
        hedge_limit = max(100.0, HEDGE_TRIGGER * abs(delta_limit))
        target_delta = max(50.0, TARGET_DELTA_FRAC * abs(delta_limit))

        now = time.time()
        if not near_end:
            for ticker in OPTION_TICKERS:
                bid, ask = best_bid_ask(client, ticker)
                if bid is None or ask is None:
                    continue

                call, strike = option_specs(ticker)
                if call is None:
                    continue

                mid = (bid + ask) / 2.0
                theo = bs_price(S, strike, ASSUMED_T_YEARS, RISK_FREE, sigma, call=call)
                edge = theo - mid

                if now - last_order_ts.get(ticker, 0.0) < ORDER_COOLDOWN_SECS:
                    continue

                action = None
                price = None
                if edge > PRICE_THRESHOLD:
                    action = "BUY"
                    price = bid
                elif edge < -PRICE_THRESHOLD:
                    action = "SELL"
                    price = ask
                else:
                    continue

                if not can_open_option_trade(positions, ticker, action, ORDER_QTY):
                    continue

                # Anti-fine guard: do not open a trade that pushes delta closer to breach.
                delta_after = delta + option_delta_change(S, sigma, ticker, action, ORDER_QTY)
                if abs(delta_after) > open_limit and abs(delta_after) >= abs(delta):
                    continue

                place_limit_chunks(client, ticker, action, ORDER_QTY, price)
                positions[ticker] = positions.get(ticker, 0) + (ORDER_QTY if action == "BUY" else -ORDER_QTY)
                delta = delta_after
                last_order_ts[ticker] = now

        if not near_end and realized_vol is not None and now - last_straddle_ts >= STRADDLE_COOLDOWN_SECS:
            atm = choose_atm_strike(S)
            call_ticker = f"RTM1C{atm}"
            put_ticker = f"RTM1P{atm}"
            call_bid, call_ask = best_bid_ask(client, call_ticker)
            put_bid, put_ask = best_bid_ask(client, put_ticker)

            if None not in (call_bid, call_ask, put_bid, put_ask):
                call_mid = (call_bid + call_ask) / 2.0
                put_mid = (put_bid + put_ask) / 2.0
                iv_call = implied_vol(S, float(atm), ASSUMED_T_YEARS, RISK_FREE, call_mid, call=True)
                iv_put = implied_vol(S, float(atm), ASSUMED_T_YEARS, RISK_FREE, put_mid, call=False)
                if iv_call is not None and iv_put is not None:
                    iv_avg = (iv_call + iv_put) / 2.0
                    vol_edge = iv_avg - realized_vol
                    call_pos = positions.get(call_ticker, 0)
                    put_pos = positions.get(put_ticker, 0)

                    if vol_edge > STRADDLE_EDGE_THRESHOLD:
                        if can_open_option_trade(positions, call_ticker, "SELL", STRADDLE_QTY):
                            call_delta_after = delta + option_delta_change(S, sigma, call_ticker, "SELL", STRADDLE_QTY)
                            if abs(call_delta_after) < open_limit or abs(call_delta_after) < abs(delta):
                                place_limit_chunks(client, call_ticker, "SELL", STRADDLE_QTY, call_ask)
                                positions[call_ticker] = call_pos - STRADDLE_QTY
                                delta = call_delta_after
                        if can_open_option_trade(positions, put_ticker, "SELL", STRADDLE_QTY):
                            put_delta_after = delta + option_delta_change(S, sigma, put_ticker, "SELL", STRADDLE_QTY)
                            if abs(put_delta_after) < open_limit or abs(put_delta_after) < abs(delta):
                                place_limit_chunks(client, put_ticker, "SELL", STRADDLE_QTY, put_ask)
                                positions[put_ticker] = put_pos - STRADDLE_QTY
                                delta = put_delta_after
                        last_straddle_ts = now
                    elif vol_edge < -STRADDLE_EDGE_THRESHOLD:
                        if can_open_option_trade(positions, call_ticker, "BUY", STRADDLE_QTY):
                            call_delta_after = delta + option_delta_change(S, sigma, call_ticker, "BUY", STRADDLE_QTY)
                            if abs(call_delta_after) < open_limit or abs(call_delta_after) < abs(delta):
                                place_limit_chunks(client, call_ticker, "BUY", STRADDLE_QTY, call_bid)
                                positions[call_ticker] = call_pos + STRADDLE_QTY
                                delta = call_delta_after
                        if can_open_option_trade(positions, put_ticker, "BUY", STRADDLE_QTY):
                            put_delta_after = delta + option_delta_change(S, sigma, put_ticker, "BUY", STRADDLE_QTY)
                            if abs(put_delta_after) < open_limit or abs(put_delta_after) < abs(delta):
                                place_limit_chunks(client, put_ticker, "BUY", STRADDLE_QTY, put_bid)
                                positions[put_ticker] = put_pos + STRADDLE_QTY
                                delta = put_delta_after
                        last_straddle_ts = now

        if abs(delta_limit) > 0 and abs(delta) > hedge_limit:
            for _ in range(MAX_HEDGE_STEPS):
                if abs(delta) <= target_delta:
                    break
                action = "SELL" if delta > 0 else "BUY"
                required = max(1, int(abs(delta) - target_delta))
                hedge_qty = min(RTM_HEDGE_QTY, RTM_MAX_TRADE_SIZE, required)
                place_rtm_market_chunks(client, action, hedge_qty)
                delta = delta - hedge_qty if action == "SELL" else delta + hedge_qty

        if now - last_status_print >= PRINT_INTERVAL_SECS:
            rv = f"{realized_vol:.3f}" if realized_vol is not None else "n/a"
            nv = f"{news_vol:.3f}" if news_vol is not None else "n/a"
            usage = (abs(delta) / max(1.0, abs(delta_limit))) * 100.0
            print(
                f"[VOL] sigma={sigma:.3f} news={nv} realized={rv} "
                f"delta={delta:.0f} limit={delta_limit} usage={usage:.1f}% "
                f"penalty_rate={penalty_rate:.2f} near_end={near_end}"
            )
            last_status_print = now

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
