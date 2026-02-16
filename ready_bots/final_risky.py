import os
import signal
import time
import math
import requests

try:
    from final_risky_reporting import RuntimeTelemetry, write_run_report
except ImportError:
    from ready_bots.final_risky_reporting import RuntimeTelemetry, write_run_report


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
TENDER_MONITOR_INTERVAL = max(
    0.005, float(_env("RIT_FINAL_RISKY_TENDER_MONITOR_INTERVAL", "RIT_FINAL_TENDER_MONITOR_INTERVAL", "0.020"))
)
TENDER_MONITOR_FAST_INTERVAL = max(
    0.003,
    float(_env("RIT_FINAL_RISKY_TENDER_MONITOR_FAST_INTERVAL", "RIT_FINAL_TENDER_MONITOR_FAST_INTERVAL", "0.008")),
)
TENDER_MONITOR_EDGE_INTERVAL = max(
    0.003,
    float(_env("RIT_FINAL_RISKY_TENDER_MONITOR_EDGE_INTERVAL", "RIT_FINAL_TENDER_MONITOR_EDGE_INTERVAL", "0.006")),
)
TENDER_MONITOR_LOG_EVERY = max(
    1, int(_env("RIT_FINAL_RISKY_TENDER_MONITOR_LOG_EVERY", "RIT_FINAL_TENDER_MONITOR_LOG_EVERY", "10"))
)
TENDER_MONITOR_MAX_POLLS = max(
    20, int(_env("RIT_FINAL_RISKY_TENDER_MONITOR_MAX_POLLS", "RIT_FINAL_TENDER_MONITOR_MAX_POLLS", "1200"))
)
TENDER_TICK_REFRESH_SECS = max(
    0.05,
    float(_env("RIT_FINAL_RISKY_TENDER_TICK_REFRESH_SECS", "RIT_FINAL_TENDER_TICK_REFRESH_SECS", "0.25")),
)
ORDER_DELAY = float(_env("RIT_FINAL_RISKY_ORDER_DELAY", "RIT_FINAL_ORDER_DELAY", "0.05"))
AFTER_ACCEPT_DELAY = float(_env("RIT_FINAL_RISKY_AFTER_ACCEPT_DELAY", "RIT_FINAL_AFTER_ACCEPT_DELAY", "0.20"))
MAX_ORDER_SIZE = 10000.0# Maximum order size
DEPTH_LEVELS = max(1, int(_env("RIT_FINAL_RISKY_DEPTH_LEVELS", "RIT_DEPTH_LEVELS", "10")))
BOOK_FETCH_LIMIT = max(20, int(_env("RIT_FINAL_RISKY_BOOK_FETCH_LIMIT", "RIT_FINAL_BOOK_FETCH_LIMIT", "120")))
ENDGAME_TICKS = int(_env("RIT_FINAL_RISKY_ENDGAME_TICKS", "RIT_FINAL_ENDGAME_TICKS", "10"))# Number of ticks to end the game
FIXED_ONLY = _env_bool("RIT_FINAL_RISKY_FIXED_ONLY", "RIT_FINAL_FIXED_ONLY", "0")# Whether to only accept fixed tenders
AGGRESSIVE_MODE = _env_bool("RIT_FINAL_RISKY_AGGRESSIVE", "RIT_FINAL_AGGRESSIVE", "1")
EDGE_FLOOR_RATIO = float(_env("RIT_FINAL_RISKY_EDGE_FLOOR_RATIO", "RIT_FINAL_EDGE_FLOOR_RATIO", "0.15"))
EDGE_DECAY_PER_ATTEMPT = float(_env("RIT_FINAL_RISKY_EDGE_DECAY_PER_ATTEMPT", "RIT_FINAL_EDGE_DECAY_PER_ATTEMPT", "0.020"))
VOL_RELAX_PER_ATTEMPT = float(_env("RIT_FINAL_RISKY_VOL_RELAX_PER_ATTEMPT", "RIT_FINAL_VOL_RELAX_PER_ATTEMPT", "0.12"))
HEDGE_RATIO = float(_env("RIT_FINAL_RISKY_HEDGE_RATIO", "RIT_FINAL_HEDGE_RATIO", "0.25" if AGGRESSIVE_MODE else "0.70"))
HEDGE_RATIO = max(0.0, min(1.0, HEDGE_RATIO))
DYN_HEDGE_RATIO_ENABLED = _env_bool("RIT_FINAL_RISKY_DYN_HEDGE_RATIO_ENABLED", "RIT_FINAL_DYN_HEDGE_RATIO_ENABLED", "1")
DYN_HEDGE_RATIO_MIN = max(
    0.0, min(1.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_RATIO_MIN", "RIT_FINAL_DYN_HEDGE_RATIO_MIN", "0.25")))
)
DYN_HEDGE_RATIO_MAX = max(
    DYN_HEDGE_RATIO_MIN,
    min(1.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_RATIO_MAX", "RIT_FINAL_DYN_HEDGE_RATIO_MAX", "1.00"))),
)
DYN_HEDGE_VOL_WEIGHT = max(
    0.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_VOL_WEIGHT", "RIT_FINAL_DYN_HEDGE_VOL_WEIGHT", "0.55"))
)
DYN_HEDGE_SPREAD_WEIGHT = max(
    0.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_SPREAD_WEIGHT", "RIT_FINAL_DYN_HEDGE_SPREAD_WEIGHT", "0.35"))
)
DYN_HEDGE_ADVERSE_BONUS = max(
    0.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_ADVERSE_BONUS", "RIT_FINAL_DYN_HEDGE_ADVERSE_BONUS", "0.20"))
)
DYN_HEDGE_FAVORABLE_DISCOUNT = max(
    0.0, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_FAVORABLE_DISCOUNT", "RIT_FINAL_DYN_HEDGE_FAVORABLE_DISCOUNT", "0.08"))
)
DYN_HEDGE_VOL_REF = max(
    0.0001, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_VOL_REF", "RIT_FINAL_DYN_HEDGE_VOL_REF", "0.0030"))
)
DYN_HEDGE_SPREAD_BPS_REF = max(
    0.5, float(_env("RIT_FINAL_RISKY_DYN_HEDGE_SPREAD_BPS_REF", "RIT_FINAL_DYN_HEDGE_SPREAD_BPS_REF", "8.0"))
)
PORTFOLIO_PRINT_INTERVAL = float(_env("RIT_FINAL_RISKY_PORTFOLIO_PRINT_INTERVAL", "RIT_FINAL_PORTFOLIO_PRINT_INTERVAL", "5.0"))
TAKE_PROFIT_ENABLED = _env_bool("RIT_FINAL_RISKY_TAKE_PROFIT_ENABLED", "RIT_FINAL_TAKE_PROFIT_ENABLED", "1")
TAKE_PROFIT_PER_SHARE = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_PER_SHARE", "RIT_FINAL_TAKE_PROFIT_PER_SHARE", "0.15"))
TAKE_PROFIT_CHUNK_QTY = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_CHUNK_QTY", "RIT_FINAL_TAKE_PROFIT_CHUNK_QTY", "10000"))
TAKE_PROFIT_CHUNK_QTY = max(1.0, TAKE_PROFIT_CHUNK_QTY)
TAKE_PROFIT_MIN_CHUNK_QTY = max(
    1.0, float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_MIN_CHUNK_QTY", "RIT_FINAL_TAKE_PROFIT_MIN_CHUNK_QTY", "500"))
)
TAKE_PROFIT_SPREAD_BPS_REF = max(
    0.5, float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_SPREAD_BPS_REF", "RIT_FINAL_TAKE_PROFIT_SPREAD_BPS_REF", "8.0"))
)
TAKE_PROFIT_SPREAD_POWER = max(
    0.2, float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_SPREAD_POWER", "RIT_FINAL_TAKE_PROFIT_SPREAD_POWER", "1.0"))
)
TAKE_PROFIT_TOP_LEVEL_PARTICIPATION = max(
    0.05,
    min(
        1.0,
        float(
            _env(
                "RIT_FINAL_RISKY_TAKE_PROFIT_TOP_LEVEL_PARTICIPATION",
                "RIT_FINAL_TAKE_PROFIT_TOP_LEVEL_PARTICIPATION",
                "0.60",
            )
        ),
    ),
)
TAKE_PROFIT_COOLDOWN = float(_env("RIT_FINAL_RISKY_TAKE_PROFIT_COOLDOWN", "RIT_FINAL_TAKE_PROFIT_COOLDOWN", "2.0"))
STOP_LOSS_ENABLED = _env_bool("RIT_FINAL_RISKY_STOP_LOSS_ENABLED", "RIT_FINAL_STOP_LOSS_ENABLED", "0")
STOP_LOSS_PER_SHARE = float(_env("RIT_FINAL_RISKY_STOP_LOSS_PER_SHARE", "RIT_FINAL_STOP_LOSS_PER_SHARE", "0.30"))
STOP_LOSS_CHUNK_QTY = float(_env("RIT_FINAL_RISKY_STOP_LOSS_CHUNK_QTY", "RIT_FINAL_STOP_LOSS_CHUNK_QTY", "10000"))
STOP_LOSS_CHUNK_QTY = max(1.0, STOP_LOSS_CHUNK_QTY)
STOP_LOSS_COOLDOWN = float(_env("RIT_FINAL_RISKY_STOP_LOSS_COOLDOWN", "RIT_FINAL_STOP_LOSS_COOLDOWN", "2.0"))
BOOK_OUTLIER_BPS = float(_env("RIT_FINAL_RISKY_BOOK_OUTLIER_BPS", "RIT_FINAL_BOOK_OUTLIER_BPS", "60"))
BOOK_OUTLIER_SPREAD_MULT = float(_env("RIT_FINAL_RISKY_BOOK_OUTLIER_SPREAD_MULT", "RIT_FINAL_BOOK_OUTLIER_SPREAD_MULT", "8.0"))
BOOK_MAX_LEVEL_QTY = float(_env("RIT_FINAL_RISKY_BOOK_MAX_LEVEL_QTY", "RIT_FINAL_BOOK_MAX_LEVEL_QTY", "25000"))
BOOK_DECISION_MAX_LEVELS = max(
    1, int(_env("RIT_FINAL_RISKY_BOOK_DECISION_MAX_LEVELS", "RIT_FINAL_BOOK_DECISION_MAX_LEVELS", "12"))
)
BOOK_DECISION_MAX_BPS = max(
    0.0, float(_env("RIT_FINAL_RISKY_BOOK_DECISION_MAX_BPS", "RIT_FINAL_BOOK_DECISION_MAX_BPS", "40"))
)
BOOK_DECISION_MIN_LEVELS = max(
    1, int(_env("RIT_FINAL_RISKY_BOOK_DECISION_MIN_LEVELS", "RIT_FINAL_BOOK_DECISION_MIN_LEVELS", "3"))
)
BOOK_MIN_FILL_RATIO = max(
    0.0,
    min(1.0, float(_env("RIT_FINAL_RISKY_BOOK_MIN_FILL_RATIO", "RIT_FINAL_BOOK_MIN_FILL_RATIO", "0.95"))),
)
LIMIT_FALLBACK_GROSS = float(_env("RIT_FINAL_RISKY_LIMIT_FALLBACK_GROSS", "RIT_FINAL_LIMIT_FALLBACK_GROSS", "250000"))
LIMIT_FALLBACK_NET = float(_env("RIT_FINAL_RISKY_LIMIT_FALLBACK_NET", "RIT_FINAL_LIMIT_FALLBACK_NET", "150000"))
AUCTION_TICK = float(_env("RIT_FINAL_RISKY_AUCTION_TICK", "RIT_FINAL_AUCTION_TICK", "0.01"))
MOM_TAS_LIMIT = max(10, int(_env("RIT_FINAL_RISKY_MOM_TAS_LIMIT", "RIT_FINAL_MOM_TAS_LIMIT", "60")))
MOM_EMA_FAST = max(2, int(_env("RIT_FINAL_RISKY_MOM_EMA_FAST", "RIT_FINAL_MOM_EMA_FAST", "8")))
MOM_EMA_SLOW = max(MOM_EMA_FAST + 1, int(_env("RIT_FINAL_RISKY_MOM_EMA_SLOW", "RIT_FINAL_MOM_EMA_SLOW", "21")))
MOM_RSI_PERIOD = max(3, int(_env("RIT_FINAL_RISKY_MOM_RSI_PERIOD", "RIT_FINAL_MOM_RSI_PERIOD", "14")))
MOM_IMBALANCE_LEVELS = max(1, int(_env("RIT_FINAL_RISKY_MOM_IMBALANCE_LEVELS", "RIT_FINAL_MOM_IMBALANCE_LEVELS", "5")))
MOM_IMBALANCE_THRESHOLD = float(_env("RIT_FINAL_RISKY_MOM_IMBALANCE_THRESHOLD", "RIT_FINAL_MOM_IMBALANCE_THRESHOLD", "0.15"))
HEDGE_MOMENTUM_AWARE = _env_bool("RIT_FINAL_RISKY_HEDGE_MOMENTUM_AWARE", "RIT_FINAL_HEDGE_MOMENTUM_AWARE", "1")
HEDGE_MIN_TICKET_QTY = float(_env("RIT_FINAL_RISKY_HEDGE_MIN_TICKET_QTY", "RIT_FINAL_HEDGE_MIN_TICKET_QTY", "1500"))
HEDGE_FAVORABLE_MULT = float(_env("RIT_FINAL_RISKY_HEDGE_FAVORABLE_MULT", "RIT_FINAL_HEDGE_FAVORABLE_MULT", "0.75"))
HEDGE_ADVERSE_MULT = float(_env("RIT_FINAL_RISKY_HEDGE_ADVERSE_MULT", "RIT_FINAL_HEDGE_ADVERSE_MULT", "1.35"))
HEDGE_REGIME_REFRESH_SECS = float(_env("RIT_FINAL_RISKY_HEDGE_REGIME_REFRESH_SECS", "RIT_FINAL_HEDGE_REGIME_REFRESH_SECS", "0.35"))
HEDGE_MAX_TICKETS = max(1, int(_env("RIT_FINAL_RISKY_HEDGE_MAX_TICKETS", "RIT_FINAL_HEDGE_MAX_TICKETS", "30")))
HEDGE_MARKETABLE_OFFSET = float(_env("RIT_FINAL_RISKY_HEDGE_MARKETABLE_OFFSET", "RIT_FINAL_HEDGE_MARKETABLE_OFFSET", "0.02"))
HEDGE_MARKETABLE_OFFSET_MAX = float(_env("RIT_FINAL_RISKY_HEDGE_MARKETABLE_OFFSET_MAX", "RIT_FINAL_HEDGE_MARKETABLE_OFFSET_MAX", "0.08"))
HEDGE_FALLBACK_CHUNK_QTY = float(_env("RIT_FINAL_RISKY_HEDGE_FALLBACK_CHUNK_QTY", "RIT_FINAL_HEDGE_FALLBACK_CHUNK_QTY", "1000"))
HEDGE_MAX_FALLBACK_SLICES = max(1, int(_env("RIT_FINAL_RISKY_HEDGE_MAX_FALLBACK_SLICES", "RIT_FINAL_HEDGE_MAX_FALLBACK_SLICES", "80")))
HEDGE_ALLOW_MARKET_FALLBACK = _env_bool("RIT_FINAL_RISKY_HEDGE_ALLOW_MARKET_FALLBACK", "RIT_FINAL_HEDGE_ALLOW_MARKET_FALLBACK", "0")
REGIME_CACHE_TTL = float(_env("RIT_FINAL_RISKY_REGIME_CACHE_TTL", "RIT_FINAL_REGIME_CACHE_TTL", "0.45"))
TREND_EDGE_FAVORABLE_MULT = float(_env("RIT_FINAL_RISKY_TREND_EDGE_FAVORABLE_MULT", "RIT_FINAL_TREND_EDGE_FAVORABLE_MULT", "0.88"))
TREND_EDGE_ADVERSE_MULT = float(_env("RIT_FINAL_RISKY_TREND_EDGE_ADVERSE_MULT", "RIT_FINAL_TREND_EDGE_ADVERSE_MULT", "1.25"))
TREND_STRICT_ACCEPT = _env_bool("RIT_FINAL_RISKY_TREND_STRICT_ACCEPT", "RIT_FINAL_TREND_STRICT_ACCEPT", "0")
TREND_STRICT_MIN_CONF = float(_env("RIT_FINAL_RISKY_TREND_STRICT_MIN_CONF", "RIT_FINAL_TREND_STRICT_MIN_CONF", "0.75"))
REGIME_SCORE_FAVORABLE = float(_env("RIT_FINAL_RISKY_REGIME_SCORE_FAVORABLE", "RIT_FINAL_REGIME_SCORE_FAVORABLE", "1.25"))
REGIME_SCORE_ADVERSE = float(_env("RIT_FINAL_RISKY_REGIME_SCORE_ADVERSE", "RIT_FINAL_REGIME_SCORE_ADVERSE", "-1.25"))
TREND_MIN_GAP_BPS = float(_env("RIT_FINAL_RISKY_TREND_MIN_GAP_BPS", "RIT_FINAL_TREND_MIN_GAP_BPS", "2.0"))
RSI_HIGH = float(_env("RIT_FINAL_RISKY_RSI_HIGH", "RIT_FINAL_RSI_HIGH", "56"))
RSI_LOW = float(_env("RIT_FINAL_RISKY_RSI_LOW", "RIT_FINAL_RSI_LOW", "44"))
VOL_LOOKBACK = max(8, int(_env("RIT_FINAL_RISKY_VOL_LOOKBACK", "RIT_FINAL_VOL_LOOKBACK", "24")))
VOL_HIGH = float(_env("RIT_FINAL_RISKY_VOL_HIGH", "RIT_FINAL_VOL_HIGH", "0.0030"))
VOL_SHOCK = float(_env("RIT_FINAL_RISKY_VOL_SHOCK", "RIT_FINAL_VOL_SHOCK", "0.0060"))
FLATTEN_FAVORABLE_MULT = float(_env("RIT_FINAL_RISKY_FLATTEN_FAVORABLE_MULT", "RIT_FINAL_FLATTEN_FAVORABLE_MULT", "0.70"))
FLATTEN_ADVERSE_MULT = float(_env("RIT_FINAL_RISKY_FLATTEN_ADVERSE_MULT", "RIT_FINAL_FLATTEN_ADVERSE_MULT", "1.40"))
FLATTEN_MIN_TICKET_QTY = float(_env("RIT_FINAL_RISKY_FLATTEN_MIN_TICKET_QTY", "RIT_FINAL_FLATTEN_MIN_TICKET_QTY", "2000"))
FLATTEN_HARD_DEADLINE_TICKS = max(
    1,
    int(_env("RIT_FINAL_RISKY_FLATTEN_HARD_DEADLINE_TICKS", "RIT_FINAL_FLATTEN_HARD_DEADLINE_TICKS", "2")),
)
SAVE_REPORT_ON_EXIT = _env_bool("RIT_FINAL_RISKY_SAVE_REPORT_ON_EXIT", "RIT_FINAL_SAVE_REPORT_ON_EXIT", "1")
REPORT_PREFIX = _env("RIT_FINAL_RISKY_REPORT_PREFIX", "RIT_FINAL_REPORT_PREFIX", "final_risky_report")
REPORT_EVENTS_CAP = max(100, int(_env("RIT_FINAL_RISKY_REPORT_EVENTS_CAP", "RIT_FINAL_REPORT_EVENTS_CAP", "6000")))
REPORT_TENDER_LOG_CAP = max(
    100, int(_env("RIT_FINAL_RISKY_REPORT_TENDER_LOG_CAP", "RIT_FINAL_REPORT_TENDER_LOG_CAP", "5000"))
)
REPORT_HEDGE_LOG_CAP = max(
    100, int(_env("RIT_FINAL_RISKY_REPORT_HEDGE_LOG_CAP", "RIT_FINAL_REPORT_HEDGE_LOG_CAP", "5000"))
)
REPORT_PORTFOLIO_LOG_CAP = max(
    50, int(_env("RIT_FINAL_RISKY_REPORT_PORTFOLIO_LOG_CAP", "RIT_FINAL_REPORT_PORTFOLIO_LOG_CAP", "2000"))
)
REPORT_ORDER_LOG_CAP = max(
    100, int(_env("RIT_FINAL_RISKY_REPORT_ORDER_LOG_CAP", "RIT_FINAL_REPORT_ORDER_LOG_CAP", "6000"))
)
REPORT_ERROR_LOG_CAP = max(
    50, int(_env("RIT_FINAL_RISKY_REPORT_ERROR_LOG_CAP", "RIT_FINAL_REPORT_ERROR_LOG_CAP", "2000"))
)
REPORT_ORDERS_FETCH_LIMIT = max(
    100, int(_env("RIT_FINAL_RISKY_REPORT_ORDERS_FETCH_LIMIT", "RIT_FINAL_REPORT_ORDERS_FETCH_LIMIT", "5000"))
)
HEAT_RESET_TICK_DROP = max(
    5, int(_env("RIT_FINAL_RISKY_HEAT_RESET_TICK_DROP", "RIT_FINAL_HEAT_RESET_TICK_DROP", "20"))
)

REPORTER = RuntimeTelemetry(
    event_cap=REPORT_EVENTS_CAP,
    tender_log_cap=REPORT_TENDER_LOG_CAP,
    hedge_log_cap=REPORT_HEDGE_LOG_CAP,
    portfolio_log_cap=REPORT_PORTFOLIO_LOG_CAP,
    order_log_cap=REPORT_ORDER_LOG_CAP,
    error_log_cap=REPORT_ERROR_LOG_CAP,
)


def _bump_counter(key, inc=1):
    REPORTER.bump_counter(key, inc)


def _record_event(kind, message=None, **fields):
    return REPORTER.record_event(kind, message=message, **fields)


def _record_error(where, exc, **context):
    REPORTER.record_error(where, exc, **context)


def _record_tender_log(event, **fields):
    REPORTER.record_tender_log(event, **fields)


def _record_hedge_log(event, **fields):
    REPORTER.record_hedge_log(event, **fields)


def _record_portfolio_log(**fields):
    REPORTER.record_portfolio_log(**fields)


def _record_order_log(order_type, **fields):
    REPORTER.record_order_log(order_type, **fields)


def signal_handler(signum, frame):
    del signum, frame
    global SHUTDOWN
    SHUTDOWN = True
    print("Shutting down...")


signal.signal(signal.SIGINT, signal_handler)


def get_case(session):
    r = session.get(f"{BASE_URL}/case")
    if not r.ok:
        _record_error("get_case", f"http_{r.status_code}")
        raise ApiException("Failed to fetch case")
    return r.json()


def get_tick(session):
    case = get_case(session)
    return int(case.get("tick", 0)), int(case.get("ticks_per_period", 600)), str(case.get("status", ""))


def get_tenders(session):
    r = session.get(f"{BASE_URL}/tenders")
    if not r.ok:
        _record_error("get_tenders", f"http_{r.status_code}")
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
        _record_error("get_securities", f"http_{r.status_code}")
        raise ApiException("Failed to fetch securities")
    return r.json()


def _safe_get_json(session, path, params=None):
    try:
        r = session.get(f"{BASE_URL}{path}", params=params)
        if not r.ok:
            _record_error("_safe_get_json", f"http_{r.status_code}", path=path, params=params or {})
            return {
                "_error": f"http_{r.status_code}",
                "_path": path,
                "_params": params or {},
            }
        return r.json()
    except Exception as exc:
        _record_error("_safe_get_json", exc, path=path, params=params or {})
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
        cost_basis = None
        for key in ("cost", "vwap"):
            val = s.get(key)
            if isinstance(val, (int, float)) and float(val) > 0:
                cost_basis = float(val)
                break
        by_ticker[ticker] = {"position": pos, "mark_price": px, "cost_basis": cost_basis}
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


def _infer_limits(limits_payload):
    gross_vals = []
    net_vals = []
    rows = limits_payload if isinstance(limits_payload, list) else [limits_payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        g = row.get("gross_limit", row.get("gross"))
        n = row.get("net_limit", row.get("net"))
        if isinstance(g, (int, float)) and g > 0:
            gross_vals.append(float(g))
        if isinstance(n, (int, float)) and n > 0:
            net_vals.append(float(n))
    gross_limit = min(gross_vals) if gross_vals else LIMIT_FALLBACK_GROSS
    net_limit = min(net_vals) if net_vals else LIMIT_FALLBACK_NET
    return gross_limit, net_limit


def _positions_by_ticker(securities):
    out = {}
    if not isinstance(securities, list):
        return out
    for row in securities:
        if not isinstance(row, dict):
            continue
        tk = row.get("ticker")
        pos = row.get("position")
        if tk and isinstance(pos, (int, float)):
            out[tk] = float(pos)
    return out


def _pretrade_limit_gate(session, ticker, my_action, qty):
    if my_action not in {"BUY", "SELL"}:
        return False, {"reason": "unknown_action"}
    if qty <= 0:
        return False, {"reason": "invalid_qty"}

    limits_payload = _safe_get_json(session, "/limits")
    gross_limit, net_limit = _infer_limits(limits_payload)
    securities = get_securities(session)
    summary = _compute_position_summary(securities)
    positions = _positions_by_ticker(securities)

    tender_qty = float(qty)
    delta_qty = tender_qty if my_action == "BUY" else -tender_qty
    gross_now = float(summary.get("gross_position", 0.0))
    net_now = float(summary.get("net_position", 0.0))
    old_pos = float(positions.get(ticker, 0.0))
    new_pos = old_pos + delta_qty

    gross_after = gross_now - abs(old_pos) + abs(new_pos)
    net_after = net_now + delta_qty

    gross_violation = gross_after > gross_limit
    net_violation = abs(net_after) > net_limit

    # Strict pre-trade gate: do not accept tender if projected position breaches limits.
    blocked = gross_violation or net_violation

    return (not blocked), {
        "ticker": ticker,
        "side": my_action,
        "new_tender_quantity": tender_qty,
        "current_net_position": net_now,
        "current_gross_position": gross_now,
        "gross_now": gross_now,
        "gross_after": gross_after,
        "gross_limit": gross_limit,
        "net_now": net_now,
        "net_after": net_after,
        "net_limit": net_limit,
        "gross_violation": gross_violation,
        "net_violation": net_violation,
    }


def _round_to_tick(price, tick, mode="nearest"):
    px = float(price)
    if tick <= 0:
        return round(px, 2)
    units = px / tick
    if mode == "down":
        snapped = math.floor(units) * tick
    elif mode == "up":
        snapped = math.ceil(units) * tick
    else:
        snapped = round(units) * tick
    return round(max(0.01, snapped), 2)


def save_run_report(session, reason, run_error=None):
    case_info = _safe_get_json(session, "/case")
    trader_info = _safe_get_json(session, "/trader")
    limits_info = _safe_get_json(session, "/limits")
    securities = _safe_get_json(session, "/securities")
    tenders = _safe_get_json(session, "/tenders")
    base_order_params = {"limit": REPORT_ORDERS_FETCH_LIMIT}
    orders_all = _safe_get_json(session, "/orders", params=dict(base_order_params))
    orders_open = _safe_get_json(session, "/orders", params={**base_order_params, "status": "OPEN"})
    orders_transacted = _safe_get_json(session, "/orders", params={**base_order_params, "status": "TRANSACTED"})
    orders_cancelled = _safe_get_json(session, "/orders", params={**base_order_params, "status": "CANCELLED"})
    config = {
        "MIN_EDGE": MIN_EDGE,
        "VOL_FACTOR": VOL_FACTOR,
        "MAX_ATTEMPTS": MAX_ATTEMPTS,
        "EVAL_DELAY": EVAL_DELAY,
        "TENDER_MONITOR_INTERVAL": TENDER_MONITOR_INTERVAL,
        "TENDER_MONITOR_FAST_INTERVAL": TENDER_MONITOR_FAST_INTERVAL,
        "TENDER_MONITOR_EDGE_INTERVAL": TENDER_MONITOR_EDGE_INTERVAL,
        "TENDER_MONITOR_LOG_EVERY": TENDER_MONITOR_LOG_EVERY,
        "TENDER_MONITOR_MAX_POLLS": TENDER_MONITOR_MAX_POLLS,
        "TENDER_TICK_REFRESH_SECS": TENDER_TICK_REFRESH_SECS,
        "ORDER_DELAY": ORDER_DELAY,
        "AFTER_ACCEPT_DELAY": AFTER_ACCEPT_DELAY,
        "MAX_ORDER_SIZE": MAX_ORDER_SIZE,
        "DEPTH_LEVELS": DEPTH_LEVELS,
        "BOOK_FETCH_LIMIT": BOOK_FETCH_LIMIT,
        "ENDGAME_TICKS": ENDGAME_TICKS,
        "FIXED_ONLY": FIXED_ONLY,
        "AGGRESSIVE_MODE": AGGRESSIVE_MODE,
        "HEDGE_RATIO": HEDGE_RATIO,
        "DYN_HEDGE_RATIO_ENABLED": DYN_HEDGE_RATIO_ENABLED,
        "DYN_HEDGE_RATIO_MIN": DYN_HEDGE_RATIO_MIN,
        "DYN_HEDGE_RATIO_MAX": DYN_HEDGE_RATIO_MAX,
        "DYN_HEDGE_VOL_WEIGHT": DYN_HEDGE_VOL_WEIGHT,
        "DYN_HEDGE_SPREAD_WEIGHT": DYN_HEDGE_SPREAD_WEIGHT,
        "DYN_HEDGE_ADVERSE_BONUS": DYN_HEDGE_ADVERSE_BONUS,
        "DYN_HEDGE_FAVORABLE_DISCOUNT": DYN_HEDGE_FAVORABLE_DISCOUNT,
        "DYN_HEDGE_VOL_REF": DYN_HEDGE_VOL_REF,
        "DYN_HEDGE_SPREAD_BPS_REF": DYN_HEDGE_SPREAD_BPS_REF,
        "TAKE_PROFIT_ENABLED": TAKE_PROFIT_ENABLED,
        "TAKE_PROFIT_PER_SHARE": TAKE_PROFIT_PER_SHARE,
        "TAKE_PROFIT_CHUNK_QTY": TAKE_PROFIT_CHUNK_QTY,
        "TAKE_PROFIT_MIN_CHUNK_QTY": TAKE_PROFIT_MIN_CHUNK_QTY,
        "TAKE_PROFIT_SPREAD_BPS_REF": TAKE_PROFIT_SPREAD_BPS_REF,
        "TAKE_PROFIT_SPREAD_POWER": TAKE_PROFIT_SPREAD_POWER,
        "TAKE_PROFIT_TOP_LEVEL_PARTICIPATION": TAKE_PROFIT_TOP_LEVEL_PARTICIPATION,
        "TAKE_PROFIT_COOLDOWN": TAKE_PROFIT_COOLDOWN,
        "STOP_LOSS_ENABLED": STOP_LOSS_ENABLED,
        "STOP_LOSS_PER_SHARE": STOP_LOSS_PER_SHARE,
        "STOP_LOSS_CHUNK_QTY": STOP_LOSS_CHUNK_QTY,
        "STOP_LOSS_COOLDOWN": STOP_LOSS_COOLDOWN,
        "BOOK_OUTLIER_BPS": BOOK_OUTLIER_BPS,
        "BOOK_OUTLIER_SPREAD_MULT": BOOK_OUTLIER_SPREAD_MULT,
        "BOOK_MAX_LEVEL_QTY": BOOK_MAX_LEVEL_QTY,
        "BOOK_DECISION_MAX_LEVELS": BOOK_DECISION_MAX_LEVELS,
        "BOOK_DECISION_MAX_BPS": BOOK_DECISION_MAX_BPS,
        "BOOK_DECISION_MIN_LEVELS": BOOK_DECISION_MIN_LEVELS,
        "BOOK_MIN_FILL_RATIO": BOOK_MIN_FILL_RATIO,
        "LIMIT_FALLBACK_GROSS": LIMIT_FALLBACK_GROSS,
        "LIMIT_FALLBACK_NET": LIMIT_FALLBACK_NET,
        "AUCTION_TICK": AUCTION_TICK,
        "MOM_TAS_LIMIT": MOM_TAS_LIMIT,
        "MOM_EMA_FAST": MOM_EMA_FAST,
        "MOM_EMA_SLOW": MOM_EMA_SLOW,
        "MOM_RSI_PERIOD": MOM_RSI_PERIOD,
        "MOM_IMBALANCE_LEVELS": MOM_IMBALANCE_LEVELS,
        "MOM_IMBALANCE_THRESHOLD": MOM_IMBALANCE_THRESHOLD,
        "HEDGE_MOMENTUM_AWARE": HEDGE_MOMENTUM_AWARE,
        "HEDGE_MIN_TICKET_QTY": HEDGE_MIN_TICKET_QTY,
        "HEDGE_FAVORABLE_MULT": HEDGE_FAVORABLE_MULT,
        "HEDGE_ADVERSE_MULT": HEDGE_ADVERSE_MULT,
        "HEDGE_REGIME_REFRESH_SECS": HEDGE_REGIME_REFRESH_SECS,
        "HEDGE_MAX_TICKETS": HEDGE_MAX_TICKETS,
        "HEDGE_MARKETABLE_OFFSET": HEDGE_MARKETABLE_OFFSET,
        "HEDGE_MARKETABLE_OFFSET_MAX": HEDGE_MARKETABLE_OFFSET_MAX,
        "HEDGE_FALLBACK_CHUNK_QTY": HEDGE_FALLBACK_CHUNK_QTY,
        "HEDGE_MAX_FALLBACK_SLICES": HEDGE_MAX_FALLBACK_SLICES,
        "HEDGE_ALLOW_MARKET_FALLBACK": HEDGE_ALLOW_MARKET_FALLBACK,
        "REGIME_CACHE_TTL": REGIME_CACHE_TTL,
        "TREND_EDGE_FAVORABLE_MULT": TREND_EDGE_FAVORABLE_MULT,
        "TREND_EDGE_ADVERSE_MULT": TREND_EDGE_ADVERSE_MULT,
        "TREND_STRICT_ACCEPT": TREND_STRICT_ACCEPT,
        "TREND_STRICT_MIN_CONF": TREND_STRICT_MIN_CONF,
        "REGIME_SCORE_FAVORABLE": REGIME_SCORE_FAVORABLE,
        "REGIME_SCORE_ADVERSE": REGIME_SCORE_ADVERSE,
        "TREND_MIN_GAP_BPS": TREND_MIN_GAP_BPS,
        "RSI_HIGH": RSI_HIGH,
        "RSI_LOW": RSI_LOW,
        "VOL_LOOKBACK": VOL_LOOKBACK,
        "VOL_HIGH": VOL_HIGH,
        "VOL_SHOCK": VOL_SHOCK,
        "FLATTEN_FAVORABLE_MULT": FLATTEN_FAVORABLE_MULT,
        "FLATTEN_ADVERSE_MULT": FLATTEN_ADVERSE_MULT,
        "FLATTEN_MIN_TICKET_QTY": FLATTEN_MIN_TICKET_QTY,
        "FLATTEN_HARD_DEADLINE_TICKS": FLATTEN_HARD_DEADLINE_TICKS,
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
    fetch_limit = max(20, BOOK_FETCH_LIMIT, DEPTH_LEVELS * 3)
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

    bids_all = []
    for lv in raw_bids:
        if max_dev is not None and lv["price"] < (best_bid - max_dev):
            continue
        q = _cap_qty(lv["quantity"])
        if q <= 0:
            continue
        bids_all.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})

    asks_all = []
    for lv in raw_asks:
        if max_dev is not None and lv["price"] > (best_ask + max_dev):
            continue
        q = _cap_qty(lv["quantity"])
        if q <= 0:
            continue
        asks_all.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})

    if not bids_all and raw_bids:
        for lv in raw_bids[:fetch_limit]:
            q = _cap_qty(lv["quantity"])
            if q <= 0:
                continue
            bids_all.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})
    if not asks_all and raw_asks:
        for lv in raw_asks[:fetch_limit]:
            q = _cap_qty(lv["quantity"])
            if q <= 0:
                continue
            asks_all.append({"ticker": lv["ticker"], "price": lv["price"], "quantity": q})

    bids = bids_all[:DEPTH_LEVELS]
    asks = asks_all[:DEPTH_LEVELS]

    bid_vol = sum(x["quantity"] for x in bids_all)
    ask_vol = sum(x["quantity"] for x in asks_all)
    vwap_bid = (sum(x["price"] * x["quantity"] for x in bids_all) / bid_vol) if bid_vol > 0 else 0.0
    vwap_ask = (sum(x["price"] * x["quantity"] for x in asks_all) / ask_vol) if ask_vol > 0 else 0.0

    return {
        "books": books,
        "bids": bids,
        "asks": asks,
        "bids_all": bids_all,
        "asks_all": asks_all,
        "bid_volume": bid_vol,
        "ask_volume": ask_vol,
        "vwap_bid": vwap_bid,
        "vwap_ask": vwap_ask,
        "best_bid": bids_all[0]["price"] if bids_all else best_bid,
        "best_ask": asks_all[0]["price"] if asks_all else best_ask,
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


def accept_tender(session, tender, submit_price=None):
    tid = tender.get("tender_id")
    if tid is None:
        _record_tender_log("accept_failed_missing_tender_id", tender=tender)
        return False
    params = {}
    if submit_price is not None:
        params["price"] = float(submit_price)
    r = session.post(f"{BASE_URL}/tenders/{tid}", params=params or None)
    if not r.ok:
        print(f"Accept failed tender {tid}: status={r.status_code}")
        _bump_counter("tenders_submit_failed", 1)
        _record_tender_log(
            "accept_failed",
            tender_id=tid,
            ticker=tender.get("ticker"),
            action=tender.get("action"),
            submit_price=submit_price,
            status_code=r.status_code,
        )
        return False
    mode = "auction_bid" if submit_price is not None else "fixed_accept"
    px_str = f" submit={float(submit_price):.2f}" if submit_price is not None else ""
    print(f"Accepted Tender {tid}: {tender.get('ticker')} {tender.get('action')} @ {tender.get('price')} [{mode}]{px_str}")
    _bump_counter("tenders_accepted", 1)
    _record_tender_log(
        "accepted",
        tender_id=tid,
        ticker=tender.get("ticker"),
        action=tender.get("action"),
        tender_price=tender.get("price"),
        submit_price=submit_price,
        mode=mode,
        quantity=tender.get("quantity"),
    )
    return True


def decline_tender(session, tender):
    tid = tender.get("tender_id")
    if tid is None:
        _record_tender_log("decline_failed_missing_tender_id", tender=tender)
        return False
    r = session.delete(f"{BASE_URL}/tenders/{tid}")
    if not r.ok:
        print(f"Decline failed tender {tid}: status={r.status_code}")
        _record_tender_log(
            "decline_failed",
            tender_id=tid,
            ticker=tender.get("ticker"),
            action=tender.get("action"),
            status_code=r.status_code,
        )
        return False
    print(f"Declined Tender {tid}: {tender.get('ticker')} {tender.get('action')} @ {tender.get('price')}")
    _bump_counter("tenders_declined", 1)
    _record_tender_log(
        "declined",
        tender_id=tid,
        ticker=tender.get("ticker"),
        action=tender.get("action"),
        tender_price=tender.get("price"),
        quantity=tender.get("quantity"),
        is_fixed=tender.get("is_fixed_bid"),
    )
    return True


def submit_limit_order(session, ticker, quantity, price, action):
    qty_i = int(math.floor(abs(float(quantity))))
    if qty_i < 1:
        raise ApiException(f"LIMIT order invalid qty {quantity} for {ticker} {action}")
    qty_i = min(qty_i, int(MAX_ORDER_SIZE))
    order = {"ticker": ticker, "type": "LIMIT", "quantity": qty_i, "action": action, "price": price}
    r = session.post(f"{BASE_URL}/orders", params=order)
    if not r.ok:
        _record_order_log(
            "LIMIT",
            ticker=ticker,
            action=action,
            quantity=qty_i,
            price=price,
            status="failed",
            status_code=r.status_code,
        )
        raise ApiException(f"LIMIT order failed {ticker} {action} {qty_i} @ {price}")
    print(f"Placed {action} LIMIT order: {qty_i} @ {price:.2f} on {ticker}")
    _bump_counter("limit_orders_submitted", 1)
    _record_order_log(
        "LIMIT",
        ticker=ticker,
        action=action,
        quantity=qty_i,
        price=price,
        status="submitted",
    )


def submit_market_order(session, ticker, quantity, action):
    qty = int(math.floor(abs(float(quantity))))
    if qty < 1:
        return
    max_chunk = max(1, int(MAX_ORDER_SIZE))
    while qty > 0:
        chunk = min(max_chunk, qty)
        order = {"ticker": ticker, "type": "MARKET", "quantity": chunk, "action": action}
        r = session.post(f"{BASE_URL}/orders", params=order)
        if not r.ok:
            _record_order_log(
                "MARKET",
                ticker=ticker,
                action=action,
                quantity=chunk,
                status="failed",
                status_code=r.status_code,
            )
            raise ApiException(f"MARKET order failed {ticker} {action} {chunk}")
        print(f"Placed {action} MARKET order: {chunk} on {ticker}")
        _bump_counter("market_orders_submitted", 1)
        _record_order_log(
            "MARKET",
            ticker=ticker,
            action=action,
            quantity=chunk,
            status="submitted",
        )
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
        _bump_counter("portfolio_logs", 1)
        _record_portfolio_log(
            tick=int(tick),
            ticks_per_period=int(tpp),
            flat=True,
            net_position=0.0,
            gross_position=0.0,
            gross_notional=0.0,
            positions=[],
        )
        return

    print(
        f"[PORTFOLIO] tick={tick}/{tpp} net={net_pos:.0f} "
        f"gross={gross_pos:.0f} gross_notional={gross_notional:.2f}"
    )
    print("[PORTFOLIO] " + " | ".join(lines))
    _bump_counter("portfolio_logs", 1)
    _record_portfolio_log(
        tick=int(tick),
        ticks_per_period=int(tpp),
        flat=False,
        net_position=float(net_pos),
        gross_position=float(gross_pos),
        gross_notional=float(gross_notional),
        positions=list(lines),
    )


def _cost_basis(row):
    for key in ("cost", "vwap"):
        px = row.get(key)
        if isinstance(px, (int, float)) and float(px) > 0:
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


def _tender_signature(tender):
    if not isinstance(tender, dict):
        return None
    return (
        tender.get("ticker"),
        tender.get("action"),
        tender.get("quantity"),
        tender.get("price"),
        tender.get("expires"),
        tender.get("status"),
        bool(tender.get("is_fixed_bid")),
    )


def _weighted_exec_price(levels, target_qty):
    if not levels:
        return None, 0.0, 0.0

    requested = max(1.0, float(target_qty))
    remaining = requested
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
        return None, 0.0, 0.0
    fill_ratio = min(1.0, filled / requested)
    return notional / filled, filled, fill_ratio


def _decision_book_levels(ob, side, target_qty):
    """
    Use a filtered decision slice of the book (not full depth) for tender economics.
    Side is hedge side: "bids" for SELL hedge, "asks" for BUY hedge.
    """
    if side not in {"bids", "asks"}:
        return []
    full = ob.get(f"{side}_all") or ob.get(side, [])
    if not full:
        return []

    max_levels = max(1, BOOK_DECISION_MAX_LEVELS)
    min_levels = max(1, min(BOOK_DECISION_MIN_LEVELS, max_levels))
    qty_target = max(1.0, float(target_qty))

    best_px = float(full[0].get("price", 0.0)) if full else 0.0
    bps_window = max(0.0, BOOK_DECISION_MAX_BPS)
    band = max(0.01, best_px * (bps_window / 10000.0))
    if side == "bids":
        px_limit = best_px - band
    else:
        px_limit = best_px + band

    out = []
    cum_qty = 0.0
    for lv in full:
        px = lv.get("price")
        qty = lv.get("quantity")
        if not isinstance(px, (int, float)) or not isinstance(qty, (int, float)) or qty <= 0:
            continue
        px_f = float(px)
        if bps_window > 0:
            if side == "bids" and px_f < px_limit:
                if len(out) >= min_levels:
                    break
            if side == "asks" and px_f > px_limit:
                if len(out) >= min_levels:
                    break

        out.append({"ticker": lv.get("ticker"), "price": px_f, "quantity": float(qty)})
        cum_qty += float(qty)
        if len(out) >= max_levels:
            break
        if cum_qty >= qty_target and len(out) >= min_levels:
            break

    if not out:
        return full[:max_levels]
    return out


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


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


def _stdev(values):
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(max(0.0, var))


def _realized_volatility(prices, lookback):
    if len(prices) < 3:
        return None
    rets = []
    start = max(1, len(prices) - lookback)
    for i in range(start, len(prices)):
        prev = max(0.01, float(prices[i - 1]))
        curr = float(prices[i])
        rets.append((curr - prev) / prev)
    if len(rets) < 2:
        return None
    return _stdev(rets)


def _spread_bps(ob):
    spread = ob.get("spread")
    if not isinstance(spread, (int, float)) or spread <= 0:
        return 0.0
    mid = ob.get("mid")
    if isinstance(mid, (int, float)) and mid > 0:
        denom = float(mid)
    else:
        best_bid = ob.get("best_bid")
        best_ask = ob.get("best_ask")
        if isinstance(best_bid, (int, float)) and isinstance(best_ask, (int, float)) and (best_bid + best_ask) > 0:
            denom = (float(best_bid) + float(best_ask)) / 2.0
        else:
            denom = 1.0
    return max(0.0, (float(spread) / max(0.01, denom)) * 10000.0)


def _spread_scaled_chunk(base_qty, spread_bps, spread_ref_bps, min_qty=1.0, power=1.0):
    base = max(1.0, float(base_qty))
    ref = max(0.5, float(spread_ref_bps))
    spread = max(0.0, float(spread_bps))
    p = max(0.2, float(power))
    if spread <= ref:
        scale = 1.0
    else:
        scale = (ref / spread) ** p
    chunk = base * max(0.01, min(1.0, scale))
    return max(float(min_qty), min(base, chunk))


def _dynamic_hedge_ratio(base_ratio, ob, regime=None):
    base = max(0.0, min(1.0, float(base_ratio)))
    spread_bps = _spread_bps(ob)
    spread_score = _clip01(spread_bps / max(0.5, DYN_HEDGE_SPREAD_BPS_REF))

    realized_vol = None
    state = "NEUTRAL"
    conf = 0.0
    if isinstance(regime, dict):
        rv = regime.get("realized_vol")
        if isinstance(rv, (int, float)) and rv >= 0:
            realized_vol = float(rv)
        state = str(regime.get("state", "NEUTRAL")).upper()
        conf = _clip01(regime.get("confidence", 0.0))

    vol_score = _clip01((realized_vol or 0.0) / max(0.0001, DYN_HEDGE_VOL_REF))

    ratio = base
    if DYN_HEDGE_RATIO_ENABLED:
        ratio += (DYN_HEDGE_VOL_WEIGHT * vol_score) + (DYN_HEDGE_SPREAD_WEIGHT * spread_score)
        if state == "ADVERSE":
            ratio += DYN_HEDGE_ADVERSE_BONUS * max(0.3, conf)
        elif state == "FAVORABLE":
            ratio -= DYN_HEDGE_FAVORABLE_DISCOUNT * conf
        ratio = max(DYN_HEDGE_RATIO_MIN, min(DYN_HEDGE_RATIO_MAX, ratio))
    else:
        ratio = max(DYN_HEDGE_RATIO_MIN, min(DYN_HEDGE_RATIO_MAX, ratio))

    return ratio, {
        "base_ratio": base,
        "final_ratio": ratio,
        "spread_bps": spread_bps,
        "spread_score": spread_score,
        "realized_vol": realized_vol,
        "vol_score": vol_score,
        "state": state,
        "confidence": conf,
    }


def _flatten_regime(session, ticker, unwind_action, ob, cache=None):
    now = time.monotonic()
    cache_key = f"{ticker}|{unwind_action}"
    if cache is not None:
        cached = cache.get(cache_key)
        if cached and (now - cached.get("ts", 0.0)) <= REGIME_CACHE_TTL:
            return cached["regime"]

    prices = _get_tas_prices(session, ticker, MOM_TAS_LIMIT)
    ema_fast = _ema(prices, MOM_EMA_FAST) if prices else None
    ema_slow = _ema(prices, MOM_EMA_SLOW) if prices else None
    rsi = _rsi(prices, MOM_RSI_PERIOD) if prices else None
    imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)
    realized_vol = _realized_volatility(prices, VOL_LOOKBACK) if prices else None

    score = 0.0
    trend_bias = "FLAT"
    momentum_bias = "NEUTRAL"

    if ema_fast is not None and ema_slow is not None:
        mid = max(0.01, (abs(ema_fast) + abs(ema_slow)) / 2.0)
        gap_bps = ((ema_fast - ema_slow) / mid) * 10000.0
        if gap_bps >= TREND_MIN_GAP_BPS:
            trend_bias = "UP"
        elif gap_bps <= -TREND_MIN_GAP_BPS:
            trend_bias = "DOWN"
        else:
            trend_bias = "FLAT"

        if unwind_action == "SELL":
            if trend_bias == "UP":
                score += 1.4
            elif trend_bias == "DOWN":
                score -= 1.4
        else:
            if trend_bias == "DOWN":
                score += 1.4
            elif trend_bias == "UP":
                score -= 1.4

    if rsi is not None:
        if rsi >= RSI_HIGH:
            momentum_bias = "UP"
        elif rsi <= RSI_LOW:
            momentum_bias = "DOWN"
        else:
            momentum_bias = "NEUTRAL"

        if unwind_action == "SELL":
            if momentum_bias == "UP":
                score += 0.9
            elif momentum_bias == "DOWN":
                score -= 0.9
        else:
            if momentum_bias == "DOWN":
                score += 0.9
            elif momentum_bias == "UP":
                score -= 0.9

    if unwind_action == "SELL":
        if imbalance >= MOM_IMBALANCE_THRESHOLD:
            score += 1.0
        elif imbalance <= -MOM_IMBALANCE_THRESHOLD:
            score -= 1.0
    else:
        if imbalance <= -MOM_IMBALANCE_THRESHOLD:
            score += 1.0
        elif imbalance >= MOM_IMBALANCE_THRESHOLD:
            score -= 1.0

    if realized_vol is not None:
        if realized_vol >= VOL_SHOCK:
            score -= 0.6
        elif realized_vol >= VOL_HIGH:
            score -= 0.3

    if score >= REGIME_SCORE_FAVORABLE:
        state = "FAVORABLE"
    elif score <= REGIME_SCORE_ADVERSE:
        state = "ADVERSE"
    else:
        state = "NEUTRAL"

    confidence = _clip01(abs(score) / max(0.1, abs(REGIME_SCORE_FAVORABLE)))

    regime = {
        "state": state,
        "score": score,
        "confidence": confidence,
        "trend_bias": trend_bias,
        "momentum_bias": momentum_bias,
        "realized_vol": realized_vol,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": rsi,
        "imbalance": imbalance,
    }
    if cache is not None:
        cache[cache_key] = {"ts": now, "regime": regime}
    return regime


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


