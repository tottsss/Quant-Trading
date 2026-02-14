"""Multi-ticker indicator assistant (EMA/RSI/Volume + order-book microstructure).

Default mode is SIGNAL_ONLY (no orders sent).
This is designed for manual decision support, not blind auto-trading.

Run (PowerShell):
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  $env:RIT_TICKERS="RITC,COMP"      # optional; default = all active tickers
  python .\01_liquidity_indicator_multiticker_assistant_standalone.py

Optional:
  $env:RIT_AUTO_EXEC="1"            # auto orders from ranked signals
  $env:RIT_ALLOW_SPECULATIVE="1"    # allow auto orders in Liquidity case
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
TICKERS_ENV = os.environ.get("RIT_TICKERS", "").strip()

POLL_SECS = float(os.environ.get("RIT_POLL_SECS", "1.5"))
BOOK_LEVELS = int(os.environ.get("RIT_BOOK_LEVELS", "10"))
TAS_LIMIT = int(os.environ.get("RIT_TAS_LIMIT", "140"))
EMA_FAST = int(os.environ.get("RIT_EMA_FAST", "9"))
EMA_SLOW = int(os.environ.get("RIT_EMA_SLOW", "21"))
RSI_PERIOD = int(os.environ.get("RIT_RSI_PERIOD", "14"))
VOL_LOOKBACK = int(os.environ.get("RIT_VOL_LOOKBACK", "20"))

ORDER_QTY = max(1.0, min(10000.0, float(os.environ.get("RIT_ORDER_QTY", "1000"))))
ORDER_COOLDOWN_SECS = float(os.environ.get("RIT_ORDER_COOLDOWN_SECS", "3.0"))
TOP_N = max(1, int(os.environ.get("RIT_TOP_N", "4")))
MAX_SPREAD_BPS = float(os.environ.get("RIT_MAX_SPREAD_BPS", "35"))

AUTO_EXEC = os.environ.get("RIT_AUTO_EXEC", "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_SPECULATIVE = os.environ.get("RIT_ALLOW_SPECULATIVE", "0").strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass
class TickerSignal:
    ticker: str
    signal: str
    score: int
    confidence: float
    reason: str
    spread_bps: float
    blocked: bool
    block_reason: str


def ema(values: list[float], period: int) -> float | None:
    if len(values) < max(2, period):
        return None
    k = 2.0 / (period + 1.0)
    out = values[0]
    for v in values[1:]:
        out = (v * k) + (out * (1.0 - k))
    return out


def rsi(values: list[float], period: int) -> float | None:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        d = values[i] - values[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    sub = values[-window:]
    return sum(sub) / float(window)


def parse_tas(tas: list[dict]) -> tuple[list[float], list[float]]:
    prices = []
    vols = []
    for row in tas:
        p = row.get("price")
        q = row.get("quantity", row.get("qty", 0))
        if isinstance(p, (int, float)) and p > 0:
            prices.append(float(p))
            vols.append(float(q) if isinstance(q, (int, float)) and q >= 0 else 0.0)
    return prices, vols


def parse_levels(book: dict, side: str) -> list[tuple[float, float]]:
    out = []
    for lv in book.get(side, []):
        px = lv.get("price")
        qty = lv.get("quantity", lv.get("qty"))
        if isinstance(px, (int, float)) and isinstance(qty, (int, float)) and px > 0 and qty > 0:
            out.append((float(px), float(qty)))
    return out


def depth_vwap_for_action(book: dict, action: str, qty: float) -> float | None:
    side = "asks" if action == "BUY" else "bids"
    lv = parse_levels(book, side)
    if not lv:
        return None
    lv.sort(key=lambda x: x[0], reverse=(action == "SELL"))
    rem = qty
    notional = 0.0
    used = 0.0
    for px, q in lv:
        if rem <= 0:
            break
        take = min(rem, q)
        rem -= take
        used += take
        notional += take * px
    if rem > 0 or used <= 0:
        return None
    return notional / used


def order_book_features(book: dict) -> tuple[float | None, float | None, float | None, float | None]:
    bids = parse_levels(book, "bids")
    asks = parse_levels(book, "asks")
    if not bids or not asks:
        return None, None, None, None
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_ask <= best_bid:
        return None, None, None, None
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / max(1e-9, mid)) * 10000.0

    bid_depth = sum(q for _p, q in bids[:BOOK_LEVELS])
    ask_depth = sum(q for _p, q in asks[:BOOK_LEVELS])
    denom = max(1.0, bid_depth + ask_depth)
    imbalance = (bid_depth - ask_depth) / denom  # [-1..1]
    return mid, spread_bps, imbalance, best_ask - best_bid


def resolve_tickers(all_tickers: list[str]) -> list[str]:
    if not TICKERS_ENV:
        return all_tickers
    raw = [x.strip().upper() for x in TICKERS_ENV.split(",") if x.strip()]
    keep = []
    seen = set()
    for t in raw:
        if t in all_tickers and t not in seen:
            seen.add(t)
            keep.append(t)
    return keep


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


def tender_ticker_set(tenders: list[dict]) -> set[str]:
    out = set()
    for t in tenders:
        tk = t.get("ticker")
        if isinstance(tk, str) and tk:
            out.add(tk)
    return out


def build_signal(prices: list[float], volumes: list[float], book: dict) -> tuple[str, int, float, str, float]:
    if len(prices) < max(EMA_SLOW + 2, RSI_PERIOD + 2) or len(volumes) < VOL_LOOKBACK:
        return "HOLD", 0, 0.0, "not_enough_data", 999.0

    e_fast = ema(prices, EMA_FAST)
    e_slow = ema(prices, EMA_SLOW)
    r = rsi(prices, RSI_PERIOD)
    vol_now = volumes[-1]
    vol_avg = sma(volumes, VOL_LOOKBACK)
    mid, spread_bps, imbalance, _spread_abs = order_book_features(book)

    if e_fast is None or e_slow is None or r is None or vol_avg is None or mid is None or spread_bps is None or imbalance is None:
        return "HOLD", 0, 0.0, "indicator_unavailable", 999.0

    trend = 1 if e_fast > e_slow else -1
    momentum = 1 if r > 55 else (-1 if r < 45 else 0)
    vol_support = 1 if (vol_now / max(1e-9, vol_avg)) >= 1.10 else 0
    ob_support = 1 if imbalance > 0.15 else (-1 if imbalance < -0.15 else 0)
    spread_penalty = -1 if spread_bps > MAX_SPREAD_BPS else 0

    score = trend + momentum + vol_support + ob_support + spread_penalty
    conf = min(1.0, abs(score) / 4.0)
    reason = f"trend={trend} rsi={r:.1f} volx={vol_now/max(1e-9,vol_avg):.2f} ob={imbalance:.2f} spr={spread_bps:.1f}bps"

    if score >= 2:
        return "BUY", score, conf, reason, spread_bps
    if score <= -2:
        return "SELL", score, conf, reason, spread_bps
    return "HOLD", score, conf, reason, spread_bps


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY)
    last_order_at = 0.0
    last_fine_print = None

    print(
        f"Connected to {BASE_URL} | mode={'AUTO' if AUTO_EXEC else 'SIGNAL_ONLY'} "
        f"| allow_speculative={ALLOW_SPECULATIVE} | tickers={TICKERS_ENV or 'ALL'}"
    )

    while True:
        case = client.get("/case")
        if case.get("status") != "ACTIVE":
            print("Case inactive. Waiting...")
            time.sleep(POLL_SECS)
            continue

        case_name = str(case.get("name") or case.get("simulation_name") or "").lower()
        in_liquidity = "liquidity" in case_name

        securities = client.get("/securities")
        all_tickers = [s.get("ticker") for s in securities if s.get("ticker")]
        tickers = resolve_tickers(all_tickers)
        if not tickers:
            print("No matching tickers resolved.")
            time.sleep(POLL_SECS)
            continue

        tenders = client.get("/tenders")
        blocked_by_tender = tender_ticker_set(tenders)

        # Fine monitor
        try:
            trader = client.get("/trader")
            limits = client.get("/limits")
            fines_guess = sum_fine_fields(trader) + sum_fine_fields(limits)
        except Exception:
            fines_guess = 0.0
        if last_fine_print is None or abs(fines_guess - last_fine_print) > 0.5:
            print(f"FINE WATCH guessed_total={fines_guess:.2f}")
            last_fine_print = fines_guess

        rows: list[TickerSignal] = []
        for tk in tickers:
            try:
                tas = client.get("/securities/tas", {"ticker": tk, "limit": TAS_LIMIT})
                book = client.get("/securities/book", {"ticker": tk, "limit": BOOK_LEVELS})
            except Exception as exc:
                rows.append(TickerSignal(tk, "HOLD", 0, 0.0, f"data_error={exc}", 999.0, True, "data_error"))
                continue

            prices, vols = parse_tas(tas)
            sig, score, conf, reason, spread_bps = build_signal(prices, vols, book)

            block_reason = ""
            blocked = False
            if tk in blocked_by_tender:
                blocked = True
                block_reason = "open_tender_front_running_risk"
            elif in_liquidity and not ALLOW_SPECULATIVE:
                blocked = True
                block_reason = "liquidity_speculation_guard"

            rows.append(TickerSignal(tk, sig, score, conf, reason, spread_bps, blocked, block_reason))

        rows.sort(key=lambda x: (abs(x.score), x.confidence), reverse=True)
        top = rows[:TOP_N]

        print(f"\nTICK {case.get('tick')} TOP {len(top)} SIGNALS")
        for r in top:
            block_txt = f" | BLOCK={r.block_reason}" if r.blocked else ""
            print(
                f"{r.ticker:6s} sig={r.signal:4s} score={r.score:+d} conf={r.confidence:.2f} "
                f"spr={r.spread_bps:6.1f}bps | {r.reason}{block_txt}"
            )

        if AUTO_EXEC:
            tradable = [r for r in rows if (not r.blocked) and r.signal in ("BUY", "SELL")]
            if tradable:
                best = tradable[0]
                now = time.time()
                if now - last_order_at >= ORDER_COOLDOWN_SECS:
                    try:
                        resp = client.post(
                            "/orders",
                            {
                                "ticker": best.ticker,
                                "type": "MARKET",
                                "action": best.signal,
                                "quantity": ORDER_QTY,
                            },
                        )
                        print(f"AUTO ORDER {best.signal} {best.ticker} qty={ORDER_QTY:.0f} resp={resp}")
                        last_order_at = now
                    except Exception as exc:
                        print(f"AUTO ORDER ERROR {best.ticker}: {exc}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
