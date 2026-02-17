"""Indicator assistant bot (EMA/RSI/Volume) for manual trading support.

Important:
- In Liquidity Risk Case, pure indicator trading is usually speculative by definition.
- So this script defaults to SIGNAL-ONLY mode (no orders sent).
- It also shows fine metrics (when available) so you can detect penalties quickly.

Run (PowerShell):
  pip install requests
  $env:RIT_API_KEY="YOUR_KEY"
  $env:RIT_BASE_URL="http://localhost:9999/v1"
  $env:RIT_TICKER="RITC"
  python .\01_liquidity_indicator_assistant_standalone.py

Optional:
  $env:RIT_AUTO_EXEC="1"            # enable real orders (NOT recommended in Liquidity case)
  $env:RIT_ALLOW_SPECULATIVE="1"    # allows auto-orders even in Liquidity case
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
TICKER = os.environ.get("RIT_TICKER", "RITC")

POLL_SECS = float(os.environ.get("RIT_POLL_SECS", "1.0"))
TAS_LIMIT = int(os.environ.get("RIT_TAS_LIMIT", "140"))
EMA_FAST = int(os.environ.get("RIT_EMA_FAST", "9"))
EMA_SLOW = int(os.environ.get("RIT_EMA_SLOW", "21"))
RSI_PERIOD = int(os.environ.get("RIT_RSI_PERIOD", "14"))
VOL_LOOKBACK = int(os.environ.get("RIT_VOL_LOOKBACK", "20"))
ORDER_QTY = max(1.0, min(10000.0, float(os.environ.get("RIT_ORDER_QTY", "1000"))))
ORDER_COOLDOWN_SECS = float(os.environ.get("RIT_ORDER_COOLDOWN_SECS", "3.0"))

AUTO_EXEC = os.environ.get("RIT_AUTO_EXEC", "0").strip() in {"1", "true", "yes", "on"}
ALLOW_SPECULATIVE = os.environ.get("RIT_ALLOW_SPECULATIVE", "0").strip() in {"1", "true", "yes", "on"}


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
class SignalState:
    signal: str
    reason: str
    confidence: float


def ema(values: list[float], period: int) -> float | None:
    if len(values) < max(2, period):
        return None
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = (v * k) + (e * (1.0 - k))
    return e


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        delta = values[i] - values[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
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


def build_signal(prices: list[float], volumes: list[float]) -> SignalState:
    if len(prices) < max(EMA_SLOW + 2, RSI_PERIOD + 2) or len(volumes) < VOL_LOOKBACK:
        return SignalState("HOLD", "not_enough_data", 0.0)

    last = prices[-1]
    e_fast = ema(prices, EMA_FAST)
    e_slow = ema(prices, EMA_SLOW)
    r = rsi(prices, RSI_PERIOD)
    vol_now = volumes[-1]
    vol_avg = sma(volumes, VOL_LOOKBACK)

    if e_fast is None or e_slow is None or r is None or vol_avg is None or vol_avg <= 0:
        return SignalState("HOLD", "indicator_unavailable", 0.0)

    trend_up = e_fast > e_slow
    trend_dn = e_fast < e_slow
    vol_ratio = vol_now / vol_avg
    ema_gap_bps = abs(e_fast - e_slow) / max(1e-9, last) * 10000.0

    conf = min(1.0, (ema_gap_bps / 8.0) + max(0.0, (vol_ratio - 1.0) * 0.4))
    if trend_up and r < 68 and vol_ratio >= 1.05:
        return SignalState("BUY", f"ema_up rsi={r:.1f} volx={vol_ratio:.2f}", conf)
    if trend_dn and r > 32 and vol_ratio >= 1.05:
        return SignalState("SELL", f"ema_down rsi={r:.1f} volx={vol_ratio:.2f}", conf)
    return SignalState("HOLD", f"mixed rsi={r:.1f} volx={vol_ratio:.2f}", conf)


def extract_prices_volumes(tas: list[dict]) -> tuple[list[float], list[float]]:
    prices = []
    vols = []
    for row in tas:
        p = row.get("price")
        q = row.get("quantity", row.get("qty", 0))
        if isinstance(p, (int, float)) and p > 0:
            prices.append(float(p))
            vols.append(float(q) if isinstance(q, (int, float)) and q >= 0 else 0.0)
    return prices, vols


def contains_open_tender_for_ticker(tenders: list[dict], ticker: str) -> bool:
    for t in tenders:
        tk = t.get("ticker")
        if tk == ticker:
            return True
    return False


def sum_fine_fields(obj) -> float:
    total = 0.0
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if "fine" in kl and isinstance(v, (int, float)):
                total += float(v)
            total += sum_fine_fields(v)
    elif isinstance(obj, list):
        for v in obj:
            total += sum_fine_fields(v)
    return total


def read_fines(client: RITClient) -> tuple[float | None, float | None]:
    # total_fines_guess, trader_reported_fines (if key exists)
    try:
        trader = client.get("/trader")
    except Exception:
        trader = None
    try:
        limits = client.get("/limits")
    except Exception:
        limits = None

    guessed = 0.0
    explicit = None

    if trader is not None:
        guessed += sum_fine_fields(trader)
        if isinstance(trader, dict):
            for k in ("fines", "fine", "case_fines", "penalty", "penalties"):
                v = trader.get(k)
                if isinstance(v, (int, float)):
                    explicit = float(v)
                    break

    if limits is not None:
        guessed += sum_fine_fields(limits)

    return guessed, explicit


def place_market_order(client: RITClient, ticker: str, action: str, qty: float):
    return client.post(
        "/orders",
        params={"ticker": ticker, "type": "MARKET", "action": action, "quantity": qty},
    )


def main():
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY first.")

    client = RITClient(API_KEY)
    last_order_at = 0.0
    last_printed_fines = None

    print(
        f"Connected to {BASE_URL} | ticker={TICKER} | mode={'AUTO' if AUTO_EXEC else 'SIGNAL_ONLY'} "
        f"| allow_speculative={ALLOW_SPECULATIVE}"
    )

    while True:
        case = client.get("/case")
        if case.get("status") != "ACTIVE":
            print("Case inactive. Waiting...")
            time.sleep(POLL_SECS)
            continue

        case_name = str(case.get("name") or case.get("simulation_name") or "").lower()
        in_liquidity_case = "liquidity" in case_name

        tas = client.get("/securities/tas", {"ticker": TICKER, "limit": TAS_LIMIT})
        prices, volumes = extract_prices_volumes(tas)
        if not prices:
            print("No TAS yet for ticker.")
            time.sleep(POLL_SECS)
            continue

        sig = build_signal(prices, volumes)
        last_px = prices[-1]

        # Front-running risk check.
        tenders = client.get("/tenders")
        front_running_risk = contains_open_tender_for_ticker(tenders, TICKER)

        # Fine monitor.
        fines_guess, fines_explicit = read_fines(client)
        if last_printed_fines is None or abs(fines_guess - last_printed_fines) > 0.5:
            if fines_explicit is not None:
                print(f"FINE WATCH explicit={fines_explicit:.2f} guessed_total={fines_guess:.2f}")
            else:
                print(f"FINE WATCH guessed_total={fines_guess:.2f}")
            last_printed_fines = fines_guess

        block_reason = None
        if front_running_risk:
            block_reason = "open tender on same ticker (front-running risk)"
        elif in_liquidity_case and not ALLOW_SPECULATIVE:
            block_reason = "liquidity case speculative-risk guard"

        print(
            f"TICK={case.get('tick')} {TICKER} px={last_px:.4f} signal={sig.signal} "
            f"conf={sig.confidence:.2f} reason={sig.reason}"
            + (f" | BLOCKED: {block_reason}" if block_reason else "")
        )

        if AUTO_EXEC and sig.signal in ("BUY", "SELL"):
            if block_reason:
                time.sleep(POLL_SECS)
                continue

            now = time.time()
            if now - last_order_at < ORDER_COOLDOWN_SECS:
                time.sleep(POLL_SECS)
                continue

            try:
                resp = place_market_order(client, TICKER, sig.signal, ORDER_QTY)
                print(f"ORDER {sig.signal} {TICKER} qty={ORDER_QTY:.0f} resp={resp}")
                last_order_at = now
            except Exception as exc:
                print(f"ORDER ERROR: {exc}")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