def _unresolved_tender_ids_for_base(session, ticker, exclude_tids=None):
    base = _base_symbol(ticker)
    exclude = {str(x) for x in (exclude_tids or set())}
    out = []
    try:
        tenders = get_tenders(session)
    except Exception as exc:
        print(f"[WARN] unresolved tender check failed for {ticker}: {exc}")
        return out

    for t in tenders:
        tk = t.get("ticker")
        if not tk or _base_symbol(tk) != base:
            continue
        if not _is_unresolved_tender(t):
            continue
        tid = t.get("tender_id")
        if tid is None:
            continue
        if str(tid) in exclude:
            continue
        out.append(tid)
    return out


def _decline_unresolved_tenders_for_bases(session, bases, exclude_tids=None):
    base_set = {_base_symbol(x) for x in (bases or set()) if x}
    if not base_set:
        return 0, set()
    exclude = {str(x) for x in (exclude_tids or set())}
    declined = 0

    try:
        tenders_now = get_tenders(session)
    except Exception as exc:
        _record_error("_decline_unresolved_tenders_for_bases", exc, bases=list(base_set), stage="load_before")
        return 0, set(base_set)

    for t in tenders_now:
        if not isinstance(t, dict):
            continue
        tk = t.get("ticker")
        if not tk or _base_symbol(tk) not in base_set:
            continue
        if not _is_unresolved_tender(t):
            continue
        tid = t.get("tender_id")
        if tid is not None and str(tid) in exclude:
            continue
        if decline_tender(session, t):
            declined += 1

    still_open = set()
    try:
        tenders_after = get_tenders(session)
    except Exception as exc:
        _record_error("_decline_unresolved_tenders_for_bases", exc, bases=list(base_set), stage="load_after")
        return declined, set(base_set)

    for t in tenders_after:
        if not isinstance(t, dict):
            continue
        tk = t.get("ticker")
        if not tk or _base_symbol(tk) not in base_set:
            continue
        if _is_unresolved_tender(t):
            still_open.add(_base_symbol(tk))
    return declined, still_open


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
        spread_bps = _spread_bps(ob)
        spread_chunk_cap = _spread_scaled_chunk(
            TAKE_PROFIT_CHUNK_QTY,
            spread_bps,
            TAKE_PROFIT_SPREAD_BPS_REF,
            min_qty=TAKE_PROFIT_MIN_CHUNK_QTY,
            power=TAKE_PROFIT_SPREAD_POWER,
        )
        if pos < 0:
            if not ob["asks"]:
                continue
            top_level_qty = float(ob["asks"][0].get("quantity", 0.0))
            exec_px = ob["asks"][0]["price"]
            edge = cost - exec_px
            action = "BUY"
        else:
            if not ob["bids"]:
                continue
            top_level_qty = float(ob["bids"][0].get("quantity", 0.0))
            exec_px = ob["bids"][0]["price"]
            edge = exec_px - cost
            action = "SELL"

        if edge < TAKE_PROFIT_PER_SHARE:
            continue

        if top_level_qty <= 0:
            continue
        top_level_cap = max(1.0, top_level_qty * TAKE_PROFIT_TOP_LEVEL_PARTICIPATION)
        cover_qty = min(abs(pos), TAKE_PROFIT_CHUNK_QTY, spread_chunk_cap, top_level_cap)
        cover_qty = math.floor(cover_qty)
        if cover_qty < 1:
            continue

        submit_market_order(session, ticker, cover_qty, action)
        last_tp_by_ticker[ticker] = now
        _bump_counter("take_profit_orders", 1)
        _record_event(
            "take_profit",
            ticker=ticker,
            action=action,
            quantity=int(cover_qty),
            edge=float(edge),
            cost=float(cost),
            exec_price=float(exec_px),
            spread_bps=float(spread_bps),
        )
        print(
            f"[TAKE_PROFIT] {ticker} {action} {cover_qty:.0f} "
            f"edge={edge:.3f} cost={cost:.2f} exec={exec_px:.2f} "
            f"spread_bps={spread_bps:.2f} chunk_cap={spread_chunk_cap:.0f} top_cap={top_level_cap:.0f}"
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
        _bump_counter("stop_loss_orders", 1)
        _record_event(
            "stop_loss",
            ticker=ticker,
            action=action,
            quantity=int(cut_qty),
            loss=float(loss),
            cost=float(cost),
            exec_price=float(exec_px),
        )
        print(
            f"[STOP_LOSS] {ticker} {action} {cut_qty:.0f} "
            f"loss={loss:.3f} cost={cost:.2f} exec={exec_px:.2f}"
        )


def unwind_inventory(session, ticker, inventory, excluded_tender_ids=None):
    if abs(inventory) < 1:
        return

    remaining = abs(inventory)
    unwind_action = "BUY" if inventory < 0 else "SELL"
    side_key = "asks" if unwind_action == "BUY" else "bids"
    pos_before = get_inventory_total(session, ticker)
    tickets = 0
    regime = {"state": "NEUTRAL", "imbalance": 0.0, "ema_fast": None, "ema_slow": None, "rsi": None}
    last_regime_refresh = 0.0
    regime_cache = {}
    fallback_slices = 0

    while remaining > 0 and tickets < HEDGE_MAX_TICKETS:
        blocked_ids = _unresolved_tender_ids_for_base(session, ticker, exclude_tids=excluded_tender_ids)
        if blocked_ids:
            _bump_counter("hedge_blocks", 1)
            _record_hedge_log(
                "blocked",
                ticker=ticker,
                action=unwind_action,
                remaining=float(remaining),
                unresolved_tenders=list(blocked_ids),
            )
            print(
                f"[HEDGE BLOCK] ticker={ticker} unresolved_tenders={blocked_ids} "
                f"rem={remaining:.0f}; stopping hedge."
            )
            break

        ob = get_order_book_agg(session, ticker)
        levels = ob.get(side_key, [])
        if not levels:
            break

        now = time.monotonic()
        if HEDGE_MOMENTUM_AWARE and (
            tickets == 0 or (now - last_regime_refresh) >= HEDGE_REGIME_REFRESH_SECS
        ):
            regime = _flatten_regime(session, ticker, unwind_action, ob, cache=regime_cache)
            last_regime_refresh = now

        state = regime["state"] if HEDGE_MOMENTUM_AWARE else "NEUTRAL"
        confidence = float(regime.get("confidence", 0.0)) if HEDGE_MOMENTUM_AWARE else 0.0
        base_ticket = min(remaining, MAX_ORDER_SIZE, max(HEDGE_MIN_TICKET_QTY, remaining * 0.5))
        ticket_qty = _state_scaled_ticket(
            remaining,
            base_ticket,
            HEDGE_FAVORABLE_MULT,
            HEDGE_ADVERSE_MULT,
            state,
            confidence=confidence,
        )

        top = levels[0]
        q = min(remaining, ticket_qty, float(top.get("quantity", 0.0)), MAX_ORDER_SIZE)
        if q < 1:
            q = min(remaining, ticket_qty, MAX_ORDER_SIZE)
        if q < 1:
            break

        base_offset = min(HEDGE_MARKETABLE_OFFSET_MAX, max(0.01, HEDGE_MARKETABLE_OFFSET))
        if unwind_action == "BUY":
            px = max(0.01, float(top["price"]) + base_offset)
        else:
            px = max(0.01, float(top["price"]) - base_offset)

        filled = 0.0
        submit_limit_order(session, top["ticker"], q, px, unwind_action)
        time.sleep(ORDER_DELAY)

        pos_after = get_inventory_total(session, ticker)
        if unwind_action == "BUY":
            filled = max(0.0, pos_after - pos_before)
        else:
            filled = max(0.0, pos_before - pos_after)
        pos_before = pos_after

        # If marketable limit under-fills, use smaller marketable-limit slices instead of a large market order.
        if filled <= 0.5:
            fallback_remaining = max(0.0, min(remaining, q) - filled)
            while fallback_remaining > 0 and fallback_slices < HEDGE_MAX_FALLBACK_SLICES:
                ob_fb = get_order_book_agg(session, ticker)
                levels_fb = ob_fb.get(side_key, [])
                if not levels_fb:
                    break

                top_fb = levels_fb[0]
                fb_q = min(
                    fallback_remaining,
                    HEDGE_FALLBACK_CHUNK_QTY,
                    float(top_fb.get("quantity", 0.0)),
                    MAX_ORDER_SIZE,
                )
                if fb_q < 1:
                    fb_q = min(fallback_remaining, HEDGE_FALLBACK_CHUNK_QTY, MAX_ORDER_SIZE)
                if fb_q < 1:
                    break

                dynamic_offset = min(HEDGE_MARKETABLE_OFFSET_MAX, base_offset * (1.0 + (0.2 * fallback_slices)))
                if unwind_action == "BUY":
                    fb_px = max(0.01, float(top_fb["price"]) + dynamic_offset)
                else:
                    fb_px = max(0.01, float(top_fb["price"]) - dynamic_offset)

                submit_limit_order(session, top_fb["ticker"], fb_q, fb_px, unwind_action)
                time.sleep(ORDER_DELAY)

                pos_after = get_inventory_total(session, ticker)
                if unwind_action == "BUY":
                    fb_filled = max(0.0, pos_after - pos_before)
                else:
                    fb_filled = max(0.0, pos_before - pos_after)
                pos_before = pos_after

                # Optional emergency fallback (off by default).
                if fb_filled <= 0.5 and HEDGE_ALLOW_MARKET_FALLBACK:
                    market_q = min(fallback_remaining, fb_q, MAX_ORDER_SIZE)
                    submit_market_order(session, ticker, market_q, unwind_action)
                    pos_after = get_inventory_total(session, ticker)
                    if unwind_action == "BUY":
                        fb_filled = max(0.0, pos_after - pos_before)
                    else:
                        fb_filled = max(0.0, pos_before - pos_after)
                    pos_before = pos_after

                filled += fb_filled
                fallback_remaining = max(0.0, fallback_remaining - fb_filled)
                fallback_slices += 1

                if fb_filled <= 0.5 and not HEDGE_ALLOW_MARKET_FALLBACK:
                    break

        remaining = max(0.0, remaining - filled)
        tickets += 1
        _bump_counter("hedge_tickets", 1)
        _record_hedge_log(
            "ticket",
            ticker=ticker,
            action=unwind_action,
            state=state,
            confidence=float(confidence),
            requested=float(q),
            filled=float(filled),
            remaining=float(remaining),
            fallback_slices=int(fallback_slices),
        )
        print(
            f"[HEDGE] ticker={ticker} action={unwind_action} state={state} "
            f"conf={confidence:.2f} ticket={q:.0f} filled={filled:.0f} rem={remaining:.0f}"
        )

    if remaining > 0:
        blocked_ids = _unresolved_tender_ids_for_base(session, ticker, exclude_tids=excluded_tender_ids)
        if blocked_ids:
            _bump_counter("hedge_blocks", 1)
            _record_hedge_log(
                "blocked_final_fallback",
                ticker=ticker,
                action=unwind_action,
                remaining=float(remaining),
                unresolved_tenders=list(blocked_ids),
            )
            print(
                f"[HEDGE BLOCK] ticker={ticker} unresolved_tenders={blocked_ids} "
                f"rem={remaining:.0f}; skipping final fallback."
            )
            return
        if HEDGE_ALLOW_MARKET_FALLBACK:
            while remaining > 0:
                market_q = min(remaining, HEDGE_FALLBACK_CHUNK_QTY, MAX_ORDER_SIZE)
                if market_q < 1:
                    break
                submit_market_order(session, ticker, market_q, unwind_action)
                pos_after = get_inventory_total(session, ticker)
                if unwind_action == "BUY":
                    mkt_filled = max(0.0, pos_after - pos_before)
                else:
                    mkt_filled = max(0.0, pos_before - pos_after)
                pos_before = pos_after
                _bump_counter("hedge_market_fallback_tickets", 1)
                _record_hedge_log(
                    "market_fallback",
                    ticker=ticker,
                    action=unwind_action,
                    requested=float(market_q),
                    filled=float(mkt_filled),
                    remaining_before=float(remaining),
                )
                if mkt_filled <= 0.5:
                    _record_hedge_log(
                        "market_fallback_no_fill",
                        ticker=ticker,
                        action=unwind_action,
                        requested=float(market_q),
                        remaining=float(remaining),
                    )
                    print(f"[HEDGE WARN] market fallback no fill on {ticker}; rem={remaining:.0f}")
                    break
                remaining = max(0.0, remaining - mkt_filled)
        else:
            _record_hedge_log(
                "remaining_no_market_fallback",
                ticker=ticker,
                action=unwind_action,
                remaining=float(remaining),
            )
            print(f"[HEDGE WARN] remaining {remaining:.0f} on {ticker}; no market fallback allowed.")


def _effective_edge_and_gate(attempt_idx, regime=None):
    eff_edge = MIN_EDGE
    if AGGRESSIVE_MODE:
        eff_edge = max(MIN_EDGE * EDGE_FLOOR_RATIO, MIN_EDGE - (EDGE_DECAY_PER_ATTEMPT * attempt_idx))
    trend_blocked = False
    if regime:
        state = regime.get("state", "NEUTRAL")
        conf = float(regime.get("confidence", 0.0))
        if state == "FAVORABLE":
            eff_edge *= TREND_EDGE_FAVORABLE_MULT
        elif state == "ADVERSE":
            eff_edge *= TREND_EDGE_ADVERSE_MULT
            if TREND_STRICT_ACCEPT and conf >= TREND_STRICT_MIN_CONF:
                trend_blocked = True
    return eff_edge, trend_blocked


def _action_edge_ok(my_action, tender_price, ob, tender_qty, attempt_idx, regime=None):
    """
    Decide fixed-price tender acceptance from executable economics on full tender quantity.
    BUY tender -> hedge by selling into bids.
    SELL tender -> hedge by buying from asks.
    """
    eff_edge, trend_blocked = _effective_edge_and_gate(attempt_idx, regime=regime)
    decision_qty = max(1.0, float(tender_qty))
    imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)

    if my_action == "BUY":
        edge_top = None
        if ob.get("best_bid") is not None:
            edge_top = float(ob["best_bid"]) - tender_price

        edge_exec = None
        decision_levels = _decision_book_levels(ob, "bids", decision_qty)
        exec_px, _, fill_ratio = _weighted_exec_price(decision_levels, decision_qty)
        if exec_px is not None and fill_ratio >= BOOK_MIN_FILL_RATIO:
            edge_exec = exec_px - tender_price

        edge = edge_exec if edge_exec is not None else edge_top
        ok = (
            edge is not None
            and edge >= eff_edge
            and not trend_blocked
            and fill_ratio >= BOOK_MIN_FILL_RATIO
        )
        return ok, edge, eff_edge, imbalance, edge_top, edge_exec, fill_ratio

    if my_action == "SELL":
        edge_top = None
        if ob.get("best_ask") is not None:
            edge_top = tender_price - float(ob["best_ask"])

        edge_exec = None
        decision_levels = _decision_book_levels(ob, "asks", decision_qty)
        exec_px, _, fill_ratio = _weighted_exec_price(decision_levels, decision_qty)
        if exec_px is not None and fill_ratio >= BOOK_MIN_FILL_RATIO:
            edge_exec = tender_price - exec_px

        edge = edge_exec if edge_exec is not None else edge_top
        ok = (
            edge is not None
            and edge >= eff_edge
            and not trend_blocked
            and fill_ratio >= BOOK_MIN_FILL_RATIO
        )
        return ok, edge, eff_edge, imbalance, edge_top, edge_exec, fill_ratio

    return False, None, eff_edge, imbalance, None, None, 0.0


