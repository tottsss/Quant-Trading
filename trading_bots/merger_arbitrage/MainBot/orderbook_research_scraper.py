#!/usr/bin/env python3
"""Order book research scraper for RIT merger-arb cases.

Captures full top-N order books over time and writes one JSON file with:
- raw snapshots (case state + per-ticker books + features)
- optional fill stream (from /orders?status=TRANSACTED)
- built-in analysis (spread/imbalance/quote update rates/fill-impact estimate)

Typical use:
  python orderbook_research_scraper.py --duration 300 --interval 0.10 --levels 10

With fill-impact tracking:
  python orderbook_research_scraper.py --duration 300 --track-fills --fills-poll-every 0.5
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp


def _find_repo_root(start: Path) -> Path:
    for p in [start] + list(start.parents):
        if (p / ".git").exists():
            return p
    return start.parent


_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _find_repo_root(_THIS_FILE)


def _default_log_dir() -> Path:
    candidates = [
        _REPO_ROOT / "trading_bots" / "merger_arbitrage" / "logs",
        _THIS_FILE.parent / "logs",
        Path.cwd() / "logs",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    return candidates[0]


BASE_URL = os.environ.get("RIT_BASE_URL", "http://flserver.rotman.utoronto.ca:16550/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "932VC8JQ")
USE_DMA_AUTH = os.environ.get("RIT_USE_DMA_AUTH", "1").strip().lower() in {"1", "true", "yes", "on"}
DMA_USER = os.environ.get("RIT_DMA_USER", "ZUAI-5").strip()
DMA_PASS = os.environ.get("RIT_DMA_PASS", "omega").strip()
DEFAULT_LOG_DIR = Path(os.environ.get("RIT_OB_LOG_DIR", str(_default_log_dir())).strip()).expanduser()
DEFAULT_TIMEOUT_SECS = float(os.environ.get("RIT_OB_TIMEOUT_SECS", "2.5"))

DEFAULT_TICKERS = ["TGX", "PHR", "BYL", "CLD", "GGD", "PNR", "FSR", "ATB", "SPK", "EEC"]


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def utc_iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except Exception:
        return 0


def _remaining_qty(level: Dict[str, Any]) -> int:
    qty = _safe_int(level.get("quantity"))
    filled = _safe_int(level.get("quantity_filled"))
    if qty == 0 and "qty" in level:
        qty = _safe_int(level.get("qty"))
    return max(0, qty - filled)


def _trim_book(book: Dict[str, Any], levels: int) -> Dict[str, Any]:
    def trim_side(side: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in list(side)[:levels]:
            out.append(
                {
                    "price": _safe_float(row.get("price")),
                    "remaining_qty": _remaining_qty(row),
                    "raw_quantity": _safe_int(row.get("quantity")),
                    "raw_filled": _safe_int(row.get("quantity_filled")),
                }
            )
        return out

    return {"bids": trim_side(book.get("bids") or []), "asks": trim_side(book.get("asks") or [])}


def _book_features(book: Dict[str, Any], levels: int) -> Dict[str, Any]:
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])
    if not bids or not asks:
        return {
            "has_book": False,
            "best_bid": None,
            "best_ask": None,
            "mid": None,
            "spread": None,
            "spread_bps": None,
            "top_bid_qty": 0,
            "top_ask_qty": 0,
            "top_imbalance": None,
            "depth_bid_n": 0,
            "depth_ask_n": 0,
            "depth_imbalance_n": None,
            "microprice": None,
            "locked_or_crossed": None,
        }

    best_bid = _safe_float(bids[0].get("price"))
    best_ask = _safe_float(asks[0].get("price"))
    if best_bid is None or best_ask is None:
        return {
            "has_book": False,
            "best_bid": None,
            "best_ask": None,
            "mid": None,
            "spread": None,
            "spread_bps": None,
            "top_bid_qty": 0,
            "top_ask_qty": 0,
            "top_imbalance": None,
            "depth_bid_n": 0,
            "depth_ask_n": 0,
            "depth_imbalance_n": None,
            "microprice": None,
            "locked_or_crossed": None,
        }

    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (spread / mid) * 10000.0 if mid > 0 else None

    top_bid_qty = _remaining_qty(bids[0])
    top_ask_qty = _remaining_qty(asks[0])
    top_sum = top_bid_qty + top_ask_qty
    top_imb = ((top_bid_qty - top_ask_qty) / top_sum) if top_sum > 0 else None

    depth_bid = sum(_remaining_qty(x) for x in bids[:levels])
    depth_ask = sum(_remaining_qty(x) for x in asks[:levels])
    depth_sum = depth_bid + depth_ask
    depth_imb = ((depth_bid - depth_ask) / depth_sum) if depth_sum > 0 else None

    microprice = None
    if top_sum > 0:
        microprice = (best_ask * top_bid_qty + best_bid * top_ask_qty) / top_sum

    return {
        "has_book": True,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "spread_bps": spread_bps,
        "top_bid_qty": top_bid_qty,
        "top_ask_qty": top_ask_qty,
        "top_imbalance": top_imb,
        "depth_bid_n": depth_bid,
        "depth_ask_n": depth_ask,
        "depth_imbalance_n": depth_imb,
        "microprice": microprice,
        "locked_or_crossed": best_ask <= best_bid,
    }


def _pctl(sorted_vals: List[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    q = max(0.0, min(1.0, q))
    idx = int(round((len(sorted_vals) - 1) * q))
    return sorted_vals[idx]


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "mean": None, "min": None, "p50": None, "p95": None, "max": None, "stdev": None}
    vals = sorted(values)
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    return {
        "count": len(vals),
        "mean": mean,
        "min": vals[0],
        "p50": _pctl(vals, 0.50),
        "p95": _pctl(vals, 0.95),
        "max": vals[-1],
        "stdev": math.sqrt(var),
    }


class AsyncRITClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_s: float = 1.5,
        pool_limit: int = 64,
        use_dma_auth: bool = False,
        dma_user: str = "",
        dma_pass: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.pool_limit = pool_limit
        self.use_dma_auth = use_dma_auth
        self.dma_user = dma_user
        self.dma_pass = dma_pass
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AsyncRITClient":
        connector = aiohttp.TCPConnector(limit=self.pool_limit, ttl_dns_cache=300, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        if self.use_dma_auth:
            creds = f"{self.dma_user}:{self.dma_pass}"
            b64_creds = base64.b64encode(creds.encode()).decode()
            headers = {"Authorization": f"Basic {b64_creds}"}
        else:
            headers = {"X-API-key": self.api_key}
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            raise_for_status=False,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _request(self, method: str, path: str, params: Optional[dict] = None, retries: int = 4) -> Any:
        if self.session is None:
            raise RuntimeError("Client not initialized")

        backoff = 0.03
        url = self.base_url + path
        last_err: Optional[str] = None
        for attempt in range(retries):
            try:
                async with self.session.request(method=method, url=url, params=params) as resp:
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        sleep_s = float(retry_after) if retry_after else backoff
                        await asyncio.sleep(max(0.01, sleep_s))
                        backoff = min(0.8, backoff * 2.0)
                        last_err = f"429 retry-after={retry_after}"
                        continue
                    if 500 <= resp.status < 600:
                        await asyncio.sleep(backoff)
                        backoff = min(0.8, backoff * 1.8)
                        last_err = f"{resp.status} server error"
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status} {path} {text}")
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_err = f"{type(exc).__name__}: {exc!r}"
                if attempt == retries - 1:
                    break
                await asyncio.sleep(backoff)
                backoff = min(0.8, backoff * 1.8)
        raise RuntimeError(f"REQUEST_FAIL method={method} path={path} retries={retries} last={last_err}")

    async def get_case(self) -> Dict[str, Any]:
        return await self._request("GET", "/case")

    async def get_book(self, ticker: str, levels: int) -> Dict[str, Any]:
        return await self._request("GET", "/securities/book", params={"ticker": ticker, "limit": levels})

    async def get_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"status": status} if status else None
        return await self._request("GET", "/orders", params=params)


class OrderBookResearchCapture:
    def __init__(self, args: argparse.Namespace, client: AsyncRITClient) -> None:
        self.args = args
        self.client = client
        self.snapshots: List[Dict[str, Any]] = []
        self.fill_events: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self._seen_fill_ids: set[int] = set()
        self._last_fill_poll_ts = 0.0
        self._active_started = False

    def _err(self, where: str, exc: Exception) -> None:
        self.errors.append(
            {
                "ts_utc": utc_iso_now(),
                "ts_epoch": round(time.time(), 6),
                "where": where,
                "error_type": type(exc).__name__,
                "error_repr": repr(exc),
            }
        )

    async def _maybe_poll_fills(self, now: float) -> None:
        if not self.args.track_fills:
            return
        if now - self._last_fill_poll_ts < self.args.fills_poll_every:
            return
        self._last_fill_poll_ts = now
        try:
            rows = await self.client.get_orders(status="TRANSACTED")
            for row in rows:
                oid = _safe_int(row.get("order_id"))
                if oid <= 0 or oid in self._seen_fill_ids:
                    continue
                self._seen_fill_ids.add(oid)
                self.fill_events.append(
                    {
                        "ts_utc": utc_iso_now(),
                        "ts_epoch": round(time.time(), 6),
                        "order_id": oid,
                        "ticker": str(row.get("ticker", "")).upper(),
                        "action": str(row.get("action", "")).upper(),
                        "quantity": _safe_int(row.get("quantity")),
                        "quantity_filled": _safe_int(row.get("quantity_filled")),
                        "price": _safe_float(row.get("price")),
                        "raw": row,
                    }
                )
        except Exception as exc:
            self._err("poll_fills", exc)

    async def _capture_one(self) -> Optional[Dict[str, Any]]:
        try:
            case = await self.client.get_case()
        except Exception as exc:
            self._err("get_case", exc)
            return None

        status = str(case.get("status", "")).upper()
        if status == "ACTIVE":
            self._active_started = True

        if (not self._active_started) and self.args.wait_active and (not self.args.capture_preactive):
            return {
                "ts_utc": utc_iso_now(),
                "ts_epoch": round(time.time(), 6),
                "case": case,
                "waiting_for_active": True,
                "tickers": {},
                "missing_tickers": self.args.tickers,
            }

        ticker_books: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []

        tasks = [self.client.get_book(t, self.args.levels) for t in self.args.tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, result in zip(self.args.tickers, results):
            if isinstance(result, Exception):
                missing.append(ticker)
                self._err(f"get_book:{ticker}", result)
                continue
            trimmed = _trim_book(result, self.args.levels)
            features = _book_features(result, self.args.levels)
            if not features["has_book"]:
                missing.append(ticker)
            ticker_books[ticker] = {"features": features, "book": trimmed}

        return {
            "ts_utc": utc_iso_now(),
            "ts_epoch": round(time.time(), 6),
            "case": case,
            "waiting_for_active": False,
            "tickers": ticker_books,
            "missing_tickers": missing,
        }

    async def run(self) -> Dict[str, Any]:
        print(f"{now_ts()} | starting capture tickers={','.join(self.args.tickers)} interval={self.args.interval:.3f}s", flush=True)
        started = time.time()
        capture_started_ts: Optional[float] = None

        while True:
            now = time.time()
            snap = await self._capture_one()
            if snap is not None:
                self.snapshots.append(snap)
                if capture_started_ts is None and not snap.get("waiting_for_active"):
                    capture_started_ts = now
                if snap.get("missing_tickers"):
                    print(f"{now_ts()} | BOOK_WARN missing={','.join(snap['missing_tickers'])}", flush=True)

            await self._maybe_poll_fills(now)

            # Stop conditions
            if self.args.duration > 0:
                base = capture_started_ts if capture_started_ts is not None else started
                if now - base >= self.args.duration:
                    break

            if self.args.until_case_end and self._active_started and snap is not None:
                status = str((snap.get("case") or {}).get("status", "")).upper()
                if status != "ACTIVE":
                    break

            await asyncio.sleep(max(0.01, self.args.interval))

        ended = time.time()
        payload = {
            "meta": {
                "captured_at_utc": utc_iso_now(),
                "duration_sec": round(ended - started, 3),
                "base_url": self.client.base_url,
            },
            "config": {
                "tickers": self.args.tickers,
                "levels": self.args.levels,
                "interval": self.args.interval,
                "duration": self.args.duration,
                "wait_active": self.args.wait_active,
                "capture_preactive": self.args.capture_preactive,
                "until_case_end": self.args.until_case_end,
                "track_fills": self.args.track_fills,
                "fills_poll_every": self.args.fills_poll_every,
            },
            "snapshots": self.snapshots,
            "fills": self.fill_events,
            "errors": self.errors,
        }
        payload["analysis"] = self._analyze(payload)
        return payload

    def _analyze(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        snaps = payload.get("snapshots") or []
        fills = payload.get("fills") or []

        per_ticker: Dict[str, Dict[str, Any]] = {}
        for ticker in self.args.tickers:
            mids: List[float] = []
            spreads: List[float] = []
            spread_bps: List[float] = []
            top_imb: List[float] = []
            depth_imb: List[float] = []
            locked = 0
            valid = 0
            quote_changes = 0
            prev_bid: Optional[float] = None
            prev_ask: Optional[float] = None

            for snap in snaps:
                row = (snap.get("tickers") or {}).get(ticker)
                if not row:
                    continue
                feat = row.get("features") or {}
                if not feat.get("has_book"):
                    continue

                valid += 1
                bid = _safe_float(feat.get("best_bid"))
                ask = _safe_float(feat.get("best_ask"))
                mid = _safe_float(feat.get("mid"))
                spr = _safe_float(feat.get("spread"))
                spr_bps = _safe_float(feat.get("spread_bps"))
                timb = _safe_float(feat.get("top_imbalance"))
                dimb = _safe_float(feat.get("depth_imbalance_n"))

                if bid is not None and ask is not None and prev_bid is not None and prev_ask is not None:
                    if (bid != prev_bid) or (ask != prev_ask):
                        quote_changes += 1
                if bid is not None:
                    prev_bid = bid
                if ask is not None:
                    prev_ask = ask

                if mid is not None:
                    mids.append(mid)
                if spr is not None:
                    spreads.append(spr)
                if spr_bps is not None:
                    spread_bps.append(spr_bps)
                if timb is not None:
                    top_imb.append(timb)
                if dimb is not None:
                    depth_imb.append(dimb)
                if feat.get("locked_or_crossed"):
                    locked += 1

            mid_rets: List[float] = []
            for i in range(1, len(mids)):
                if mids[i - 1] > 0:
                    mid_rets.append((mids[i] - mids[i - 1]) / mids[i - 1])

            per_ticker[ticker] = {
                "snapshot_count": len(snaps),
                "valid_book_count": valid,
                "book_coverage_ratio": (valid / len(snaps)) if snaps else None,
                "quote_change_rate": (quote_changes / max(1, valid - 1)) if valid > 1 else None,
                "locked_or_crossed_count": locked,
                "spread_stats": _stats(spreads),
                "spread_bps_stats": _stats(spread_bps),
                "top_imbalance_stats": _stats(top_imb),
                "depth_imbalance_stats": _stats(depth_imb),
                "mid_return_stats": _stats(mid_rets),
            }

        # Optional fill-impact estimate: did mid move in trade direction immediately after our fills?
        impact_rows: List[Dict[str, Any]] = []
        mids_by_ticker: Dict[str, List[Tuple[float, float]]] = {}
        for ticker in self.args.tickers:
            seq: List[Tuple[float, float]] = []
            for snap in snaps:
                row = (snap.get("tickers") or {}).get(ticker)
                if not row:
                    continue
                mid = _safe_float((row.get("features") or {}).get("mid"))
                ts = _safe_float(snap.get("ts_epoch"))
                if mid is None or ts is None:
                    continue
                seq.append((ts, mid))
            mids_by_ticker[ticker] = seq

        for fill in fills:
            ticker = str(fill.get("ticker", "")).upper()
            action = str(fill.get("action", "")).upper()
            ts = _safe_float(fill.get("ts_epoch"))
            if ticker not in mids_by_ticker or ts is None:
                continue
            seq = mids_by_ticker[ticker]
            if not seq:
                continue

            pre_mid = None
            post_mid = None
            for t, m in seq:
                if t <= ts:
                    pre_mid = m
                elif t > ts:
                    post_mid = m
                    break
            if pre_mid is None or post_mid is None:
                continue

            raw_delta = post_mid - pre_mid
            signed_delta = None
            if action == "BUY":
                signed_delta = raw_delta
            elif action == "SELL":
                signed_delta = -raw_delta

            impact_rows.append(
                {
                    "order_id": fill.get("order_id"),
                    "ticker": ticker,
                    "action": action,
                    "pre_mid": pre_mid,
                    "post_mid": post_mid,
                    "raw_mid_delta": raw_delta,
                    "signed_mid_delta": signed_delta,
                }
            )

        signed = [x["signed_mid_delta"] for x in impact_rows if x.get("signed_mid_delta") is not None]
        impact_summary = {
            "fill_count_considered": len(impact_rows),
            "signed_mid_delta_stats": _stats([float(x) for x in signed]) if signed else _stats([]),
            "suggested_no_impact_hint": None,
        }
        if signed:
            mean_abs = abs(float(impact_summary["signed_mid_delta_stats"]["mean"] or 0.0))
            impact_summary["suggested_no_impact_hint"] = (len(signed) >= 20 and mean_abs <= 0.01)

        missing_counts: Dict[str, int] = {t: 0 for t in self.args.tickers}
        for snap in snaps:
            for t in snap.get("missing_tickers") or []:
                missing_counts[t] = missing_counts.get(t, 0) + 1

        return {
            "snapshot_count": len(snaps),
            "fill_event_count": len(fills),
            "error_count": len(payload.get("errors") or []),
            "missing_book_counts": missing_counts,
            "per_ticker": per_ticker,
            "fill_impact": impact_summary,
            "interesting_checks": [
                "spread regime by ticker (mean/p95 spread and spread_bps)",
                "book health (coverage ratio, missing counts, locked/crossed count)",
                "liquidity pressure (top/depth imbalance distributions)",
                "quote refresh speed (quote_change_rate)",
                "price stability (mid_return_stats)",
                "self-impact hint from fills (signed_mid_delta_stats)",
            ],
        }


def _default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_LOG_DIR / f"orderbook_research_capture_{stamp}.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture and analyze RIT order book structure into one JSON file.")
    p.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS, help="Tickers to poll.")
    p.add_argument("--levels", type=int, default=10, help="Depth levels to request from /securities/book.")
    p.add_argument("--interval", type=float, default=0.10, help="Polling interval in seconds.")
    p.add_argument("--duration", type=float, default=300.0, help="Capture duration in seconds (0 = unlimited until case-end).")
    p.add_argument("--until-case-end", action="store_true", help="Stop when case leaves ACTIVE after it has started.")
    p.add_argument("--wait-active", action=argparse.BooleanOptionalAction, default=True, help="Wait for ACTIVE case before capture.")
    p.add_argument("--capture-preactive", action="store_true", help="Also record snapshots before ACTIVE.")
    p.add_argument("--track-fills", action="store_true", help="Poll transacted orders and estimate fill impact.")
    p.add_argument("--fills-poll-every", type=float, default=0.5, help="Polling interval for /orders?status=TRANSACTED.")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECS, help="HTTP timeout seconds.")
    p.add_argument("--pool-limit", type=int, default=64, help="aiohttp connector limit.")
    p.add_argument("--output", type=str, default="", help="Output JSON path.")
    p.add_argument("--allow-empty", action="store_true", help="Do not fail when capture has zero snapshots.")
    return p.parse_args()


async def _main_async() -> int:
    args = parse_args()
    if args.output:
        out_path = Path(args.output).expanduser()
        if not out_path.is_absolute():
            out_path = DEFAULT_LOG_DIR / out_path
    else:
        out_path = _default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not API_KEY and not USE_DMA_AUTH:
        raise RuntimeError("Set RIT_API_KEY or enable DMA auth")

    auth_mode = "DMA_BASIC" if USE_DMA_AUTH else "X_API_KEY"
    print(
        f"{now_ts()} | config base_url={BASE_URL} auth={auth_mode} timeout={args.timeout:.2f}s "
        f"output={out_path}",
        flush=True,
    )
    async with AsyncRITClient(
        api_key=API_KEY,
        base_url=BASE_URL,
        timeout_s=args.timeout,
        pool_limit=args.pool_limit,
        use_dma_auth=USE_DMA_AUTH,
        dma_user=DMA_USER,
        dma_pass=DMA_PASS,
    ) as client:
        runner = OrderBookResearchCapture(args, client)
        payload = await runner.run()

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"{now_ts()} | saved {out_path}", flush=True)
    analysis = payload.get("analysis") or {}
    print(
        f"{now_ts()} | snapshots={analysis.get('snapshot_count')} "
        f"errors={analysis.get('error_count')} fills={analysis.get('fill_event_count')}",
        flush=True,
    )
    if int(analysis.get("snapshot_count") or 0) <= 0 and int(analysis.get("error_count") or 0) > 0 and (not args.allow_empty):
        first_err = ((payload.get("errors") or [{}])[0] or {})
        print(
            f"{now_ts()} | NO_DATA_FAIL where={first_err.get('where')} "
            f"type={first_err.get('error_type')} err={first_err.get('error_repr')}",
            flush=True,
        )
        return 2
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_main_async()))
    except KeyboardInterrupt:
        print(f"{now_ts()} | interrupted", flush=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
