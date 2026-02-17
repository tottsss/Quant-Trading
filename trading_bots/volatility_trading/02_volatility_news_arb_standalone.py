"""
RITC 2026 Volatility Trading Case: simple vol mis-pricing trader
+ STOP / PAUSE / "RESET CYCLE" overlay (does not change trading strategy)

Strategy is unchanged:
- compute IV vs analyst sigma
- trade ATM straddle if diff >= threshold
- delta hedge with RTM

Overlay:
- when flat and edge is gone + pnl plateau/drawdown => flatten and PAUSE or STOP
- optionally resume on next new "this week vol" news (weekly sigma regime change)
"""

import os
import time
import math
import re
import requests
from dataclasses import dataclass
from typing import Dict, Optional, Any, List


# ----------------------------
# Black-Scholes helpers
# ----------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_d1(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))

def bs_price(opt: str, S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    opt = opt.upper()
    if T <= 0:
        return max(0.0, S - K) if opt == "C" else max(0.0, K - S)
    d1 = bs_d1(S, K, T, sigma, r)
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "C":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def bs_delta(opt: str, S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    opt = opt.upper()
    if T <= 0:
        if opt == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = bs_d1(S, K, T, sigma, r)
    if opt == "C":
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0  # put delta

def implied_vol_bisect(
    opt: str,
    price: float,
    S: float,
    K: float,
    T: float,
    r: float = 0.0,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-4,
    max_iter: int = 60,
) -> Optional[float]:
    opt = opt.upper()

    intrinsic = max(0.0, S - K) if opt == "C" else max(0.0, K - S)
    if price < intrinsic - 1e-6:
        return None
    if opt == "C" and price > S + 1e-6:
        return None
    if opt == "P" and price > K + 1e-6:
        return None

    f_lo = bs_price(opt, S, K, T, lo, r) - price
    f_hi = bs_price(opt, S, K, T, hi, r) - price
    if f_lo * f_hi > 0:
        return None  # couldn't bracket

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(opt, S, K, T, mid, r) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# ----------------------------
# RIT API helpers
# ----------------------------

def request_json(
    session: requests.Session,
    method: str,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0,
    max_retries: int = 50,
):
    for attempt in range(max_retries):
        try:
            resp = session.request(method, url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt >= max_retries - 1:
                raise
            time.sleep(min(1.0, 0.10 + 0.05 * attempt))
            continue

        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("wait", 0.25))
            except Exception:
                wait = 0.25
            time.sleep(max(0.05, wait))
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError("Too many retries / rate limits. Slow down polling.")

def mid(bid: Any, ask: Any) -> Optional[float]:
    try:
        b = float(bid)
        a = float(ask)
    except Exception:
        return None
    if b <= 0 or a <= 0:
        return None
    return 0.5 * (b + a)

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ----------------------------
# News parsing
# ----------------------------

VOL_THIS_WEEK_RE = re.compile(
    r"realized volatility of RTM(?:\s+for)?\s+this week\s+will be\s+(\d+(?:\.\d+)?)%?",
    re.I,
)
DELTA_LIMIT_RE = re.compile(
    r"delta limit.*?is\s+([\d,]+)", re.I
)
PENALTY_PCT_RE = re.compile(
    r"penalty percentage is\s+(\d+(?:\.\d+)?)%?", re.I
)

@dataclass
class State:
    last_news_id: int = 0
    sigma_week: Optional[float] = None
    delta_limit: int = 10_000
    penalty_rate: float = 0.01

    current_sign: int = 0  # -1 short, +1 long, 0 flat

    # --- STOP / RESET overlay state ---
    paused: bool = False
    pause_until_tick: int = 0
    new_sigma_event: bool = False

    initial_nlv: Optional[float] = None
    cycle_start_pnl: Optional[float] = None
    peak_pnl: Optional[float] = None
    peak_tick: int = 0
    flat_edge_secs: int = 0

def parse_news(text: str, state: State) -> None:
    text = " ".join(text.split())

    m = VOL_THIS_WEEK_RE.search(text)
    if m:
        new_sigma = float(m.group(1)) / 100.0
        if state.sigma_week is None or abs(new_sigma - state.sigma_week) > 1e-12:
            state.new_sigma_event = True
        state.sigma_week = new_sigma

    m = DELTA_LIMIT_RE.search(text)
    if m:
        state.delta_limit = int(m.group(1).replace(",", ""))

    m = PENALTY_PCT_RE.search(text)
    if m:
        state.penalty_rate = float(m.group(1)) / 100.0


# ----------------------------
# Main strategy (unchanged) + overlay
# ----------------------------

def main():
    API_BASE = os.environ.get("RIT_API_BASE", "http://localhost:9999/v1").rstrip("/")
    AUTH_MODE = os.environ.get("RIT_AUTH_MODE", "auto").strip().lower()
    API_KEY = os.environ.get("RIT_API_KEY", "").strip()
    DMA_USERNAME = os.environ.get("RIT_DMA_USERNAME", "").strip()
    DMA_PASSWORD = os.environ.get("RIT_DMA_PASSWORD", "").strip()
    HOST_HEADER = os.environ.get("RIT_HOST_HEADER", "").strip()

    UNDERLYING = "RTM"

    STRIKES = list(range(45, 55))
    def CALL(k: int) -> str: return f"RTM1C{k}"
    def PUT(k: int) -> str:  return f"RTM1P{k}"

    TOTAL_TICKS = 300
    INIT_TTM_YEARS = (20 / 240)

    ENTRY_VOL_DIFF = 0.03
    EXIT_VOL_DIFF  = 0.015

    MAX_LEG_CONTRACTS = 500
    FULL_SIZE_AT = 0.10

    MAX_OPT_ORDER = 100
    MAX_STK_ORDER = 10_000

    END_TICK_FLATTEN = 295
    LOOP_SLEEP = 0.25
    REQUEST_TIMEOUT = float(os.environ.get("RIT_REQUEST_TIMEOUT", "5.0"))
    REQUEST_RETRIES = int(os.environ.get("RIT_MAX_RETRIES", "40"))
    R = 0.0

    # ----------------------------
    # Overlay knobs (stop / pause / reset)
    # ----------------------------
    PROTECT_AFTER = 50_000      # start protecting profits after this (if pnl readable)
    TAKE_PROFIT   = 100_000     # if pnl readable and >= this while flat, lock it

    PLATEAU_SECS  = 20          # no new high for this long => pnl plateau
    EDGE_GONE_SECS = 15         # abs(diff) < EXIT_VOL_DIFF while flat for this long

    DRAWDOWN_STOP = 6_000       # if pnl drawdown from peak exceeds this (after PROTECT_AFTER), lock

    AFTER_LOCK_ACTION = "PAUSE" # "STOP" or "PAUSE"
    RESUME_ON_NEW_SIGMA = True  # if PAUSE: resume only on new sigma news (recommended)
    COOLDOWN_SECS = 30          # if not resuming on new sigma

    state = State()

    with requests.Session() as s:
        if HOST_HEADER:
            s.headers.update({"Host": HOST_HEADER})

        # Auth selection:
        # - dma: use HTTP Basic (username/password)
        # - client: use X-API-key
        # - auto: choose dma for non-9999 endpoints, otherwise client
        if AUTH_MODE == "dma":
            use_dma = True
            use_client = False
        elif AUTH_MODE == "client":
            use_dma = False
            use_client = True
        else:
            use_dma = ":9999" not in API_BASE
            use_client = not use_dma
            if use_dma and not (DMA_USERNAME and DMA_PASSWORD) and API_KEY:
                use_dma = False
                use_client = True

        if use_dma and DMA_USERNAME and DMA_PASSWORD:
            s.auth = (DMA_USERNAME, DMA_PASSWORD)
        elif use_client and API_KEY:
            s.headers.update({"X-API-key": API_KEY})

        print(
            f"[startup] API_BASE={API_BASE} auth_mode={AUTH_MODE} "
            f"dma={bool(use_dma and DMA_USERNAME and DMA_PASSWORD)} "
            f"client={bool(use_client and API_KEY)} timeout={REQUEST_TIMEOUT}s",
            flush=True,
        )

        def get_case():
            return request_json(
                s,
                "GET",
                f"{API_BASE}/case",
                timeout=REQUEST_TIMEOUT,
                max_retries=REQUEST_RETRIES,
            )

        def get_news():
            return request_json(
                s,
                "GET",
                f"{API_BASE}/news",
                timeout=REQUEST_TIMEOUT,
                max_retries=REQUEST_RETRIES,
            )

        def get_securities():
            return request_json(
                s,
                "GET",
                f"{API_BASE}/securities",
                timeout=REQUEST_TIMEOUT,
                max_retries=REQUEST_RETRIES,
            )

        def try_get_trader() -> Optional[Dict[str, Any]]:
            # Not all installs expose /trader; keep it optional.
            try:
                x = request_json(s, "GET", f"{API_BASE}/trader")
                if isinstance(x, dict):
                    return x
                if isinstance(x, list) and x and isinstance(x[0], dict):
                    return x[0]
            except Exception:
                pass
            return None

        def extract_pnl(case: Dict[str, Any]) -> Optional[float]:
            # 1) direct pnl fields if present
            for k in ("pnl", "profit", "total_pnl", "realized_pnl", "unrealized_pnl"):
                if k in case:
                    try:
                        return float(case[k])
                    except Exception:
                        pass

            # 2) NLV in case
            for k in ("nlv", "net_liquidation", "portfolio_value"):
                if k in case:
                    try:
                        nlv = float(case[k])
                        if state.initial_nlv is None:
                            state.initial_nlv = nlv
                        return nlv - state.initial_nlv
                    except Exception:
                        pass

            # 3) /trader fallback
            tr = try_get_trader()
            if tr:
                for k in ("pnl", "profit", "total_pnl"):
                    if k in tr:
                        try:
                            return float(tr[k])
                        except Exception:
                            pass
                for k in ("nlv", "net_liquidation", "portfolio_value"):
                    if k in tr:
                        try:
                            nlv = float(tr[k])
                            if state.initial_nlv is None:
                                state.initial_nlv = nlv
                            return nlv - state.initial_nlv
                        except Exception:
                            pass

            return None

        def place_market(ticker: str, action: str, qty: int):
            remaining = int(qty)
            max_clip = MAX_STK_ORDER if ticker == UNDERLYING else MAX_OPT_ORDER
            while remaining > 0:
                clip = min(remaining, max_clip)
                request_json(
                    s,
                    "POST",
                    f"{API_BASE}/orders",
                    params={"ticker": ticker, "type": "MARKET", "quantity": clip, "action": action},
                    timeout=REQUEST_TIMEOUT,
                    max_retries=REQUEST_RETRIES,
                )
                remaining -= clip
                time.sleep(0.02)

        def flatten_all(sec: Dict[str, Dict[str, Any]]):
            # Close all options (robust)
            for K in STRIKES:
                for tkr in (CALL(K), PUT(K)):
                    pos = int(sec.get(tkr, {}).get("position", 0))
                    if pos != 0:
                        place_market(tkr, "SELL" if pos > 0 else "BUY", abs(pos))

            # Close RTM
            rtm_pos = int(sec.get(UNDERLYING, {}).get("position", 0))
            if rtm_pos != 0:
                place_market(UNDERLYING, "SELL" if rtm_pos > 0 else "BUY", abs(rtm_pos))

            state.current_sign = 0

        def maybe_resume(tick: int, pnl: Optional[float]):
            if not state.paused:
                return
            if RESUME_ON_NEW_SIGMA:
                if state.new_sigma_event:
                    state.paused = False
                    # reset cycle baseline (your “pnl is zero” idea for decision logic)
                    if pnl is not None:
                        state.cycle_start_pnl = pnl
                        state.peak_pnl = pnl
                        state.peak_tick = tick
                    state.flat_edge_secs = 0
            else:
                if tick >= state.pause_until_tick:
                    state.paused = False
                    if pnl is not None:
                        state.cycle_start_pnl = pnl
                        state.peak_pnl = pnl
                        state.peak_tick = tick
                    state.flat_edge_secs = 0

        # ---- Main loop ----
        while True:
            try:
                case = get_case()
            except Exception as exc:
                print(f"[warn] get_case failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(LOOP_SLEEP)
                continue
            tick = int(case["tick"])
            status = case.get("status", "ACTIVE")
            if status != "ACTIVE":
                time.sleep(0.2)
                continue

            # News
            try:
                news_items: List[Dict[str, Any]] = get_news()
            except Exception:
                news_items = []

            state.new_sigma_event = False
            max_seen_id = state.last_news_id
            for item in sorted(news_items, key=lambda x: int(x.get("news_id", 0))):
                nid = int(item.get("news_id", 0))
                if nid <= state.last_news_id:
                    continue
                text = f"{item.get('headline','')} {item.get('body','')}"
                parse_news(text, state)
                max_seen_id = max(max_seen_id, nid)
            state.last_news_id = max_seen_id

            pnl = extract_pnl(case)

            # If paused: stay flat + wait
            if state.paused:
                try:
                    sec_map = {d["ticker"]: d for d in get_securities()}
                except Exception as exc:
                    print(f"[warn] get_securities failed while paused: {type(exc).__name__}: {exc}", flush=True)
                    time.sleep(LOOP_SLEEP)
                    continue
                flatten_all(sec_map)
                maybe_resume(tick, pnl)
                time.sleep(LOOP_SLEEP)
                continue

            # init cycle baseline when pnl becomes readable
            if pnl is not None and state.cycle_start_pnl is None:
                state.cycle_start_pnl = pnl
                state.peak_pnl = pnl
                state.peak_tick = tick

            # snapshot
            try:
                secs = get_securities()
            except Exception as exc:
                print(f"[warn] get_securities failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(LOOP_SLEEP)
                continue
            sec = {d["ticker"]: d for d in secs}

            rtm = sec.get(UNDERLYING)
            if not rtm:
                time.sleep(LOOP_SLEEP)
                continue

            S_mid = mid(rtm.get("bid"), rtm.get("ask"))
            if S_mid is None:
                time.sleep(LOOP_SLEEP)
                continue

            if state.sigma_week is None:
                time.sleep(LOOP_SLEEP)
                continue

            # TTM
            T = INIT_TTM_YEARS * max(0.0, (TOTAL_TICKS - tick) / TOTAL_TICKS)
            T = max(T, 1e-6)

            # ATM strike
            K = min(STRIKES, key=lambda k: abs(k - S_mid))
            call_t, put_t = CALL(K), PUT(K)

            c = sec.get(call_t)
            p = sec.get(put_t)
            if not c or not p:
                time.sleep(LOOP_SLEEP)
                continue

            c_mid = mid(c.get("bid"), c.get("ask"))
            p_mid = mid(p.get("bid"), p.get("ask"))
            if c_mid is None or p_mid is None:
                time.sleep(LOOP_SLEEP)
                continue

            iv_c = implied_vol_bisect("C", c_mid, S_mid, K, T, R)
            iv_p = implied_vol_bisect("P", p_mid, S_mid, K, T, R)
            if iv_c is None or iv_p is None:
                time.sleep(LOOP_SLEEP)
                continue
            iv = 0.5 * (iv_c + iv_p)

            sigma_fair = state.sigma_week
            diff = sigma_fair - iv

            # ---- overlay: stop/lock decision (only when flat) ----
            if state.current_sign == 0 and abs(diff) < EXIT_VOL_DIFF:
                state.flat_edge_secs += 1
            else:
                state.flat_edge_secs = 0

            cycle_pnl = None
            if pnl is not None and state.cycle_start_pnl is not None:
                cycle_pnl = pnl - state.cycle_start_pnl
                if state.peak_pnl is None or pnl > state.peak_pnl + 1.0:
                    state.peak_pnl = pnl
                    state.peak_tick = tick

            plateau = (pnl is not None and (tick - state.peak_tick) >= PLATEAU_SECS)

            should_lock = False
            if state.current_sign == 0:
                if cycle_pnl is not None:
                    if cycle_pnl >= TAKE_PROFIT:
                        should_lock = True
                    elif cycle_pnl >= PROTECT_AFTER and plateau and state.flat_edge_secs >= EDGE_GONE_SECS:
                        should_lock = True
                    elif cycle_pnl >= PROTECT_AFTER and state.peak_pnl is not None and pnl <= state.peak_pnl - DRAWDOWN_STOP:
                        should_lock = True
                else:
                    # no pnl readable: edge-gone pause
                    if state.flat_edge_secs >= EDGE_GONE_SECS:
                        should_lock = True

            if should_lock:
                flatten_all(sec)
                if AFTER_LOCK_ACTION.upper() == "STOP":
                    break

                state.paused = True
                if not RESUME_ON_NEW_SIGMA:
                    state.pause_until_tick = tick + int(COOLDOWN_SECS)

                # reset cycle baseline ("as if pnl is zero") for next cycle
                if pnl is not None:
                    state.cycle_start_pnl = pnl
                    state.peak_pnl = pnl
                    state.peak_tick = tick

                state.flat_edge_secs = 0
                time.sleep(LOOP_SLEEP)
                continue

            # ----------------------------
            # ORIGINAL STRATEGY (unchanged)
            # ----------------------------

            target_sign = state.current_sign
            if diff > ENTRY_VOL_DIFF:
                target_sign = +1
            elif diff < -ENTRY_VOL_DIFF:
                target_sign = -1
            elif abs(diff) < EXIT_VOL_DIFF:
                target_sign = 0

            size = int(MAX_LEG_CONTRACTS * min(1.0, abs(diff) / FULL_SIZE_AT))
            if target_sign == 0:
                size = 0

            target_call_pos = target_sign * size
            target_put_pos  = target_sign * size

            if tick >= END_TICK_FLATTEN:
                target_call_pos = 0
                target_put_pos = 0

            pos_call = int(c.get("position", 0))
            pos_put  = int(p.get("position", 0))
            state.current_sign = target_sign

            d_call = target_call_pos - pos_call
            if d_call != 0:
                try:
                    place_market(call_t, "BUY" if d_call > 0 else "SELL", abs(d_call))
                except Exception as exc:
                    print(f"[warn] order failed {call_t}: {type(exc).__name__}: {exc}", flush=True)

            d_put = target_put_pos - pos_put
            if d_put != 0:
                try:
                    place_market(put_t, "BUY" if d_put > 0 else "SELL", abs(d_put))
                except Exception as exc:
                    print(f"[warn] order failed {put_t}: {type(exc).__name__}: {exc}", flush=True)

            # refresh for delta hedge
            try:
                secs = get_securities()
            except Exception as exc:
                print(f"[warn] get_securities refresh failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(LOOP_SLEEP)
                continue
            sec = {d["ticker"]: d for d in secs}

            rtm_pos = int(sec[UNDERLYING].get("position", 0))
            pos_call = int(sec[call_t].get("position", 0))
            pos_put  = int(sec[put_t].get("position", 0))

            S_mid2 = mid(sec[UNDERLYING].get("bid"), sec[UNDERLYING].get("ask")) or S_mid

            delta_c = bs_delta("C", S_mid2, K, T, sigma_fair, R)
            delta_p = bs_delta("P", S_mid2, K, T, sigma_fair, R)
            opt_delta = (pos_call * delta_c + pos_put * delta_p) * 100.0
            port_delta = opt_delta + rtm_pos

            band = max(250, int(0.10 * state.delta_limit))
            if abs(port_delta) > band:
                target_rtm = int(round(-opt_delta))
                target_rtm = int(clamp(target_rtm, -50_000, 50_000))
                trade = target_rtm - rtm_pos
                if trade != 0:
                    try:
                        place_market(UNDERLYING, "BUY" if trade > 0 else "SELL", abs(trade))
                    except Exception as exc:
                        print(f"[warn] order failed {UNDERLYING}: {type(exc).__name__}: {exc}", flush=True)

            if tick >= END_TICK_FLATTEN:
                break

            time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