def _build_non_fixed_submit_price(my_action, ob, tender_qty, attempt_idx, regime=None):
    """
    Build a profitable auction/winner-take-all bid from executable hedge VWAP ± edge.
    """
    eff_edge, trend_blocked = _effective_edge_and_gate(attempt_idx, regime=regime)
    imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)
    if trend_blocked:
        return False, None, None, eff_edge, imbalance, 0.0

    decision_qty = max(1.0, float(tender_qty))

    if my_action == "BUY":
        decision_levels = _decision_book_levels(ob, "bids", decision_qty)
        exec_px, _, fill_ratio = _weighted_exec_price(decision_levels, decision_qty)
        if exec_px is None or fill_ratio < BOOK_MIN_FILL_RATIO:
            return False, None, exec_px, eff_edge, imbalance, fill_ratio
        submit_px = _round_to_tick(exec_px - eff_edge, AUCTION_TICK, mode="down")
        return True, submit_px, exec_px, eff_edge, imbalance, fill_ratio

    if my_action == "SELL":
        decision_levels = _decision_book_levels(ob, "asks", decision_qty)
        exec_px, _, fill_ratio = _weighted_exec_price(decision_levels, decision_qty)
        if exec_px is None or fill_ratio < BOOK_MIN_FILL_RATIO:
            return False, None, exec_px, eff_edge, imbalance, fill_ratio
        submit_px = _round_to_tick(exec_px + eff_edge, AUCTION_TICK, mode="up")
        return True, submit_px, exec_px, eff_edge, imbalance, fill_ratio

    return False, None, None, eff_edge, imbalance, 0.0


