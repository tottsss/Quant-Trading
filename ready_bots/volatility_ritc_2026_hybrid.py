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

try:
    from final_risky_reporting import RuntimeTelemetry, write_run_report
except ImportError:
    from ready_bots.final_risky_reporting import RuntimeTelemetry, write_run_report


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _envf(name: str, default: str) -> float:
    return float(_env(name, default))


def _envi(name: str, default: str) -> int:
    return int(_env(name, default))


def _env_bool(name: str, default: str) -> bool:
    return _env(name, default).strip().lower() in {"1", "true", "yes", "on"}


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
SIGMA_FLOOR = max(0.0001, _envf("RIT_VOL2026_SIGMA_FLOOR", "0.05"))
SIGMA_CAP = max(SIGMA_FLOOR, _envf("RIT_VOL2026_SIGMA_CAP", "2.00"))
VOL_MIN_VALID = max(0.0, _envf("RIT_VOL2026_VOL_MIN_VALID", "0.01"))
VOL_MAX_VALID = max(VOL_MIN_VALID, _envf("RIT_VOL2026_VOL_MAX_VALID", "2.50"))
ENTRY_BLOCK_WHEN_DELTA_STRESSED = _env_bool("RIT_VOL2026_ENTRY_BLOCK_WHEN_DELTA_STRESSED", "1")

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
VOL_WEEK_TICKS = max(1, _envi("RIT_VOL2026_VOL_WEEK_TICKS", "75"))

OPTION_MAX_TRADE_SIZE = _envi("RIT_VOL2026_OPTION_MAX_TRADE_SIZE", "100")
RTM_MAX_TRADE_SIZE = _envi("RIT_VOL2026_RTM_MAX_TRADE_SIZE", "10000")
OPTION_CONTRACT_MULTIPLIER = 100
OPTION_GROSS_LIMIT = _envi("RIT_VOL2026_OPTION_GROSS_LIMIT", "2500")
OPTION_NET_LIMIT = _envi("RIT_VOL2026_OPTION_NET_LIMIT", "1000")
PASSIVE_HEDGE_ENABLED = _env_bool("RIT_VOL2026_PASSIVE_HEDGE_ENABLED", "1")
PASSIVE_HEDGE_QTY = max(1, _envi("RIT_VOL2026_PASSIVE_HEDGE_QTY", "500"))
SAVE_REPORT_ON_EXIT = _env_bool("RIT_VOL2026_SAVE_REPORT_ON_EXIT", "1")
REPORT_PREFIX = _env("RIT_VOL2026_REPORT_PREFIX", "volatility_ritc_2026_hybrid_report")
REPORT_EVENTS_CAP = max(100, _envi("RIT_VOL2026_REPORT_EVENTS_CAP", "4000"))
REPORT_TENDER_LOG_CAP = max(100, _envi("RIT_VOL2026_REPORT_TENDER_LOG_CAP", "1500"))
REPORT_HEDGE_LOG_CAP = max(100, _envi("RIT_VOL2026_REPORT_HEDGE_LOG_CAP", "2000"))
REPORT_PORTFOLIO_LOG_CAP = max(50, _envi("RIT_VOL2026_REPORT_PORTFOLIO_LOG_CAP", "1500"))
REPORT_ORDER_LOG_CAP = max(100, _envi("RIT_VOL2026_REPORT_ORDER_LOG_CAP", "4000"))
REPORT_ERROR_LOG_CAP = max(50, _envi("RIT_VOL2026_REPORT_ERROR_LOG_CAP", "1500"))

OPTION_TICKERS = [f"RTM1C{k}" for k in range(45, 55)] + [f"RTM1P{k}" for k in range(45, 55)]

REPORTER = RuntimeTelemetry(
    event_cap=REPORT_EVENTS_CAP,
    tender_log_cap=REPORT_TENDER_LOG_CAP,
    hedge_log_cap=REPORT_HEDGE_LOG_CAP,
    portfolio_log_cap=REPORT_PORTFOLIO_LOG_CAP,
    order_log_cap=REPORT_ORDER_LOG_CAP,
    error_log_cap=REPORT_ERROR_LOG_CAP,
)