def evaluate_tender(session, tender):
    ticker = tender.get("ticker")
    tid = tender.get("tender_id")
    my_action = _infer_my_action(tender) or str(tender.get("action", "")).upper()
    is_fixed = bool(tender.get("is_fixed_bid"))
    tender_qty = _tender_quantity(tender)

    if not ticker or tid is None:
        _record_tender_log("invalid_tender", tender=tender)
        return
    _record_tender_log(
        "evaluate_start",
        tender_id=tid,
        ticker=ticker,
        action=my_action,
        is_fixed=is_fixed,
        quantity=tender_qty,
        tender_price=tender.get("price"),
        expires=tender.get("expires"),
    )
    if FIXED_ONLY and not is_fixed:
        print(f"Tender {tid} non-fixed while FIXED_ONLY=1; declining.")
        decline_tender(session, tender)
        return
    if tender_qty <= 0:
        decline_tender(session, tender)
        return

    attempts = MAX_ATTEMPTS
    latest_tick = None

    # Bound attempts by remaining tender window.
    expires = tender.get("expires")
    try:
        tick_now, _, _ = get_tick(session)
        latest_tick = tick_now
        if isinstance(expires, (int, float)):
            ticks_left = max(0, int(expires) - int(tick_now))
            attempts = max(1, min(attempts, ticks_left - 1))
    except Exception:
        pass

    monitor_interval = max(0.005, TENDER_MONITOR_INTERVAL)
    monitor_budget_secs = max(monitor_interval, float(attempts) * max(EVAL_DELAY, monitor_interval))
    max_polls = max(1, min(TENDER_MONITOR_MAX_POLLS, int(monitor_budget_secs / monitor_interval) + 2))
    monitor_deadline = time.monotonic() + monitor_budget_secs

    accepted = False
    regime_cache = {}
    last_fixed_price = tender.get("price")
    poll_idx = 0
    last_tick_refresh = 0.0

    while poll_idx < max_polls and time.monotonic() <= monitor_deadline:
        now = time.monotonic()
        if latest_tick is None or (now - last_tick_refresh) >= TENDER_TICK_REFRESH_SECS:
            try:
                latest_tick, _, _ = get_tick(session)
                last_tick_refresh = now
            except Exception:
                pass

        live = get_tender_map(session).get(tid)
        if live is None:
            print(f"Tender {tid} unavailable.")
            _record_tender_log("unavailable_during_eval", tender_id=tid, ticker=ticker, poll_idx=poll_idx)
            return
        status = str(live.get("status", "")).upper()
        if status and status not in {"OFFERED", "OPEN", "ACTIVE"}:
            print(f"Tender {tid} status={status}.")
            _record_tender_log("status_closed", tender_id=tid, ticker=ticker, status=status, poll_idx=poll_idx)
            return
        is_fixed_live = bool(live.get("is_fixed_bid"))
        if FIXED_ONLY and not is_fixed_live:
            decline_tender(session, live)
            return

        live_price = live.get("price")
        if isinstance(live_price, (int, float)):
            last_fixed_price = float(live_price)
        live_qty = _tender_quantity(live)
        if live_qty > 0:
            tender_qty = live_qty
        live_action = _infer_my_action(live) or my_action
        hedge_action = "SELL" if live_action == "BUY" else "BUY"

        ob = get_order_book_agg(session, ticker)
        regime = _flatten_regime(session, ticker, hedge_action, ob, cache=regime_cache)

        should_submit = False
        submit_price = None
        edge = None
        edge_top = None
        edge_exec = None
        eff_edge = MIN_EDGE
        imbalance = _order_book_imbalance(ob, MOM_IMBALANCE_LEVELS)
        fill_ratio = 0.0
        mode = "fixed"

        if is_fixed_live:
            if not isinstance(last_fixed_price, (int, float)):
                print(f"HOLD tender {tid}: fixed price missing")
                _record_tender_log("fixed_price_missing", tender_id=tid, ticker=ticker, poll_idx=poll_idx)
                poll_idx += 1
                time.sleep(monitor_interval)
                continue
            edge_ok, edge, eff_edge, imbalance, edge_top, edge_exec, fill_ratio = _action_edge_ok(
                live_action,
                float(last_fixed_price),
                ob,
                tender_qty,
                poll_idx,
                regime=regime,
            )
            should_submit = edge_ok
        else:
            mode = "auction"
            price_ok, submit_price, hedge_exec_px, eff_edge, imbalance, fill_ratio = _build_non_fixed_submit_price(
                live_action,
                ob,
                tender_qty,
                poll_idx,
                regime=regime,
            )
            if submit_price is not None and hedge_exec_px is not None:
                edge = abs(hedge_exec_px - submit_price)
                edge_exec = edge
            should_submit = price_ok

        if should_submit:
            ok_limits, lim = _pretrade_limit_gate(session, ticker, live_action, tender_qty)
            if not ok_limits:
                _bump_counter("tenders_limit_blocked", 1)
                _record_tender_log(
                    "limit_blocked",
                    tender_id=tid,
                    ticker=ticker,
                    side=live_action,
                    quantity=float(tender_qty),
                    gross_after=float(lim["gross_after"]),
                    gross_limit=float(lim["gross_limit"]),
                    net_after=float(lim["net_after"]),
                    net_limit=float(lim["net_limit"]),
                )
                print(
                    f"DECLINE tender {tid}: limits gate "
                    f"gross {lim['gross_after']:.0f}/{lim['gross_limit']:.0f} "
                    f"net {abs(lim['net_after']):.0f}/{lim['net_limit']:.0f}"
                )
                decline_tender(session, live)
                return

            pre = get_inventory_total(session, ticker)
            accepted = accept_tender(session, live, submit_price=submit_price)
            if not accepted:
                print(f"Tender {tid} submit failed on poll {poll_idx + 1}/{max_polls}; retrying while still open.")
                poll_idx += 1
                time.sleep(min(monitor_interval, TENDER_MONITOR_FAST_INTERVAL))
                continue

            time.sleep(AFTER_ACCEPT_DELAY)
            post = get_inventory_total(session, ticker)
            delta = post - pre
            # Position-delta hedging (core anti-fine fix):
            # only unwind the quantity actually added by tender fill.
            if abs(delta) > 0:
                hedge_unwind_action = "BUY" if delta < 0 else "SELL"
                ob_post = get_order_book_agg(session, ticker)
                regime_post = _flatten_regime(session, ticker, hedge_unwind_action, ob_post, cache=regime_cache)
                hedge_ratio_now, hedge_meta = _dynamic_hedge_ratio(HEDGE_RATIO, ob_post, regime=regime_post)
                hedge_delta = delta * hedge_ratio_now
                retained = delta - hedge_delta
                _record_tender_log(
                    "accepted_fill_delta",
                    tender_id=tid,
                    ticker=ticker,
                    side=live_action,
                    quantity=float(tender_qty),
                    mode=mode,
                    submit_price=float(submit_price) if isinstance(submit_price, (int, float)) else None,
                    delta=float(delta),
                    hedge_ratio=float(hedge_ratio_now),
                    hedge_delta=float(hedge_delta),
                    retained=float(retained),
                    regime=regime_post.get("state"),
                )
                print(
                    f"[HEDGE_RATIO] {ticker} base={HEDGE_RATIO:.2f} dyn={hedge_ratio_now:.2f} "
                    f"spread_bps={hedge_meta['spread_bps']:.2f} vol={hedge_meta['realized_vol']} "
                    f"state={hedge_meta['state']} conf={hedge_meta['confidence']:.2f}"
                )
                unwind_inventory(session, ticker, hedge_delta, excluded_tender_ids={tid})
                if abs(retained) >= 1:
                    print(
                        f"Holding risk on {ticker}: retained={retained:.0f} "
                        f"(hedge_ratio={hedge_ratio_now:.2f})"
                    )
            else:
                _record_tender_log(
                    "accepted_no_fill",
                    tender_id=tid,
                    ticker=ticker,
                    side=live_action,
                    quantity=float(tender_qty),
                    mode=mode,
                    submit_price=float(submit_price) if isinstance(submit_price, (int, float)) else None,
                )
                print(f"Tender {tid} accepted but no fill delta (likely lost auction or reserve miss).")
            break

        ticks_left_live = None
        if isinstance(expires, (int, float)) and isinstance(latest_tick, (int, float)):
            ticks_left_live = int(expires) - int(latest_tick)

        should_log = (
            poll_idx == 0
            or (poll_idx % TENDER_MONITOR_LOG_EVERY == 0)
            or (edge is not None and edge >= (0.92 * eff_edge))
            or (ticks_left_live is not None and ticks_left_live <= 2)
        )
        if should_log:
            _record_tender_log(
                "poll_snapshot",
                tender_id=tid,
                ticker=ticker,
                poll_idx=int(poll_idx),
                max_polls=int(max_polls),
                mode=mode,
                side=live_action,
                tender_qty=float(tender_qty),
                tender_price=float(last_fixed_price) if isinstance(last_fixed_price, (int, float)) else None,
                submit_price=float(submit_price) if isinstance(submit_price, (int, float)) else None,
                edge=float(edge) if isinstance(edge, (int, float)) else None,
                required_edge=float(eff_edge),
                edge_top=float(edge_top) if isinstance(edge_top, (int, float)) else None,
                edge_exec=float(edge_exec) if isinstance(edge_exec, (int, float)) else None,
                fill_ratio=float(fill_ratio),
                imbalance=float(imbalance),
                regime=regime.get("state"),
                confidence=float(regime.get("confidence", 0.0)),
                trend_bias=regime.get("trend_bias"),
                ticks_left=ticks_left_live,
            )
            print(
                f"Evaluating tender {tid} ({poll_idx + 1}/{max_polls}) "
                f"mode={mode} px={(f'{last_fixed_price:.2f}' if isinstance(last_fixed_price, (int, float)) else 'N/A')} "
                f"submit={(f'{submit_price:.2f}' if isinstance(submit_price, (int, float)) else 'N/A')} "
                f"qty={tender_qty:.0f} side={live_action} "
                f"edge={edge if edge is not None else 'N/A'} req={eff_edge:.3f} "
                f"top={edge_top if edge_top is not None else 'N/A'} "
                f"exec={edge_exec if edge_exec is not None else 'N/A'} "
                f"fill={fill_ratio:.2f} imb={imbalance:.2f} regime={regime['state']} "
                f"conf={regime.get('confidence', 0.0):.2f} "
                f"trend={regime.get('trend_bias', 'N/A')} "
                f"ticks_left={(ticks_left_live if ticks_left_live is not None else 'N/A')}"
            )

        poll_delay = monitor_interval
        if edge is not None and edge >= (0.80 * eff_edge):
            poll_delay = min(poll_delay, TENDER_MONITOR_EDGE_INTERVAL)
        if ticks_left_live is not None and ticks_left_live <= 2:
            poll_delay = min(poll_delay, TENDER_MONITOR_FAST_INTERVAL)
        time.sleep(max(0.001, poll_delay))
        poll_idx += 1

    if not accepted:
        _record_tender_log(
            "decline_after_eval",
            tender_id=tid,
            ticker=ticker,
            quantity=float(tender_qty),
            last_fixed_price=float(last_fixed_price) if isinstance(last_fixed_price, (int, float)) else None,
            polls=int(poll_idx),
            max_polls=int(max_polls),
        )
        decline_tender(session, tender)


def _live_positions(session):
    out = []
    for s in get_securities(session):
        ticker = s.get("ticker")
        if not ticker:
            continue
        pos = float(s.get("position", 0.0))
        if abs(pos) < 1:
            continue
        out.append({"ticker": ticker, "position": pos})
    return out


def close_positions(session, force_if_blocked=True):
    print("Closing all positions before trading ends.")
    _record_event("close_positions_start", force_if_blocked=bool(force_if_blocked))

    for pass_idx in range(1, 4):
        live = _live_positions(session)
        if not live:
            _record_event("close_positions_flat", pass_idx=pass_idx)
            return True

        bases = {_base_symbol(p["ticker"]) for p in live}
        declined, blocked_bases = _decline_unresolved_tenders_for_bases(session, bases)
        if declined > 0:
            _record_event(
                "close_positions_declined_unresolved",
                pass_idx=pass_idx,
                declined=int(declined),
            )

        for row in live:
            ticker = row["ticker"]
            pos = float(row["position"])
            base = _base_symbol(ticker)
            if base in blocked_bases and not force_if_blocked:
                _record_event(
                    "close_position_skipped_blocked",
                    ticker=ticker,
                    pass_idx=pass_idx,
                    quantity=float(abs(pos)),
                )
                continue
            action = "SELL" if pos > 0 else "BUY"
            _record_event(
                "close_position_order",
                ticker=ticker,
                action=action,
                quantity=float(abs(pos)),
                pass_idx=pass_idx,
                blocked_base=(base in blocked_bases),
            )
            submit_market_order(session, ticker, abs(pos), action)

        time.sleep(max(0.01, ORDER_DELAY))

    return len(_live_positions(session)) == 0