def _bump_counter(key: str, inc: int = 1):
    REPORTER.bump_counter(key, inc)


def _record_event(kind: str, message: str | None = None, **fields):
    return REPORTER.record_event(kind, message=message, **fields)


def _record_error(where: str, exc, **context):
    REPORTER.record_error(where, exc, **context)


def _record_portfolio_log(**fields):
    REPORTER.record_portfolio_log(**fields)


def _record_order_log(order_type: str, **fields):
    REPORTER.record_order_log(order_type, **fields)


def _safe_get_json(client: "RITClient", path: str, params: dict | None = None):
    try:
        r = client.session.get(client.base_url + path, params=params, timeout=client.timeout)
        if not r.ok:
            _record_error("_safe_get_json", f"http_{r.status_code}", path=path, params=params or {})
            return {"_error": f"http_{r.status_code}", "_path": path, "_params": params or {}}
        return r.json()
    except Exception as exc:
        _record_error("_safe_get_json", exc, path=path, params=params or {})
        return {"_error": str(exc), "_path": path, "_params": params or {}}


def _mark_price(row: dict) -> float | None:
    for key in ("last", "close", "price"):
        px = row.get(key)
        if isinstance(px, (int, float)):
            return float(px)
    return None


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


def save_run_report(client: "RITClient", reason: str, run_error: str | None = None):
    case_info = _safe_get_json(client, "/case")
    trader_info = _safe_get_json(client, "/trader")
    limits_info = _safe_get_json(client, "/limits")
    securities = _safe_get_json(client, "/securities")
    tenders = _safe_get_json(client, "/tenders")
    orders_all = _safe_get_json(client, "/orders")
    orders_open = _safe_get_json(client, "/orders", params={"status": "OPEN"})
    orders_transacted = _safe_get_json(client, "/orders", params={"status": "TRANSACTED"})
    orders_cancelled = _safe_get_json(client, "/orders", params={"status": "CANCELLED"})

    config = {
        "POLL_SECS": POLL_SECS,
        "ORDER_COOLDOWN_SECS": ORDER_COOLDOWN_SECS,
        "PRINT_INTERVAL_SECS": PRINT_INTERVAL_SECS,
        "PRICE_THRESHOLD": PRICE_THRESHOLD,
        "ORDER_QTY": ORDER_QTY,
        "MAX_POS_PER_OPTION": MAX_POS_PER_OPTION,
        "REALIZED_WINDOW": REALIZED_WINDOW,
        "TICKS_PER_YEAR": TICKS_PER_YEAR,
        "FALLBACK_SIGMA": FALLBACK_SIGMA,
        "SIGMA_FLOOR": SIGMA_FLOOR,
        "SIGMA_CAP": SIGMA_CAP,
        "VOL_MIN_VALID": VOL_MIN_VALID,
        "VOL_MAX_VALID": VOL_MAX_VALID,
        "ENTRY_BLOCK_WHEN_DELTA_STRESSED": ENTRY_BLOCK_WHEN_DELTA_STRESSED,
        "STRADDLE_QTY": STRADDLE_QTY,
        "STRADDLE_EDGE_THRESHOLD": STRADDLE_EDGE_THRESHOLD,
        "STRADDLE_COOLDOWN_SECS": STRADDLE_COOLDOWN_SECS,
        "ASSUMED_T_YEARS": ASSUMED_T_YEARS,
        "RISK_FREE": RISK_FREE,
        "RTM_HEDGE_QTY": RTM_HEDGE_QTY,
        "HEDGE_TRIGGER": HEDGE_TRIGGER,
        "TARGET_DELTA_FRAC": TARGET_DELTA_FRAC,
        "SAFE_OPEN_DELTA_FRAC": SAFE_OPEN_DELTA_FRAC,
        "MAX_HEDGE_STEPS": MAX_HEDGE_STEPS,
        "ENDGAME_BUFFER_TICKS": ENDGAME_BUFFER_TICKS,
        "VOL_WEEK_TICKS": VOL_WEEK_TICKS,
        "OPTION_MAX_TRADE_SIZE": OPTION_MAX_TRADE_SIZE,
        "RTM_MAX_TRADE_SIZE": RTM_MAX_TRADE_SIZE,
        "OPTION_CONTRACT_MULTIPLIER": OPTION_CONTRACT_MULTIPLIER,
        "OPTION_GROSS_LIMIT": OPTION_GROSS_LIMIT,
        "OPTION_NET_LIMIT": OPTION_NET_LIMIT,
        "PASSIVE_HEDGE_ENABLED": PASSIVE_HEDGE_ENABLED,
        "PASSIVE_HEDGE_QTY": PASSIVE_HEDGE_QTY,
        "REPORT_EVENTS_CAP": REPORT_EVENTS_CAP,
        "REPORT_TENDER_LOG_CAP": REPORT_TENDER_LOG_CAP,
        "REPORT_HEDGE_LOG_CAP": REPORT_HEDGE_LOG_CAP,
        "REPORT_PORTFOLIO_LOG_CAP": REPORT_PORTFOLIO_LOG_CAP,
        "REPORT_ORDER_LOG_CAP": REPORT_ORDER_LOG_CAP,
        "REPORT_ERROR_LOG_CAP": REPORT_ERROR_LOG_CAP,
    }

    out_path = write_run_report(
        script_path=__file__,
        report_prefix=REPORT_PREFIX,
        base_url=BASE_URL,
        reason=reason,
        run_error=run_error,
        config=config,
        case_info=case_info,
        trader_info=trader_info,
        limits_info=limits_info,
        securities=securities,
        position_summary=_compute_position_summary(securities),
        tenders=tenders,
        orders_all=orders_all,
        orders_open=orders_open,
        orders_transacted=orders_transacted,
        orders_cancelled=orders_cancelled,
        telemetry=REPORTER,
    )
    print(f"[REPORT] saved: {out_path}")
    return out_path


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

    def get_open_orders(self, ticker: str | None = None):
        params = {"status": "OPEN"}
        if ticker:
            params["ticker"] = ticker
        r = self._get("/orders", params=params)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: int):
        r = self.session.delete(f"{self.base_url}/orders/{order_id}", timeout=self.timeout)
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