def _state_scaled_ticket(abs_qty, min_ticket_qty, favorable_mult, adverse_mult, state, confidence=1.0):
    ticket = max(1.0, min(float(abs_qty), float(min_ticket_qty)))
    conf = _clip01(confidence)
    scale = 1.0
    if state == "FAVORABLE":
        scale = 1.0 + ((float(favorable_mult) - 1.0) * conf)
    elif state == "ADVERSE":
        scale = 1.0 + ((float(adverse_mult) - 1.0) * conf)
    ticket *= scale
    return max(1.0, min(float(abs_qty), float(MAX_ORDER_SIZE), ticket))


def _ticket_qty_for_state(abs_pos, ticks_to_end, state, confidence=1.0):
    base = abs_pos / max(1.0, float(ticks_to_end))
    base = max(1.0, base)
    ticket = max(base, min(FLATTEN_MIN_TICKET_QTY, abs_pos))
    ticket = _state_scaled_ticket(
        abs_pos,
        ticket,
        FLATTEN_FAVORABLE_MULT,
        FLATTEN_ADVERSE_MULT,
        state,
        confidence=confidence,
    )
    return ticket


def _liquidity_aware_limit_sweep(session, ticker, action, target_qty, ticks_to_end, excluded_tender_ids=None):
    remaining = float(abs(target_qty))
    if remaining < 1 or action not in {"BUY", "SELL"}:
        return 0.0

    side_key = "asks" if action == "BUY" else "bids"
    total_filled = 0.0
    slices = 0

    while remaining >= 1 and slices < HEDGE_MAX_FALLBACK_SLICES:
        blocked_ids = _unresolved_tender_ids_for_base(session, ticker, exclude_tids=excluded_tender_ids)
        if blocked_ids:
            _record_hedge_log(
                "endgame_blocked",
                ticker=ticker,
                action=action,
                remaining=float(remaining),
                unresolved_tenders=list(blocked_ids),
            )
            print(f"[ENDGAME BLOCK] ticker={ticker} unresolved_tenders={blocked_ids} rem={remaining:.0f}")
            break

        ob = get_order_book_agg(session, ticker)
        levels = ob.get(side_key, [])
        if not levels:
            break

        top = levels[0]
        top_px = float(top.get("price", 0.0))
        if top_px <= 0:
            break
        top_qty = max(0.0, float(top.get("quantity", 0.0)))
        depth4_qty = sum(max(0.0, float(lv.get("quantity", 0.0))) for lv in levels[:4])

        if ticks_to_end > (ENDGAME_TICKS // 2):
            top_participation = 0.35
            depth_participation = 0.18
            min_clip = 250.0
        elif ticks_to_end > (FLATTEN_HARD_DEADLINE_TICKS + 1):
            top_participation = 0.55
            depth_participation = 0.30
            min_clip = 500.0
        else:
            top_participation = 0.90
            depth_participation = 0.70
            min_clip = 1000.0

        top_cap = max(min_clip, top_qty * top_participation)
        depth_cap = max(min_clip, depth4_qty * depth_participation)
        planned = min(remaining, MAX_ORDER_SIZE, max(min_clip, min(top_cap, depth_cap)))
        planned = math.floor(planned)
        if planned < 1:
            planned = min(int(MAX_ORDER_SIZE), int(math.floor(remaining)))
        if planned < 1:
            break

        base_offset = min(HEDGE_MARKETABLE_OFFSET_MAX, max(AUCTION_TICK, HEDGE_MARKETABLE_OFFSET))
        urgency_mult = 1.0
        if ticks_to_end <= FLATTEN_HARD_DEADLINE_TICKS + 1:
            urgency_mult = 1.6
        elif ticks_to_end <= (ENDGAME_TICKS // 2):
            urgency_mult = 1.25
        offset = min(HEDGE_MARKETABLE_OFFSET_MAX, base_offset * urgency_mult)

        if action == "BUY":
            px = _round_to_tick(top_px + offset, AUCTION_TICK, mode="up")
        else:
            px = _round_to_tick(max(0.01, top_px - offset), AUCTION_TICK, mode="down")

        pos_before = get_inventory_total(session, ticker)
        submit_limit_order(session, ticker, planned, px, action)
        time.sleep(max(0.01, ORDER_DELAY))
        pos_after = get_inventory_total(session, ticker)

        if action == "BUY":
            filled = max(0.0, pos_after - pos_before)
        else:
            filled = max(0.0, pos_before - pos_after)

        if filled <= 0.5 and ticks_to_end <= FLATTEN_HARD_DEADLINE_TICKS + 1:
            market_qty = min(remaining, float(planned), float(MAX_ORDER_SIZE))
            if market_qty >= 1:
                submit_market_order(session, ticker, market_qty, action)
                pos_after_mkt = get_inventory_total(session, ticker)
                if action == "BUY":
                    filled += max(0.0, pos_after_mkt - pos_after)
                else:
                    filled += max(0.0, pos_after - pos_after_mkt)

        if filled <= 0.5:
            break

        total_filled += filled
        remaining = max(0.0, remaining - filled)
        slices += 1

    return total_filled


def flatten_positions_ticketed(session, tick, tpp):
    ticks_to_end = max(0, int(tpp) - int(tick))
    regime_cache = {}
    passes = 4 if ticks_to_end > (FLATTEN_HARD_DEADLINE_TICKS + 1) else 2

    for pass_idx in range(1, passes + 1):
        live = _live_positions(session)
        if not live:
            return True

        bases = {_base_symbol(p["ticker"]) for p in live}
        declined, blocked_bases = _decline_unresolved_tenders_for_bases(session, bases)
        if declined > 0:
            _record_event(
                "endgame_declined_unresolved",
                tick=int(tick),
                ticks_to_end=int(ticks_to_end),
                pass_idx=pass_idx,
                declined=int(declined),
            )

        for row in live:
            ticker = row["ticker"]
            pos = float(row["position"])
            abs_pos = abs(pos)
            if abs_pos < 1:
                continue

            base = _base_symbol(ticker)
            if base in blocked_bases and ticks_to_end > FLATTEN_HARD_DEADLINE_TICKS:
                _record_hedge_log(
                    "endgame_blocked_skip",
                    ticker=ticker,
                    action=("SELL" if pos > 0 else "BUY"),
                    abs_position=float(abs_pos),
                    ticks_to_end=int(ticks_to_end),
                    pass_idx=pass_idx,
                )
                print(
                    f"[ENDGAME BLOCK] ticker={ticker} unresolved_tender_open "
                    f"pos={abs_pos:.0f} pass={pass_idx}/{passes}"
                )
                continue

            unwind_action = "SELL" if pos > 0 else "BUY"
            ob = get_order_book_agg(session, ticker)
            regime = _flatten_regime(session, ticker, unwind_action, ob, cache=regime_cache)

            safe_ticks = max(1, ticks_to_end - FLATTEN_HARD_DEADLINE_TICKS)
            pace_ticket = abs_pos / float(safe_ticks)
            ticket_qty = _ticket_qty_for_state(
                abs_pos,
                safe_ticks,
                regime["state"],
                confidence=float(regime.get("confidence", 0.0)),
            )
            ticket_qty = max(ticket_qty, pace_ticket)
            ticket_qty = min(abs_pos, MAX_ORDER_SIZE, ticket_qty)

            mode = "limit_sweep"
            filled = 0.0
            if ticks_to_end <= FLATTEN_HARD_DEADLINE_TICKS + 1:
                mode = "hard_market"
                ticket_qty = min(abs_pos, MAX_ORDER_SIZE)
                pos_before_market = get_inventory_total(session, ticker)
                submit_market_order(session, ticker, ticket_qty, unwind_action)
                pos_after_market = get_inventory_total(session, ticker)
                if unwind_action == "BUY":
                    filled = max(0.0, pos_after_market - pos_before_market)
                else:
                    filled = max(0.0, pos_before_market - pos_after_market)
            else:
                filled = _liquidity_aware_limit_sweep(
                    session,
                    ticker,
                    unwind_action,
                    ticket_qty,
                    ticks_to_end,
                )
                min_expected = max(1.0, float(ticket_qty) * 0.25)
                if filled < min_expected:
                    mode = "limit_then_market"
                    remaining_after_limit = max(0.0, abs_pos - filled)
                    fallback_qty = min(remaining_after_limit, MAX_ORDER_SIZE)
                    if fallback_qty >= 1:
                        pos_before_market = get_inventory_total(session, ticker)
                        submit_market_order(session, ticker, fallback_qty, unwind_action)
                        pos_after_market = get_inventory_total(session, ticker)
                        if unwind_action == "BUY":
                            market_filled = max(0.0, pos_after_market - pos_before_market)
                        else:
                            market_filled = max(0.0, pos_before_market - pos_after_market)
                        filled += market_filled
                remaining_after_ticket = max(0.0, abs_pos - filled)
                if remaining_after_ticket >= 1 and ticks_to_end <= ENDGAME_TICKS:
                    mode = "limit_then_market"
                    pos_before_market = get_inventory_total(session, ticker)
                    submit_market_order(session, ticker, remaining_after_ticket, unwind_action)
                    pos_after_market = get_inventory_total(session, ticker)
                    if unwind_action == "BUY":
                        market_filled = max(0.0, pos_after_market - pos_before_market)
                    else:
                        market_filled = max(0.0, pos_before_market - pos_after_market)
                    filled += market_filled

            _record_hedge_log(
                "endgame_ticket",
                ticker=ticker,
                action=unwind_action,
                ticket=float(ticket_qty),
                filled=float(filled),
                abs_position=float(abs_pos),
                state=regime["state"],
                confidence=float(regime.get("confidence", 0.0)),
                ticks_to_end=int(ticks_to_end),
                pass_idx=pass_idx,
                mode=mode,
                blocked_base=(base in blocked_bases),
            )
            ema_fast = regime["ema_fast"]
            ema_slow = regime["ema_slow"]
            rsi = regime["rsi"]
            ema_fast_s = f"{ema_fast:.3f}" if isinstance(ema_fast, (int, float)) else "N/A"
            ema_slow_s = f"{ema_slow:.3f}" if isinstance(ema_slow, (int, float)) else "N/A"
            rsi_s = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "N/A"
            print(
                f"[ENDGAME] ticker={ticker} action={unwind_action} "
                f"mode={mode} pass={pass_idx}/{passes} ticket={ticket_qty:.0f}/{abs_pos:.0f} "
                f"filled={filled:.0f} state={regime['state']} "
                f"ema_f={ema_fast_s} ema_s={ema_slow_s} rsi={rsi_s} "
                f"imb={regime['imbalance']:.2f} conf={regime.get('confidence', 0.0):.2f} "
                f"trend={regime.get('trend_bias', 'N/A')} ticks_to_end={ticks_to_end}"
            )

        time.sleep(max(0.01, ORDER_DELAY))

    return len(_live_positions(session)) == 0


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY env var.")

    processed = {}
    next_portfolio_print = 0.0
    last_tp_by_ticker = {}
    last_sl_by_ticker = {}
    last_active_tick = None
    heat_index = 1
    exit_reason = "shutdown_or_manual_stop"
    run_error = None
    _record_event("run_start", base_url=BASE_URL, report_prefix=REPORT_PREFIX)
    with requests.Session() as session:
        session.headers.update(HEADERS)
        try:
            while not SHUTDOWN:
                _bump_counter("loops", 1)
                tick, tpp, status = get_tick(session)
                if status != "ACTIVE":
                    time.sleep(1.0)
                    continue

                if (
                    isinstance(last_active_tick, int)
                    and int(tick) + HEAT_RESET_TICK_DROP < int(last_active_tick)
                ):
                    heat_index += 1
                    processed.clear()
                    _record_event(
                        "heat_reset_detected",
                        heat_index=int(heat_index),
                        prev_tick=int(last_active_tick),
                        curr_tick=int(tick),
                    )
                last_active_tick = int(tick)

                if tick >= tpp - ENDGAME_TICKS:
                    try:
                        flat_now = flatten_positions_ticketed(session, tick, tpp)
                    except Exception as exc:
                        print(f"Endgame ticketed flatten error: {exc}")
                        _record_error("flatten_positions_ticketed", exc, tick=tick, tpp=tpp)
                        flat_now = False

                    if tick >= tpp - FLATTEN_HARD_DEADLINE_TICKS:
                        close_positions(session)
                        _record_event("endgame_hard_deadline_flatten", tick=tick, tpp=tpp)
                        exit_reason = "endgame_hard_deadline_flatten"
                        break

                    if flat_now:
                        _record_event("endgame_ticketed_flatten", tick=tick, tpp=tpp)
                        exit_reason = "endgame_ticketed_flatten"
                        break

                    time.sleep(1.0)
                    continue

                try:
                    maybe_stop_loss_cut(session, last_sl_by_ticker)
                except Exception as exc:
                    print(f"Stop-loss error: {exc}")
                    _record_error("maybe_stop_loss_cut", exc, tick=tick, tpp=tpp)

                try:
                    maybe_take_profit_cover(session, last_tp_by_ticker)
                except Exception as exc:
                    print(f"Take-profit error: {exc}")
                    _record_error("maybe_take_profit_cover", exc, tick=tick, tpp=tpp)

                if PORTFOLIO_PRINT_INTERVAL > 0:
                    now = time.monotonic()
                    if now >= next_portfolio_print:
                        try:
                            log_portfolio(session, tick, tpp)
                        except Exception as exc:
                            print(f"Portfolio log error: {exc}")
                            _record_error("log_portfolio", exc, tick=tick, tpp=tpp)
                        next_portfolio_print = now + PORTFOLIO_PRINT_INTERVAL

                for tender in get_tenders(session):
                    tid = tender.get("tender_id")
                    if tid is None:
                        continue
                    sig = _tender_signature(tender)
                    if processed.get(tid) == sig:
                        continue
                    _bump_counter("tenders_seen", 1)
                    try:
                        evaluate_tender(session, tender)
                    except Exception as exc:
                        print(f"Tender error {tid}: {exc}")
                        _record_error("evaluate_tender", exc, tender_id=tid, ticker=tender.get("ticker"))
                        continue
                    processed[tid] = sig
                    _bump_counter("tenders_processed", 1)
                time.sleep(1.0)
        except Exception as exc:
            run_error = str(exc)
            exit_reason = "fatal_error"
            print(f"Fatal error: {exc}")
            _record_error("main_loop", exc)
        finally:
            _record_event("run_end", exit_reason=exit_reason, run_error=run_error)
            if SAVE_REPORT_ON_EXIT:
                try:
                    save_run_report(session, exit_reason, run_error=run_error)
                except Exception as exc:
                    print(f"Report save error: {exc}")
                    _record_error("save_run_report", exc, exit_reason=exit_reason)


if __name__ == "__main__":
    main()