def _extract_vol_from_text(text: str) -> float | None:
    m = re.search(r"between\s+(\d+(?:\.\d+)?)%\s+and\s+(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 200.0
    text_lower = text.lower()
    # Match a % that is explicitly tied to volatility context while avoiding
    # nearby "risk free rate X%" and similar non-vol percentages.
    for pct_match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
        pct = float(pct_match.group(1))
        left_ctx = text_lower[max(0, pct_match.start() - 48) : pct_match.start()]
        near_ctx = text_lower[max(0, pct_match.start() - 20) : min(len(text_lower), pct_match.end() + 20)]
        if "volatility" not in left_ctx and "iv" not in left_ctx:
            continue
        if "risk free" in near_ctx or "delta limit" in near_ctx or "penalty" in near_ctx:
            continue
        return pct / 100.0
    m = re.search(r"\biv\b[^\d]*(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 100.0
    return None


def _current_week_from_tick(current_tick: int | float) -> int:
    return max(1, (int(current_tick) // VOL_WEEK_TICKS) + 1)


def _extract_news_week(news_item: dict) -> int | None:
    for key in ("ticker", "symbol", "headline"):
        value = news_item.get(key)
        if not isinstance(value, str):
            continue
        m = re.search(r"\bweek\s*[:#-]?\s*([1-9]\d*)\b", value, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def parse_vol_from_news(news_items, current_tick: int | float):
    """
    Returns a mapping {target_week: volatility_float}.
    Vol forecasts tagged as "next week" are scheduled to next week.
    """
    current_week = _current_week_from_tick(current_tick)
    forecasts = {}
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        vol = _extract_vol_from_text(text)
        if vol is None:
            continue
        if vol <= VOL_MIN_VALID or vol > VOL_MAX_VALID:
            continue
        news_week = _extract_news_week(n)
        base_week = news_week if news_week is not None else current_week
        is_next_week = "next week" in text.lower()
        target_week = base_week + 1 if is_next_week else base_week
        forecasts[target_week] = float(vol)
    return forecasts


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
        m = re.search(r"penalty(?:\s+percentage|\s+percent)?[^\d]*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
        if m:
            rate = float(m.group(1))
            continue
        m = re.search(r"penalty[^\d$]*(?:\$)?(\d+(?:\.\d+)?)\s*(?:per\s*second|/sec|sec)", text, re.IGNORECASE)
        if m:
            rate = float(m.group(1))
    return rate


def parse_risk_free_rate(news_items, current_rate: float) -> float:
    rate = current_rate
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"risk\s*free\s*rate[^\d-]*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
        if m:
            rate = float(m.group(1)) / 100.0
    return rate


def option_specs(ticker: str):
    m = re.match(r"RTM1([CP])(\d+)$", ticker)
    if not m:
        return None, None
    return m.group(1) == "C", float(m.group(2))


def choose_atm_strike(S: float) -> int:
    return min(range(45, 55), key=lambda k: abs(S - k))


def compute_portfolio_delta(S: float, positions: dict, sigma: float, r: float = RISK_FREE) -> float:
    delta = positions.get("RTM", 0)
    for t in OPTION_TICKERS:
        pos = positions.get(t, 0)
        if pos == 0:
            continue
        call, K = option_specs(t)
        if call is None:
            continue
        delta += bs_delta(S, K, ASSUMED_T_YEARS, r, sigma, call=call) * pos * 100.0
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


def option_delta_change(S: float, sigma: float, ticker: str, action: str, qty: int, r: float = RISK_FREE) -> float:
    call, strike = option_specs(ticker)
    if call is None:
        return 0.0
    d = bs_delta(S, strike, ASSUMED_T_YEARS, r, sigma, call=call)
    sign = 1.0 if action == "BUY" else -1.0
    return sign * d * qty * OPTION_CONTRACT_MULTIPLIER


def place_limit_chunks(client: RITClient, ticker: str, action: str, qty: int, price: float):
    remaining = int(max(0, qty))
    while remaining > 0:
        chunk = min(OPTION_MAX_TRADE_SIZE, remaining)
        try:
            client.place_order(ticker, "LIMIT", chunk, action, price=price)
            _bump_counter("limit_orders_submitted", 1)
            _record_order_log(
                "LIMIT",
                ticker=ticker,
                action=action,
                quantity=int(chunk),
                price=float(price),
                status="submitted",
            )
        except Exception as exc:
            _record_order_log(
                "LIMIT",
                ticker=ticker,
                action=action,
                quantity=int(chunk),
                price=float(price),
                status="failed",
                error=str(exc),
            )
            _record_error("place_limit_chunks", exc, ticker=ticker, action=action, quantity=int(chunk), price=float(price))
            raise
        remaining -= chunk


def place_rtm_limit_chunks(client: RITClient, action: str, qty: int, price: float):
    remaining = int(max(0, qty))
    while remaining > 0:
        chunk = min(RTM_MAX_TRADE_SIZE, remaining)
        try:
            client.place_order("RTM", "LIMIT", chunk, action, price=price)
            _bump_counter("limit_orders_submitted", 1)
            _record_order_log(
                "LIMIT",
                ticker="RTM",
                action=action,
                quantity=int(chunk),
                price=float(price),
                status="submitted",
            )
        except Exception as exc:
            _record_order_log(
                "LIMIT",
                ticker="RTM",
                action=action,
                quantity=int(chunk),
                price=float(price),
                status="failed",
                error=str(exc),
            )
            _record_error("place_rtm_limit_chunks", exc, action=action, quantity=int(chunk), price=float(price))
            raise
        remaining -= chunk


def place_rtm_market_chunks(client: RITClient, action: str, qty: int):
    remaining = int(max(0, qty))
    while remaining > 0:
        chunk = min(RTM_MAX_TRADE_SIZE, remaining)
        try:
            client.place_order("RTM", "MARKET", chunk, action)
            _bump_counter("market_orders_submitted", 1)
            _record_order_log(
                "MARKET",
                ticker="RTM",
                action=action,
                quantity=int(chunk),
                status="submitted",
            )
        except Exception as exc:
            _record_order_log(
                "MARKET",
                ticker="RTM",
                action=action,
                quantity=int(chunk),
                status="failed",
                error=str(exc),
            )
            _record_error("place_rtm_market_chunks", exc, action=action, quantity=int(chunk))
            raise
        remaining -= chunk


def is_endgame(case: dict) -> bool:
    tick = case.get("tick")
    tpp = case.get("ticks_per_period")
    if not isinstance(tick, (int, float)) or not isinstance(tpp, (int, float)):
        return False
    return int(tick) >= int(tpp) - ENDGAME_BUFFER_TICKS


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _open_order_unfilled_qty(order: dict) -> int:
    qty = _to_float(order.get("quantity", order.get("qty", 0.0)), 0.0)
    filled = _to_float(
        order.get(
            "quantity_filled",
            order.get("qty_filled", order.get("filled_quantity", order.get("quantity_transacted", 0.0))),
        ),
        0.0,
    )
    return max(0, int(qty - filled))


def _same_price(a: float, b: float, tick_tol: float = 0.005) -> bool:
    return abs(float(a) - float(b)) <= float(tick_tol)


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    last_news_id = 0
    vol_forecasts: dict[int, float] = {}
    active_news_vol = None
    active_news_week = None
    delta_limit = 10000
    penalty_rate = 0.0
    last_order_ts: dict[str, float] = {}
    last_straddle_ts = 0.0
    last_status_print = 0.0

    rtm_returns: deque[float] = deque(maxlen=REALIZED_WINDOW)
    last_rtm_mid = None
    exit_reason = "shutdown_or_manual_stop"
    run_error = None

    print(f"Connected to {BASE_URL}. Running volatility hybrid bot...")
    _record_event("run_start", base_url=BASE_URL, report_prefix=REPORT_PREFIX)

    try:
        while True:
            _bump_counter("loops", 1)
            case = client.get_case()
            if case.get("status") != "ACTIVE":
                print("Case no longer ACTIVE. Exiting.")
                exit_reason = "case_inactive"
                break

            raw_tick = case.get("tick", 0)
            current_tick = int(raw_tick) if isinstance(raw_tick, (int, float)) else 0
            current_week = _current_week_from_tick(current_tick)

            if active_news_week != current_week and current_week in vol_forecasts:
                active_news_vol = float(vol_forecasts[current_week])
                prior_week = active_news_week
                active_news_week = current_week
                _record_event(
                    "news_vol_activated",
                    week_index=int(current_week),
                    active_news_vol=float(active_news_vol),
                    previous_week=prior_week,
                )

            news = client.get_news(since=last_news_id)
            if news:
                last_news_id = max(n["news_id"] for n in news)
                delta_limit = parse_delta_limit(news, delta_limit)
                penalty_rate = parse_penalty_rate(news, penalty_rate)
                forecasts = parse_vol_from_news(news, current_tick)
                for target_week, vol in sorted(forecasts.items()):
                    vol_forecasts[int(target_week)] = float(vol)
                    _record_event(
                        "vol_forecast_update",
                        target_week=int(target_week),
                        vol=float(vol),
                    )
                if current_week in vol_forecasts:
                    active_news_vol = float(vol_forecasts[current_week])
                    active_news_week = current_week
                _bump_counter("news_updates", 1)
                _record_event(
                    "news_update",
                    count=len(news),
                    last_news_id=int(last_news_id),
                    active_news_vol=active_news_vol,
                    active_news_week=active_news_week,
                    queued_news_vol=vol_forecasts.get(current_week + 1),
                    delta_limit=int(delta_limit),
                    penalty_rate=float(penalty_rate),
                )

            try:
                limits_snapshot = client.get_limits()
                if isinstance(limits_snapshot, list):
                    for item in limits_snapshot:
                        if isinstance(item, dict):
                            val = item.get("net_limit") or item.get("delta_limit")
                            if isinstance(val, (int, float)) and val > 0:
                                delta_limit = int(val)
            except Exception as exc:
                _record_error("get_limits", exc)

            rtm_bid, rtm_ask = best_bid_ask(client, "RTM")
            if rtm_bid is None or rtm_ask is None:
                _record_event("skip_no_rtm_book")
                time.sleep(POLL_SECS)
                continue
            S = (rtm_bid + rtm_ask) / 2.0

            if last_rtm_mid is not None and S > 0 and last_rtm_mid > 0:
                rtm_returns.append(math.log(S / last_rtm_mid))
            last_rtm_mid = S

            realized_vol = compute_realized_vol(rtm_returns)
            sigma_raw = active_news_vol if active_news_vol is not None else (realized_vol if realized_vol is not None else FALLBACK_SIGMA)
            sigma = min(SIGMA_CAP, max(SIGMA_FLOOR, float(sigma_raw)))

            positions = {s["ticker"]: int(s.get("position", 0)) for s in client.get_securities()}
            delta = compute_portfolio_delta(S, positions, sigma)
            near_end = is_endgame(case)
            open_limit = max(100.0, SAFE_OPEN_DELTA_FRAC * abs(delta_limit))
            target_delta = max(50.0, TARGET_DELTA_FRAC * abs(delta_limit))
            soft_limit = max(100.0, SAFE_OPEN_DELTA_FRAC * abs(delta_limit))
            hard_limit = max(100.0, HEDGE_TRIGGER * abs(delta_limit))
            entry_blocked_by_delta = ENTRY_BLOCK_WHEN_DELTA_STRESSED and abs(delta) > soft_limit

            now = time.time()
            if not near_end and not entry_blocked_by_delta:
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
                        price = ask
                    elif edge < -PRICE_THRESHOLD:
                        action = "SELL"
                        price = bid
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
                    _bump_counter("option_trades", 1)
                    _record_event(
                        "option_trade",
                        ticker=ticker,
                        action=action,
                        quantity=int(ORDER_QTY),
                        edge=float(edge),
                        price=float(price),
                        sigma=float(sigma),
                    )
            elif entry_blocked_by_delta:
                _record_event(
                    "entry_blocked_delta_stressed",
                    delta=float(delta),
                    soft_limit=float(soft_limit),
                    hard_limit=float(hard_limit),
                    sigma=float(sigma),
                )

            straddle_ref_vol = active_news_vol if active_news_vol is not None else realized_vol
            straddle_ref_source = "news" if active_news_vol is not None else "realized"
            if (
                not near_end
                and not entry_blocked_by_delta
                and straddle_ref_vol is not None
                and now - last_straddle_ts >= STRADDLE_COOLDOWN_SECS
            ):
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
                        vol_edge = iv_avg - straddle_ref_vol
                        call_pos = positions.get(call_ticker, 0)
                        put_pos = positions.get(put_ticker, 0)

                        if vol_edge > STRADDLE_EDGE_THRESHOLD:
                            legs = 0
                            if can_open_option_trade(positions, call_ticker, "SELL", STRADDLE_QTY):
                                call_delta_after = delta + option_delta_change(S, sigma, call_ticker, "SELL", STRADDLE_QTY)
                                if abs(call_delta_after) < open_limit or abs(call_delta_after) < abs(delta):
                                    place_limit_chunks(client, call_ticker, "SELL", STRADDLE_QTY, call_bid)
                                    positions[call_ticker] = call_pos - STRADDLE_QTY
                                    delta = call_delta_after
                                    legs += 1
                            if can_open_option_trade(positions, put_ticker, "SELL", STRADDLE_QTY):
                                put_delta_after = delta + option_delta_change(S, sigma, put_ticker, "SELL", STRADDLE_QTY)
                                if abs(put_delta_after) < open_limit or abs(put_delta_after) < abs(delta):
                                    place_limit_chunks(client, put_ticker, "SELL", STRADDLE_QTY, put_bid)
                                    positions[put_ticker] = put_pos - STRADDLE_QTY
                                    delta = put_delta_after
                                    legs += 1
                            if legs > 0:
                                last_straddle_ts = now
                                _bump_counter("straddle_trades", legs)
                                _record_event(
                                    "straddle_trade",
                                    mode="sell_vol",
                                    atm=int(atm),
                                    legs=int(legs),
                                    vol_edge=float(vol_edge),
                                    iv_avg=float(iv_avg),
                                    ref_vol=float(straddle_ref_vol),
                                    ref_source=straddle_ref_source,
                                    realized_vol=realized_vol,
                                )
                        elif vol_edge < -STRADDLE_EDGE_THRESHOLD:
                            legs = 0
                            if can_open_option_trade(positions, call_ticker, "BUY", STRADDLE_QTY):
                                call_delta_after = delta + option_delta_change(S, sigma, call_ticker, "BUY", STRADDLE_QTY)
                                if abs(call_delta_after) < open_limit or abs(call_delta_after) < abs(delta):
                                    place_limit_chunks(client, call_ticker, "BUY", STRADDLE_QTY, call_ask)
                                    positions[call_ticker] = call_pos + STRADDLE_QTY
                                    delta = call_delta_after
                                    legs += 1
                            if can_open_option_trade(positions, put_ticker, "BUY", STRADDLE_QTY):
                                put_delta_after = delta + option_delta_change(S, sigma, put_ticker, "BUY", STRADDLE_QTY)
                                if abs(put_delta_after) < open_limit or abs(put_delta_after) < abs(delta):
                                    place_limit_chunks(client, put_ticker, "BUY", STRADDLE_QTY, put_ask)
                                    positions[put_ticker] = put_pos + STRADDLE_QTY
                                    delta = put_delta_after
                                    legs += 1
                            if legs > 0:
                                last_straddle_ts = now
                                _bump_counter("straddle_trades", legs)
                                _record_event(
                                    "straddle_trade",
                                    mode="buy_vol",
                                    atm=int(atm),
                                    legs=int(legs),
                                    vol_edge=float(vol_edge),
                                    iv_avg=float(iv_avg),
                                    ref_vol=float(straddle_ref_vol),
                                    ref_source=straddle_ref_source,
                                    realized_vol=realized_vol,
                                )

            if abs(delta_limit) > 0:
                if abs(delta) > soft_limit:
                    action = "SELL" if delta > 0 else "BUY"
                    passive_price = rtm_ask if action == "SELL" else rtm_bid

                    try:
                        open_rtm_orders = client.get_open_orders(ticker="RTM")
                    except Exception as exc:
                        _record_error("get_open_orders", exc)
                        open_rtm_orders = []

                    pending_hedge_qty = 0
                    for order in open_rtm_orders if isinstance(open_rtm_orders, list) else []:
                        order_id = order.get("order_id")
                        order_action = str(order.get("action", "")).upper()
                        order_price = _to_float(order.get("price", 0.0), 0.0)
                        unfilled_qty = _open_order_unfilled_qty(order)
                        is_stale = (order_action != action) or (not _same_price(order_price, passive_price))
                        if is_stale:
                            if order_id is None:
                                continue
                            try:
                                client.cancel_order(int(order_id))
                                _bump_counter("cancelled_orders", 1)
                                _record_order_log(
                                    "CANCEL",
                                    ticker="RTM",
                                    order_id=int(order_id),
                                    action=order_action,
                                    price=order_price,
                                    quantity_unfilled=int(unfilled_qty),
                                    status="cancelled",
                                )
                            except Exception as exc:
                                msg = str(exc)
                                if "404" in msg:
                                    _record_event("cancel_order_race", order_id=int(order_id), ticker="RTM")
                                else:
                                    _record_error("cancel_order", exc, order_id=order_id, ticker="RTM")
                        else:
                            pending_hedge_qty += int(unfilled_qty)

                    total_required = max(0, int(abs(delta) - target_delta))
                    net_required = max(0, total_required - int(pending_hedge_qty))
                    if net_required > 0:
                        hedge_qty = min(RTM_HEDGE_QTY, RTM_MAX_TRADE_SIZE, net_required)

                        if abs(delta) > hard_limit:
                            before_delta = delta
                            place_rtm_market_chunks(client, action, hedge_qty)
                            delta = delta - hedge_qty if action == "SELL" else delta + hedge_qty
                            _bump_counter("hard_delta_hedges", 1)
                            _record_event(
                                "delta_hedge_hard_market",
                                action=action,
                                quantity=int(hedge_qty),
                                delta_before=float(before_delta),
                                delta_after=float(delta),
                                soft_limit=float(soft_limit),
                                hard_limit=float(hard_limit),
                                target_delta=float(target_delta),
                                pending_hedge_qty=int(pending_hedge_qty),
                                total_required=int(total_required),
                                net_required=int(net_required),
                            )
                        elif PASSIVE_HEDGE_ENABLED:
                            place_rtm_limit_chunks(client, action, hedge_qty, passive_price)
                            _bump_counter("soft_delta_hedges", 1)
                            _record_event(
                                "delta_hedge_soft_passive",
                                action=action,
                                quantity=int(hedge_qty),
                                price=float(passive_price),
                                delta=float(delta),
                                soft_limit=float(soft_limit),
                                hard_limit=float(hard_limit),
                                target_delta=float(target_delta),
                                pending_hedge_qty=int(pending_hedge_qty),
                                total_required=int(total_required),
                                net_required=int(net_required),
                            )
                    else:
                        _record_event(
                            "delta_hedge_pending_sufficient",
                            action=action,
                            delta=float(delta),
                            soft_limit=float(soft_limit),
                            hard_limit=float(hard_limit),
                            target_delta=float(target_delta),
                            pending_hedge_qty=int(pending_hedge_qty),
                            total_required=int(total_required),
                            net_required=int(net_required),
                        )
                else:
                    # No hedge needed: clear resting RTM hedge orders to avoid stale exposure.
                    try:
                        open_rtm_orders = client.get_open_orders(ticker="RTM")
                    except Exception as exc:
                        _record_error("get_open_orders_clear", exc)
                        open_rtm_orders = []
                    for order in open_rtm_orders if isinstance(open_rtm_orders, list) else []:
                        order_id = order.get("order_id")
                        if order_id is None:
                            continue
                        try:
                            client.cancel_order(int(order_id))
                            _bump_counter("cancelled_orders", 1)
                            _record_order_log(
                                "CANCEL",
                                ticker="RTM",
                                order_id=int(order_id),
                                action=str(order.get("action", "")).upper(),
                                price=_to_float(order.get("price", 0.0), 0.0),
                                quantity_unfilled=int(_open_order_unfilled_qty(order)),
                                status="cancelled",
                            )
                        except Exception as exc:
                            msg = str(exc)
                            if "404" in msg:
                                _record_event("cancel_order_clear_race", order_id=int(order_id), ticker="RTM")
                            else:
                                _record_error("cancel_order_clear", exc, order_id=order_id, ticker="RTM")

            if now - last_status_print >= PRINT_INTERVAL_SECS:
                rv = f"{realized_vol:.3f}" if realized_vol is not None else "n/a"
                nv = f"{active_news_vol:.3f}" if active_news_vol is not None else "n/a"
                queued_news_vol = vol_forecasts.get(current_week + 1)
                qv = f"{queued_news_vol:.3f}" if queued_news_vol is not None else "n/a"
                usage = (abs(delta) / max(1.0, abs(delta_limit))) * 100.0
                print(
                    f"[VOL] sigma={sigma:.3f} news={nv} queued={qv} realized={rv} "
                    f"delta={delta:.0f} limit={delta_limit} usage={usage:.1f}% "
                    f"penalty_rate={penalty_rate:.2f} near_end={near_end}"
                )
                _bump_counter("portfolio_logs", 1)
                _record_portfolio_log(
                    tick=case.get("tick"),
                    ticks_per_period=case.get("ticks_per_period"),
                    sigma=float(sigma),
                    news_vol=active_news_vol,
                    queued_news_vol=queued_news_vol,
                    current_week=int(current_week),
                    realized_vol=realized_vol,
                    delta=float(delta),
                    delta_limit=int(delta_limit),
                    usage_pct=float(usage),
                    penalty_rate=float(penalty_rate),
                    near_end=bool(near_end),
                )
                last_status_print = now

            time.sleep(POLL_SECS)
    except Exception as exc:
        run_error = str(exc)
        exit_reason = "fatal_error"
        print(f"Fatal error: {exc}")
        _record_error("main_loop", exc)
    finally:
        _record_event("run_end", exit_reason=exit_reason, run_error=run_error)
        if SAVE_REPORT_ON_EXIT:
            try:
                save_run_report(client, exit_reason, run_error=run_error)
            except Exception as exc:
                print(f"Report save error: {exc}")
                _record_error("save_run_report", exc, exit_reason=exit_reason)
        try:
            client.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
