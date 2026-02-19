#!/usr/bin/env python3
"""Async FinBERT-driven merger arbitrage bot for RIT REST API.

Key differences vs sync version:
- Uses aiohttp + asyncio for non-blocking API I/O.
- Persistent connection pooling.
- Concurrent market-data and paired-order flows.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
import base64

BASE_URL = os.environ.get("RIT_BASE_URL", "http://flserver.rotman.utoronto.ca:16550/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "932VC8JQ")
USE_DMA_AUTH = os.environ.get("RIT_USE_DMA_AUTH", "1").strip().lower() in {"1", "true", "yes", "on"}
DMA_USER = os.environ.get("RIT_DMA_USER", "ZUAI-5").strip()
DMA_PASS = os.environ.get("RIT_DMA_PASS", "omega").strip()


def env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _find_repo_root(start: Path) -> Path:
    for p in [start] + list(start.parents):
        if (p / ".git").exists():
            return p
    return start.parent


def _looks_like_hf_model_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and (path / "config.json").exists()


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    seen = set()
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


_THIS_FILE = Path(__file__).resolve()
BOT_ROOT = _THIS_FILE.parent
REPO_ROOT = _find_repo_root(_THIS_FILE)

DEFAULT_FINBERT_ONNX_MODEL = BOT_ROOT / "finbert_hft" / "model_opt_int8.onnx"
DEFAULT_FINBERT_TOKENIZER_DIR = BOT_ROOT / "finbert_hft" / "local_finbert"


def _detect_finbert_assets(onnx_hint: str, tokenizer_hint: str) -> Tuple[Optional[Path], Optional[Path], str]:
    onnx_candidates = _dedupe_paths(
        [
            Path(onnx_hint).expanduser(),
            DEFAULT_FINBERT_ONNX_MODEL,
            BOT_ROOT / "finbert_hft" / "output" / "model_opt_int8.onnx",
            BOT_ROOT / "finbert_hft" / "onnx" / "model_opt_int8.onnx",
            REPO_ROOT / "ready_bots" / "finbert_hft" / "model_opt_int8.onnx",
        ]
    )
    onnx_path = next((p for p in onnx_candidates if p.exists() and p.is_file()), None)
    if onnx_path is None:
        return None, None, "No ONNX model found. Checked: " + ", ".join(str(p) for p in onnx_candidates)

    tokenizer_candidates: List[Path] = [Path(tokenizer_hint).expanduser()]
    manifest = onnx_path.parent / "export_manifest.json"
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            model_dir = payload.get("model_dir")
            if model_dir:
                tokenizer_candidates.append(Path(model_dir).expanduser())
        except Exception:
            pass

    tokenizer_candidates.extend(
        [
            DEFAULT_FINBERT_TOKENIZER_DIR,
            BOT_ROOT / "finbert_hft" / "model",
            BOT_ROOT / "finbert_hft",
            REPO_ROOT / "ready_bots" / "finbert_hft" / "model",
            REPO_ROOT / "ready_bots" / "finbert_hft",
        ]
    )
    tokenizer_candidates = _dedupe_paths(tokenizer_candidates)
    tokenizer_path = next((p for p in tokenizer_candidates if _looks_like_hf_model_dir(p)), None)
    if tokenizer_path is None:
        return onnx_path, None, "Tokenizer/model dir not found. Checked: " + ", ".join(str(p) for p in tokenizer_candidates)

    return onnx_path, tokenizer_path, "ok"


# Logging
WRITE_RUN_JSON = env_bool("RIT_MA_WRITE_RUN_JSON", "1")
RUN_LOG_DIR = os.environ.get("RIT_MA_LOG_DIR", str(BOT_ROOT / "logs"))
RUN_LOG_JSON_PATH = os.environ.get("RIT_MA_LOG_JSON_PATH", "").strip()
RUN_RECORDER: Optional["RunRecorder"] = None

# Timing
NEWS_POLL_SECS = float(os.environ.get("RIT_MA_NEWS_POLL_SECS", "0.15"))
TRADE_LOOP_SECS = float(os.environ.get("RIT_MA_TRADE_LOOP_SECS", "0.12"))
CASE_POLL_SECS = float(os.environ.get("RIT_MA_CASE_POLL_SECS", "0.25"))
TRADE_COOLDOWN_SECS = float(os.environ.get("RIT_MA_TRADE_COOLDOWN_SECS", "0.25"))
SNAPSHOT_SECS = float(os.environ.get("RIT_MA_SNAPSHOT_SECS", "2.0"))
INIT_WARMUP_SECS = float(os.environ.get("RIT_MA_INIT_WARMUP_SECS", "3.5"))
INIT_SNAPSHOTS = int(os.environ.get("RIT_MA_INIT_SNAPSHOTS", "8"))
INIT_SAMPLE_INTERVAL_SECS = float(os.environ.get("RIT_MA_INIT_SAMPLE_INTERVAL_SECS", "0.25"))

# Execution
MAX_ORDER_SIZE = 5000
BASE_ORDER_QTY = int(os.environ.get("RIT_MA_BASE_ORDER_QTY", "1200"))
MIN_ORDER_QTY = int(os.environ.get("RIT_MA_MIN_ORDER_QTY", "400"))
ORDER_STEP = int(os.environ.get("RIT_MA_ORDER_STEP", "100"))
MARKETABLE_LIMIT_OFFSET = float(os.environ.get("RIT_MA_LIMIT_OFFSET", "0.02"))
PING_AT_TOUCH = env_bool("RIT_MA_PING_AT_TOUCH", "1")
SIMULTANEOUS_LEGS = env_bool("RIT_MA_SIMULTANEOUS_LEGS", "1")
ENABLE_IOC_EMULATION = env_bool("RIT_MA_ENABLE_IOC_EMULATION", "1")
IOC_CANCEL_SECS = float(os.environ.get("RIT_MA_IOC_CANCEL_SECS", "0.20"))
STALE_ORDER_SECS = float(os.environ.get("RIT_MA_STALE_ORDER_SECS", "0.80"))
STALE_CANCEL_CHECK_SECS = float(os.environ.get("RIT_MA_STALE_CANCEL_CHECK_SECS", "0.15"))

# Thresholds
COMMISSION_PER_SHARE = float(os.environ.get("RIT_MA_COMMISSION_PER_SHARE", "0.02"))
MIN_ENTRY_THRESHOLD = float(os.environ.get("RIT_MA_MIN_ENTRY_THRESHOLD", "0.12"))
ENTRY_MARGIN_PER_SHARE = float(os.environ.get("RIT_MA_ENTRY_MARGIN", "0.04"))
EXIT_BUFFER = float(os.environ.get("RIT_MA_EXIT_BUFFER", "0.01"))
STOP_BUFFER = float(os.environ.get("RIT_MA_STOP_BUFFER", "0.30"))
MAX_HOLD_SECS = float(os.environ.get("RIT_MA_MAX_HOLD_SECS", "60.0"))
TIME_REDUCE_FRACTION = float(os.environ.get("RIT_MA_TIME_REDUCE_FRACTION", "0.55"))
RISK_REDUCE_COOLDOWN_SECS = float(os.environ.get("RIT_MA_RISK_REDUCE_COOLDOWN_SECS", "2.00"))
STOP_REENTRY_COOLDOWN_SECS = float(os.environ.get("RIT_MA_STOP_REENTRY_COOLDOWN_SECS", "4.00"))
ADD_INVENTORY_SLOPE = float(os.environ.get("RIT_MA_ADD_INVENTORY_SLOPE", "1.20"))
ADD_GLOBAL_SLOPE = float(os.environ.get("RIT_MA_ADD_GLOBAL_SLOPE", "0.90"))

# Probability blending
NEWS_HALF_LIFE_SECS = float(os.environ.get("RIT_MA_NEWS_HALF_LIFE_SECS", "45.0"))
PROB_SMOOTH_ALPHA = float(os.environ.get("RIT_MA_PROB_SMOOTH_ALPHA", "0.25"))
P_NEWS_MIN = float(os.environ.get("RIT_MA_P_NEWS_MIN", "0.25"))
P_NEWS_MAX = float(os.environ.get("RIT_MA_P_NEWS_MAX", "0.88"))
P_NEWS_BASE = float(os.environ.get("RIT_MA_P_NEWS_BASE", "0.30"))
P_NEWS_EVENT_WEIGHT = float(os.environ.get("RIT_MA_P_NEWS_EVENT_WEIGHT", "0.35"))
P_NEWS_RECENCY_WEIGHT = float(os.environ.get("RIT_MA_P_NEWS_RECENCY_WEIGHT", "0.35"))
EVENT_MIN_STRENGTH = float(os.environ.get("RIT_MA_EVENT_MIN_STRENGTH", "0.18"))
DISLOCATION_GATE = float(os.environ.get("RIT_MA_DISLOCATION_GATE", "0.08"))
EVENT_WINDOW_SECS = float(os.environ.get("RIT_MA_EVENT_WINDOW_SECS", "18.0"))
MIN_NEWS_DELTA = float(os.environ.get("RIT_MA_MIN_NEWS_DELTA", "0.06"))
ALT_POS_SHORT_LOCKOUT_SECS = float(os.environ.get("RIT_MA_ALT_POS_SHORT_LOCKOUT_SECS", "45.0"))

# News jump model
NEWS_BASE_MAG = float(os.environ.get("RIT_MA_NEWS_BASE_MAG", "0.03"))
NEWS_MAX_EXTRA_MAG = float(os.environ.get("RIT_MA_NEWS_MAX_EXTRA_MAG", "0.18"))
NEWS_STRENGTH_POWER = float(os.environ.get("RIT_MA_NEWS_STRENGTH_POWER", "1.35"))

# Risk limits
GROSS_LIMIT = 100_000
NET_LIMIT = 50_000
RISK_BUFFER = float(os.environ.get("RIT_MA_RISK_BUFFER", "0.98"))
PER_DEAL_TARGET_MAX = int(os.environ.get("RIT_MA_PER_DEAL_TARGET_MAX", "25000"))
PER_DEAL_ACQ_CAP_MULT = float(os.environ.get("RIT_MA_PER_DEAL_ACQ_CAP_MULT", "1.30"))
HEDGE_MIN_TOP_SIZE = int(os.environ.get("RIT_MA_HEDGE_MIN_TOP_SIZE", "600"))
HEDGE_TOP_BOOK_MULT = float(os.environ.get("RIT_MA_HEDGE_TOP_BOOK_MULT", "1.15"))
CAPACITY_RECYCLE_TRIGGER_UTIL = float(os.environ.get("RIT_MA_CAPACITY_RECYCLE_TRIGGER_UTIL", "0.95"))
CAPACITY_RECYCLE_TARGET_UTIL = float(os.environ.get("RIT_MA_CAPACITY_RECYCLE_TARGET_UTIL", "0.88"))
CAPACITY_RECYCLE_REDUCE_FRACTION = float(os.environ.get("RIT_MA_CAPACITY_RECYCLE_REDUCE_FRACTION", "0.50"))

# FinBERT
ENABLE_MANUAL_OVERRIDE = env_bool("RIT_MA_ENABLE_MANUAL_OVERRIDE", "0")
USE_FINBERT = env_bool("RIT_MA_USE_FINBERT", "1")
FINBERT_ONNX_MODEL = os.environ.get("RIT_MA_FINBERT_ONNX_MODEL", str(DEFAULT_FINBERT_ONNX_MODEL)).strip()
FINBERT_TOKENIZER_DIR = os.environ.get("RIT_MA_FINBERT_TOKENIZER_DIR", str(DEFAULT_FINBERT_TOKENIZER_DIR)).strip()
FINBERT_MAX_LENGTH = int(os.environ.get("RIT_MA_FINBERT_MAX_LENGTH", "128"))
FINBERT_POS_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_POS_THRESHOLD", "0.55"))
FINBERT_NEG_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_NEG_THRESHOLD", "0.55"))
FINBERT_GAP_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_GAP_THRESHOLD", "0.06"))
FINBERT_SEV_MEDIUM = float(os.environ.get("RIT_MA_FINBERT_SEV_MEDIUM", "0.63"))
FINBERT_SEV_LARGE = float(os.environ.get("RIT_MA_FINBERT_SEV_LARGE", "0.79"))
FINBERT_CATEGORY_FALLBACK = os.environ.get("RIT_MA_FINBERT_CATEGORY_FALLBACK", "FIN").strip().upper()

if not USE_FINBERT:
    raise RuntimeError("This async alpha strategy is FinBERT-only. Set RIT_MA_USE_FINBERT=1.")


DEALS = {
    "D1": {"target": "TGX", "acquirer": "PHR", "structure": "cash", "cash_terms": 50.0, "ratio": 0.0, "p0": 0.70, "deal_mult": 1.00},
    "D2": {"target": "BYL", "acquirer": "CLD", "structure": "stock", "cash_terms": 0.0, "ratio": 0.75, "p0": 0.55, "deal_mult": 1.05},
    "D3": {"target": "GGD", "acquirer": "PNR", "structure": "mixed", "cash_terms": 33.0, "ratio": 0.20, "p0": 0.50, "deal_mult": 1.10},
    "D4": {"target": "FSR", "acquirer": "ATB", "structure": "cash", "cash_terms": 40.0, "ratio": 0.0, "p0": 0.38, "deal_mult": 1.30},
    "D5": {"target": "SPK", "acquirer": "EEC", "structure": "stock", "cash_terms": 0.0, "ratio": 1.20, "p0": 0.45, "deal_mult": 1.15},
}

SEVERITY_MULT = {"S": 0.75, "M": 1.00, "L": 1.35}
CATEGORY_MULT = {"REG": 1.30, "FIN": 1.05, "SHR": 0.85, "ALT": 1.45, "PRC": 0.75}

CATEGORY_RE = re.compile(r"\b(REG|FIN|SHR|ALT|PRC)\b", re.IGNORECASE)
DEAL_RE = re.compile(r"\bD([1-5])\b", re.IGNORECASE)

CATEGORY_KEYWORDS = {
    "REG": ["regulator", "regulatory", "antitrust", "approval", "clearance", "ftc", "doj", "injunction"],
    "FIN": ["financing", "funding", "credit", "loan", "debt", "bridge", "liquidity", "covenant"],
    "SHR": ["shareholder", "board", "vote", "proxy", "activist", "committee"],
    "ALT": ["competing bid", "rival bid", "alternative", "counterbid", "higher offer", "topping bid"],
    "PRC": ["timeline", "delay", "extended", "deadline", "process", "condition"],
}

ENTITY_TO_DEAL = {
    "TARGENIX": "D1", "TGX": "D1", "PHARMACO": "D1", "PHR": "D1", "FDA": "D1",
    "BYTELAYER": "D2", "BYL": "D2", "CLOUDSYS": "D2", "CLD": "D2",
    "GREENGRID": "D3", "GGD": "D3", "PETRONORTH": "D3", "PNR": "D3", "FERC": "D3",
    "FINSURE": "D4", "FSR": "D4", "ATLAS BANK": "D4", "ATB": "D4", "FDIC": "D4",
    "SOLARPEAK": "D5", "SPK": "D5", "EASTENERGY": "D5", "EEC": "D5", "RENEWABLE": "D5",
}

AMBIGUOUS_PHRASES = {
    "ROUTINE LEGAL DILIGENCE",
    "AGENDA HAS NOT BEEN DISCLOSED",
    "MIXED CONCLUSIONS",
    "DIVERGENT VALUATION",
    "MAY REFLECT ROUTINE LEGAL DILIGENCE",
}
FLOW_DISLOCATION_PHRASES = {
    "PORTFOLIO REBALANCING",
    "TEMPORARY DISLOCATIONS",
    "MAY NOT REFLECT FUNDAMENTAL CHANGES",
}


def _phrase_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![A-Z0-9]){escaped}(?![A-Z0-9])")


ENTITY_PATTERNS = {term: _phrase_pattern(term) for term in ENTITY_TO_DEAL}


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def utc_iso_now() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


class RunRecorder:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.started_at_utc = utc_iso_now()
        self.started_epoch = time.time()
        self.lock = asyncio.Lock()
        self.events: List[dict] = []
        self.news: List[dict] = []
        self.context: Dict[str, object] = {}
        self.finalized = False
        self.output_path = self._resolve_output_path()

    def _resolve_output_path(self) -> Path:
        if RUN_LOG_JSON_PATH:
            return Path(RUN_LOG_JSON_PATH).expanduser()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Path(RUN_LOG_DIR).expanduser() / f"merger_arb_alpha_async_heat_{stamp}.json"

    async def set_context(self, context: Dict[str, object]) -> None:
        async with self.lock:
            self.context = context

    async def add_event(self, level: str, message: str, extra: Optional[dict] = None) -> None:
        async with self.lock:
            self.events.append(
                {
                    "ts_utc": utc_iso_now(),
                    "ts_epoch": round(time.time(), 6),
                    "level": level,
                    "message": message,
                    "extra": extra or {},
                }
            )

    async def add_news(
        self,
        item: dict,
        refs: List[str],
        category: Optional[str],
        direction: Optional[str],
        severity: Optional[str],
        applied: List[dict],
        skipped: bool,
        skip_reason: Optional[str] = None,
        classifier_meta: Optional[dict] = None,
    ) -> None:
        async with self.lock:
            self.news.append(
                {
                    "ts_utc": utc_iso_now(),
                    "news_id": item.get("news_id"),
                    "api_ticker": item.get("ticker"),
                    "headline": item.get("headline"),
                    "body": item.get("body"),
                    "refs": refs,
                    "classification": {
                        "category": category,
                        "direction": direction,
                        "severity": severity,
                    },
                    "classifier_meta": classifier_meta or {},
                    "applied_updates": applied,
                    "skipped": skipped,
                    "skip_reason": skip_reason,
                }
            )

    async def flush(self, summary: Dict[str, object]) -> str:
        async with self.lock:
            if self.finalized:
                return str(self.output_path)
            payload = {
                "meta": {
                    "started_at_utc": self.started_at_utc,
                    "ended_at_utc": utc_iso_now(),
                    "duration_sec": round(max(0.0, time.time() - self.started_epoch), 3),
                    "base_url": self.base_url,
                },
                "context": self.context,
                "summary": summary,
                "events": self.events,
                "news": self.news,
            }
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with self.output_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self.finalized = True
            return str(self.output_path)


def clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


async def log(msg: str, level: str = "INFO", extra: Optional[dict] = None) -> None:
    print(f"{now_ts()} | {msg}", flush=True)
    if RUN_RECORDER is not None:
        await RUN_RECORDER.add_event(level=level, message=msg, extra=extra)


def _resolve_finbert_module_path() -> Optional[Path]:
    here = Path(__file__).resolve()
    candidates = [
        here.parent / "finbert_hft" / "fast_inference.py",
        REPO_ROOT / "ready_bots" / "finbert_hft" / "fast_inference.py",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_finbert_trader_class():
    mod_path = _resolve_finbert_module_path()
    if mod_path is None:
        return None, "Could not find finbert_hft/fast_inference.py"
    try:
        spec = importlib.util.spec_from_file_location("finbert_hft_fast_inference_async", str(mod_path))
        if spec is None or spec.loader is None:
            return None, f"Failed to create import spec for {mod_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trader_cls = getattr(module, "FinBERTTrader", None)
        if trader_cls is None:
            return None, "FinBERTTrader class not found"
        return trader_cls, None
    except Exception as exc:
        return None, f"FinBERT module import failed: {exc}"


class AsyncRITClient:
    def __init__(self, api_key: str, base_url: str, timeout_s: float = 1.5, pool_limit: int = 64,
                 use_dma_auth: bool = False, dma_user: str = "", dma_pass: str = "") -> None:
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

    async def _request(self, method: str, path: str, params: Optional[dict] = None, retries: int = 4):
        if self.session is None:
            raise RuntimeError("Client session is not initialized. Use 'async with AsyncRITClient(...)'.")

        backoff = 0.03
        url = self.base_url + path
        for attempt in range(retries):
            try:
                async with self.session.request(method=method, url=url, params=params) as resp:
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        sleep_s = float(retry_after) if retry_after else backoff
                        await asyncio.sleep(max(0.01, sleep_s))
                        backoff = min(0.5, backoff * 2.0)
                        continue
                    if 500 <= resp.status < 600:
                        await asyncio.sleep(backoff)
                        backoff = min(0.6, backoff * 1.8)
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=text,
                            headers=resp.headers,
                        )
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(0.6, backoff * 1.8)

        raise RuntimeError("Unreachable request flow")

    async def get_case(self) -> dict:
        return await self._request("GET", "/case")

    async def get_news(self, since: Optional[int] = None, limit: Optional[int] = None) -> List[dict]:
        params = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/news", params=params)

    async def get_securities(self) -> List[dict]:
        return await self._request("GET", "/securities")

    async def get_book(self, ticker: str) -> dict:
        return await self._request("GET", "/securities/book", params={"ticker": ticker, "limit": 1})

    async def get_orders(self, status: Optional[str] = None) -> List[dict]:
        params = {"status": status} if status else None
        return await self._request("GET", "/orders", params=params)

    async def cancel_order(self, order_id: int) -> dict:
        return await self._request("DELETE", f"/orders/{order_id}")

    async def place_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> dict:
        params = {
            "ticker": ticker,
            "type": order_type,
            "quantity": quantity,
            "action": action,
        }
        if price is not None:
            params["price"] = price
        return await self._request("POST", "/orders", params=params, retries=3)


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def top_of_book_from_book(book: dict) -> Tuple[Optional[float], Optional[float], int, int]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None, None, 0, 0

    bid_px = _safe_float(bids[0].get("price"))
    ask_px = _safe_float(asks[0].get("price"))
    if bid_px is None or ask_px is None:
        return None, None, 0, 0

    bid_qty = max(0, _safe_int(bids[0].get("quantity")) - _safe_int(bids[0].get("quantity_filled")))
    ask_qty = max(0, _safe_int(asks[0].get("quantity")) - _safe_int(asks[0].get("quantity_filled")))
    return float(bid_px), float(ask_px), bid_qty, ask_qty


def deal_value(deal: dict, acquirer_price: float) -> float:
    structure = deal["structure"]
    cash = float(deal["cash_terms"])
    ratio = float(deal["ratio"])
    if structure == "cash":
        return cash
    if structure == "stock":
        return ratio * acquirer_price
    return cash + ratio * acquirer_price


def infer_standalone_value(target_start: float, p0: float, k0: float) -> float:
    if p0 >= 0.999:
        return target_start
    return (target_start - p0 * k0) / (1.0 - p0)


def classify_category(text: str) -> Optional[str]:
    text_upper = text.upper()
    m = CATEGORY_RE.search(text_upper)
    if m:
        return m.group(1).upper()

    text_lower = text.lower()
    best_cat = None
    best_score = 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(1 for w in words if w in text_lower)
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat


def phrase_hits(text: str, phrases: Iterable[str]) -> List[str]:
    text_upper = text.upper()
    hits: List[str] = []
    for phrase in phrases:
        if phrase in text_upper:
            hits.append(phrase)
    return sorted(set(hits))


def extract_referenced_deals(
    text: str,
    deal_tickers: Dict[str, str],
    include_entities: bool = True,
) -> Tuple[List[str], Dict[str, List[str]]]:
    text_upper = text.upper()
    refs = set()
    matched: Dict[str, List[str]] = {"deal_ids": [], "tickers": [], "entities": []}

    for m in DEAL_RE.finditer(text_upper):
        did = f"D{m.group(1)}"
        refs.add(did)
        matched["deal_ids"].append(did)

    for ticker, did in deal_tickers.items():
        if re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text_upper):
            refs.add(did)
            matched["tickers"].append(ticker)

    if include_entities:
        for term, did in ENTITY_TO_DEAL.items():
            if ENTITY_PATTERNS[term].search(text_upper):
                refs.add(did)
                matched["entities"].append(term)

    return sorted(refs), {k: sorted(set(v)) for k, v in matched.items()}


def compute_gross_net(positions: Dict[str, int]) -> Tuple[int, int]:
    gross = int(sum(abs(v) for v in positions.values()))
    net = int(abs(sum(positions.values())))
    return gross, net


def project_limits_ok(positions: Dict[str, int], deltas: Dict[str, int], gross_cap: int, net_cap: int) -> bool:
    projected = dict(positions)
    for ticker, delta in deltas.items():
        projected[ticker] = int(projected.get(ticker, 0) + delta)
    gross, net = compute_gross_net(projected)
    return gross <= int(gross_cap * RISK_BUFFER) and net <= int(net_cap * RISK_BUFFER)


def compute_transaction_friction(ratio: float, t_bid: float, t_ask: float, a_bid: float, a_ask: float) -> float:
    target_half = max(0.0, (t_ask - t_bid) / 2.0)
    acq_half = max(0.0, (a_ask - a_bid) / 2.0)
    commission = COMMISSION_PER_SHARE * (1.0 + ratio)
    marketable = MARKETABLE_LIMIT_OFFSET * (1.0 + ratio)
    return commission + target_half + ratio * acq_half + marketable


def inventory_adjusted_threshold(base: float, target_pos: int, action: str, gross_used: int, net_used: int) -> float:
    direction = 1 if action == "BUY" else -1
    reducing = target_pos * direction < 0
    if reducing:
        return max(0.03, base * 0.45)

    inv_util = min(1.0, abs(target_pos) / max(1.0, NET_LIMIT * 0.5))
    gross_util = min(1.0, gross_used / max(1.0, GROSS_LIMIT))
    net_util = min(1.0, net_used / max(1.0, NET_LIMIT))
    util = max(gross_util, net_util)
    return base * (1.0 + ADD_INVENTORY_SLOPE * inv_util + ADD_GLOBAL_SLOPE * util)


def close_qty_for_position(position_abs: int) -> int:
    qty = min(MAX_ORDER_SIZE, position_abs)
    qty = max(ORDER_STEP, qty - (qty % ORDER_STEP))
    return min(position_abs, qty)


def scaled_close_qty(position_abs: int, fraction: float) -> int:
    base = close_qty_for_position(position_abs)
    fraction = max(0.05, min(1.0, fraction))
    qty = int(base * fraction)
    qty = max(ORDER_STEP, qty - (qty % ORDER_STEP))
    return min(position_abs, qty)


def compute_hedge_close_qty(target_close_action: str, ratio: float, target_close_qty: int, acquirer_pos: int) -> int:
    desired = int(round(ratio * target_close_qty))
    if desired <= 0:
        return 0
    if target_close_action == "SELL":
        return min(desired, abs(acquirer_pos), MAX_ORDER_SIZE) if acquirer_pos < 0 else 0
    return min(desired, acquirer_pos, MAX_ORDER_SIZE) if acquirer_pos > 0 else 0


def per_deal_acq_cap(ratio: float) -> int:
    return max(PER_DEAL_TARGET_MAX, int(round(PER_DEAL_TARGET_MAX * max(1.0, ratio) * PER_DEAL_ACQ_CAP_MULT)))


def max_qty_for_position_cap(position: int, action: str, cap_abs: int) -> int:
    if action == "BUY":
        return max(0, cap_abs - position)
    return max(0, position + cap_abs)


def scale_target_qty(
    ratio: float,
    seed_qty: int,
    action: str,
    target_ticker: str,
    acquirer_ticker: str,
    positions: Dict[str, int],
    max_target_qty: Optional[int] = None,
) -> Tuple[int, int]:
    max_by_order = MAX_ORDER_SIZE
    if ratio > 0:
        max_by_order = min(max_by_order, int(MAX_ORDER_SIZE / ratio))
    if max_target_qty is not None:
        max_by_order = min(max_by_order, max_target_qty)
        if max_by_order <= 0:
            return 0, 0

    qty = max(MIN_ORDER_QTY, min(seed_qty, max_by_order))
    qty = max(MIN_ORDER_QTY, qty - (qty % ORDER_STEP))

    while qty >= MIN_ORDER_QTY:
        hedge_qty = int(round(ratio * qty))
        target_delta = qty if action == "BUY" else -qty
        hedge_delta = -hedge_qty if action == "BUY" else hedge_qty
        deltas = {target_ticker: target_delta}
        if hedge_qty > 0:
            deltas[acquirer_ticker] = hedge_delta
        if project_limits_ok(positions, deltas, GROSS_LIMIT, NET_LIMIT):
            return qty, hedge_qty
        qty -= ORDER_STEP

    return 0, 0


@dataclass
class DealState:
    prob_news: float
    prob_live: float
    standalone_value: float
    last_news_update_ts: float = 0.0
    last_news_id: int = 0
    last_news_delta_abs: float = 0.0
    last_event_strength: float = 0.0
    last_trade_ts: float = 0.0
    hold_start_ts: float = 0.0
    last_risk_reduce_ts: float = 0.0
    last_stop_ts: float = 0.0
    last_entry_news_id: int = 0
    alt_short_lock_until_ts: float = 0.0


@dataclass
class TrackedOrder:
    ts: float
    ticker: str
    reason: str
    action: str
    quantity: int
    pending_delta: int
    cancel_after_ts: float = 0.0


class MergerArbAlphaAsyncBot:
    def __init__(self, client: AsyncRITClient):
        self.client = client
        self.running = True
        self.last_news_id = 0
        self.last_snapshot_ts = 0.0

        self.state_lock = asyncio.Lock()
        self.order_lock = asyncio.Lock()
        self.deal_states: Dict[str, DealState] = {}
        self.open_order_meta: Dict[int, TrackedOrder] = {}
        self.pending_pos_deltas: Dict[str, int] = {}
        self.last_stale_check_ts = 0.0

        self.finbert = None
        self.finbert_enabled = False
        self.finbert_model_path: Optional[str] = None
        self.finbert_tokenizer_path: Optional[str] = None

        self.deal_ticker_to_id: Dict[str, str] = {}
        for did, deal in DEALS.items():
            self.deal_ticker_to_id[deal["target"].upper()] = did
            self.deal_ticker_to_id[deal["acquirer"].upper()] = did
        self.trade_tickers = sorted(self.deal_ticker_to_id.keys())

    async def initialize(self) -> None:
        case = await self.client.get_case()
        if case.get("status") != "ACTIVE":
            await log(f"Case status={case.get('status')} waiting ACTIVE")
            while self.running:
                await asyncio.sleep(CASE_POLL_SECS)
                case = await self.client.get_case()
                if case.get("status") == "ACTIVE":
                    break

        if INIT_WARMUP_SECS > 0:
            await log(f"INIT warmup {INIT_WARMUP_SECS:.1f}s")
            await asyncio.sleep(INIT_WARMUP_SECS)

        mids = await self._average_snapshot_mid(self.trade_tickers, INIT_SNAPSHOTS, INIT_SAMPLE_INTERVAL_SECS)
        async with self.state_lock:
            for did, deal in DEALS.items():
                target = deal["target"].upper()
                acq = deal["acquirer"].upper()
                p0 = float(deal["p0"])
                k0 = deal_value(deal, mids[acq])
                v0 = infer_standalone_value(mids[target], p0, k0)
                self.deal_states[did] = DealState(prob_news=p0, prob_live=p0, standalone_value=v0)
                await log(
                    f"INIT {did} target={target} mid0={mids[target]:.2f} acq={acq} mid0={mids[acq]:.2f} "
                    f"K0={k0:.2f} V0={v0:.2f} p0={p0:.3f}"
                )

    async def _fetch_book_one(self, ticker: str) -> Tuple[str, Optional[Tuple[float, float, float, int, int]]]:
        try:
            b = await self.client.get_book(ticker)
            bid, ask, bid_qty, ask_qty = top_of_book_from_book(b)
            if bid is None or ask is None:
                return ticker, None
            return ticker, (bid, ask, (bid + ask) / 2.0, bid_qty, ask_qty)
        except Exception:
            return ticker, None

    async def _fetch_books_parallel(self) -> Dict[str, Tuple[float, float, float, int, int]]:
        out: Dict[str, Tuple[float, float, float, int, int]] = {}
        results = await asyncio.gather(*[self._fetch_book_one(t) for t in self.trade_tickers], return_exceptions=False)
        for ticker, res in results:
            if res is not None:
                out[ticker] = res
        return out

    async def _snapshot_mid(self, tickers: Iterable[str]) -> Dict[str, float]:
        books = await asyncio.gather(*[self.client.get_book(t) for t in tickers])
        out: Dict[str, float] = {}
        for ticker, book in zip(tickers, books):
            bid, ask, _, _ = top_of_book_from_book(book)
            if bid is None or ask is None:
                raise RuntimeError(f"Missing bid/ask for {ticker} during initialization")
            out[ticker] = (bid + ask) / 2.0
        return out

    async def _average_snapshot_mid(self, tickers: Iterable[str], samples: int, sleep_s: float) -> Dict[str, float]:
        sums: Dict[str, float] = {t: 0.0 for t in tickers}
        count = 0
        for _ in range(max(1, samples)):
            mids = await self._snapshot_mid(tickers)
            for t, mid in mids.items():
                sums[t] += mid
            count += 1
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
        return {t: sums[t] / max(1, count) for t in sums}

    async def _safe_positions(self) -> Dict[str, int]:
        sec = await self.client.get_securities()
        return {s["ticker"].upper(): int(s.get("position", 0)) for s in sec}

    async def _initialize_finbert(self) -> None:
        model_path, tokenizer_path, note = _detect_finbert_assets(FINBERT_ONNX_MODEL, FINBERT_TOKENIZER_DIR)
        if model_path is None or tokenizer_path is None:
            raise RuntimeError(f"FinBERT assets missing: {note}")

        trader_cls, err = _load_finbert_trader_class()
        if trader_cls is None:
            raise RuntimeError(f"FinBERT module unavailable: {err}")

        try:
            self.finbert = trader_cls(
                onnx_model_path=str(model_path),
                tokenizer_dir=str(tokenizer_path),
                max_length=FINBERT_MAX_LENGTH,
            )
            self.finbert_enabled = True
            self.finbert_model_path = str(model_path)
            self.finbert_tokenizer_path = str(tokenizer_path)
            await log(
                f"FINBERT enabled model={model_path} tokenizer={tokenizer_path} "
                f"pos_thr={FINBERT_POS_THRESHOLD:.2f} neg_thr={FINBERT_NEG_THRESHOLD:.2f}"
            )
        except Exception as exc:
            raise RuntimeError(f"FinBERT init failed: {exc}") from exc

    def _finbert_infer_sync(self, text: str) -> Tuple[Optional[str], str, float, Optional[dict]]:
        if self.finbert is None:
            return None, "S", 0.0, None
        try:
            probs = self.finbert.predict(text)
        except Exception as exc:
            return None, "S", 0.0, {"error": str(exc)}

        p_pos = float(probs.get("positive_probability", 0.0))
        p_neg = float(probs.get("negative_probability", 0.0))
        gap = abs(p_pos - p_neg)
        conf = max(p_pos, p_neg)

        direction = None
        if p_pos >= FINBERT_POS_THRESHOLD and (p_pos - p_neg) >= FINBERT_GAP_THRESHOLD:
            direction = "POS"
        elif p_neg >= FINBERT_NEG_THRESHOLD and (p_neg - p_pos) >= FINBERT_GAP_THRESHOLD:
            direction = "NEG"

        if conf >= FINBERT_SEV_LARGE:
            severity = "L"
        elif conf >= FINBERT_SEV_MEDIUM:
            severity = "M"
        else:
            severity = "S"

        strength = clamp((conf - 0.5) * 1.4 + gap * 0.9, 0.0, 1.0)
        meta = {
            "positive_probability": round(p_pos, 6),
            "negative_probability": round(p_neg, 6),
            "gap": round(gap, 6),
            "confidence": round(conf, 6),
            "strength": round(strength, 6),
        }
        return direction, severity, strength, meta

    async def _finbert_infer(self, text: str) -> Tuple[Optional[str], str, float, Optional[dict]]:
        return await asyncio.to_thread(self._finbert_infer_sync, text)

    def _news_delta(self, direction: str, severity: str, strength: float, category: str, deal_mult: float, current_p: float) -> float:
        sign = 1.0 if direction == "POS" else -1.0
        sev_mult = SEVERITY_MULT.get(severity, 1.0)
        cat_mult = CATEGORY_MULT.get(category, 1.0)
        mag = NEWS_BASE_MAG + NEWS_MAX_EXTRA_MAG * (max(0.0, strength) ** NEWS_STRENGTH_POWER)
        raw = sign * mag * sev_mult * cat_mult * float(deal_mult)

        headroom = (1.0 - current_p) if sign > 0 else current_p
        damp = 0.35 + 0.65 * clamp(headroom, 0.0, 1.0)
        return raw * damp

    def _marketable_limit_price(self, action: str, bid: float, ask: float) -> float:
        if PING_AT_TOUCH:
            return round(ask if action == "BUY" else bid, 2)
        if action == "BUY":
            return round(ask + MARKETABLE_LIMIT_OFFSET, 2)
        return round(max(0.01, bid - MARKETABLE_LIMIT_OFFSET), 2)

    async def _apply_pending_delta_locked(self, ticker: str, delta: int) -> None:
        if delta == 0:
            return
        cur = int(self.pending_pos_deltas.get(ticker, 0))
        nxt = cur + int(delta)
        if nxt == 0:
            self.pending_pos_deltas.pop(ticker, None)
        else:
            self.pending_pos_deltas[ticker] = nxt

    async def _track_order(self, response: Optional[dict], ticker: str, reason: str, action: str, quantity: int) -> None:
        if not response:
            return
        order_id = response.get("order_id")
        if order_id is None:
            return
        try:
            oid = int(order_id)
        except Exception:
            return

        qty_resp = response.get("quantity")
        tracked_qty = _safe_int(qty_resp) if qty_resp is not None else int(quantity)
        if tracked_qty <= 0:
            tracked_qty = int(quantity)

        pending_delta = tracked_qty if action == "BUY" else -tracked_qty
        cancel_after_ts = time.time() + max(0.05, IOC_CANCEL_SECS) if ENABLE_IOC_EMULATION else 0.0

        meta = TrackedOrder(
            ts=time.time(),
            ticker=ticker,
            reason=reason,
            action=action,
            quantity=tracked_qty,
            pending_delta=pending_delta,
            cancel_after_ts=cancel_after_ts,
        )

        async with self.order_lock:
            self.open_order_meta[oid] = meta
            await self._apply_pending_delta_locked(ticker, pending_delta)

    async def _effective_positions(self, live_positions: Dict[str, int]) -> Dict[str, int]:
        merged = {k.upper(): int(v) for k, v in live_positions.items()}
        async with self.order_lock:
            for ticker, delta in self.pending_pos_deltas.items():
                merged[ticker] = int(merged.get(ticker, 0) + delta)
        return merged

    async def _cancel_stale_orders(self, now: float) -> None:
        if now - self.last_stale_check_ts < STALE_CANCEL_CHECK_SECS:
            return
        self.last_stale_check_ts = now

        try:
            open_orders = await self.client.get_orders(status="OPEN")
        except Exception as exc:
            await log(f"ORDERS_WARN open order poll failed: {exc}", level="WARN")
            return

        open_by_id: Dict[int, dict] = {}
        for order in open_orders:
            oid = order.get("order_id")
            if oid is None:
                continue
            try:
                open_by_id[int(oid)] = order
            except Exception:
                continue

        stale: List[Tuple[int, TrackedOrder, str]] = []
        async with self.order_lock:
            tracked = set(self.open_order_meta.keys())
            for oid in tracked - set(open_by_id.keys()):
                meta = self.open_order_meta.pop(oid, None)
                if meta is not None:
                    await self._apply_pending_delta_locked(meta.ticker, -meta.pending_delta)

            for oid, meta in list(self.open_order_meta.items()):
                open_order = open_by_id.get(oid)
                if open_order is None:
                    continue

                open_qty = _safe_int(open_order.get("quantity", meta.quantity))
                if open_qty <= 0:
                    open_qty = meta.quantity
                filled = _safe_int(open_order.get("quantity_filled"))
                remaining = max(0, open_qty - filled)
                expected_pending = remaining if meta.action == "BUY" else -remaining

                if expected_pending != meta.pending_delta:
                    await self._apply_pending_delta_locked(meta.ticker, expected_pending - meta.pending_delta)
                    meta.pending_delta = expected_pending
                    self.open_order_meta[oid] = meta

                is_stale = now - meta.ts >= STALE_ORDER_SECS
                is_ioc = ENABLE_IOC_EMULATION and meta.cancel_after_ts > 0 and now >= meta.cancel_after_ts
                if is_stale or is_ioc:
                    stale.append((oid, meta, "IOC_TIMEOUT" if is_ioc else "STALE"))

        for oid, meta, cancel_reason in stale:
            try:
                await self.client.cancel_order(oid)
                await log(
                    f"CANCEL order_id={oid} ticker={meta.ticker} reason={meta.reason} "
                    f"cancel_reason={cancel_reason} age={now - meta.ts:.2f}s"
                )
            except Exception:
                pass
            finally:
                async with self.order_lock:
                    active = self.open_order_meta.pop(oid, None)
                    if active is not None:
                        await self._apply_pending_delta_locked(active.ticker, -active.pending_delta)

    async def _submit_pair(
        self,
        deal_id: str,
        target: str,
        acquirer: str,
        signal_action: str,
        target_qty: int,
        hedge_qty: int,
        edge: float,
        p_star: float,
        t_bid: float,
        t_ask: float,
        a_bid: float,
        a_ask: float,
        reason: str,
    ) -> Tuple[bool, bool]:
        target_px = self._marketable_limit_price(signal_action, t_bid, t_ask)
        hedge_side = "SELL" if signal_action == "BUY" else "BUY"
        hedge_px = self._marketable_limit_price(hedge_side, a_bid, a_ask) if hedge_qty > 0 else None

        start = time.perf_counter()
        tgt_resp: Optional[dict] = None
        hedge_resp: Optional[dict] = None

        if hedge_qty > 0 and SIMULTANEOUS_LEGS:
            tgt_task = asyncio.create_task(
                self.client.place_order(
                    ticker=target,
                    action=signal_action,
                    quantity=target_qty,
                    order_type="LIMIT",
                    price=target_px,
                )
            )
            hedge_task = asyncio.create_task(
                self.client.place_order(
                    ticker=acquirer,
                    action=hedge_side,
                    quantity=hedge_qty,
                    order_type="LIMIT",
                    price=hedge_px,
                )
            )
            tgt_res, hedge_res = await asyncio.gather(tgt_task, hedge_task, return_exceptions=True)

            if isinstance(tgt_res, Exception):
                await log(
                    f"ORDER_FAIL {deal_id} reason={reason} target={target} side={signal_action} "
                    f"qty={target_qty} px={target_px:.2f} err={tgt_res}",
                    level="WARN",
                )
            else:
                tgt_resp = tgt_res
                await self._track_order(tgt_resp, target, reason, signal_action, target_qty)

            if isinstance(hedge_res, Exception):
                await log(
                    f"HEDGE_FAIL {deal_id} reason={reason} acq={acquirer} side={hedge_side} "
                    f"qty={hedge_qty} px={hedge_px:.2f} err={hedge_res}",
                    level="WARN",
                )
            else:
                hedge_resp = hedge_res
                await self._track_order(hedge_resp, acquirer, reason, hedge_side, hedge_qty)
        else:
            try:
                tgt_resp = await self.client.place_order(
                    ticker=target,
                    action=signal_action,
                    quantity=target_qty,
                    order_type="LIMIT",
                    price=target_px,
                )
                await self._track_order(tgt_resp, target, reason, signal_action, target_qty)
            except Exception as exc:
                await log(
                    f"ORDER_FAIL {deal_id} reason={reason} target={target} side={signal_action} "
                    f"qty={target_qty} px={target_px:.2f} err={exc}",
                    level="WARN",
                )
                return False, False

            if hedge_qty > 0:
                try:
                    hedge_resp = await self.client.place_order(
                        ticker=acquirer,
                        action=hedge_side,
                        quantity=hedge_qty,
                        order_type="LIMIT",
                        price=hedge_px,
                    )
                    await self._track_order(hedge_resp, acquirer, reason, hedge_side, hedge_qty)
                except Exception as exc:
                    await log(
                        f"HEDGE_FAIL {deal_id} reason={reason} acq={acquirer} side={hedge_side} "
                        f"qty={hedge_qty} px={hedge_px:.2f} err={exc}",
                        level="WARN",
                    )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        await log(
            f"TRADE {deal_id} reason={reason} side={signal_action} tgt={target} qty={target_qty}@{target_px:.2f} "
            f"hedge={acquirer}:{hedge_side if hedge_qty > 0 else 'NONE'}:{hedge_qty}"
            f"{f'@{hedge_px:.2f}' if hedge_qty > 0 and hedge_px is not None else ''} "
            f"edge={edge:.3f} p*={p_star:.3f} bid/ask={t_bid:.2f}/{t_ask:.2f} lat={elapsed_ms:.1f}ms "
            f"tgt_id={(tgt_resp or {}).get('order_id')} hedge_id={(hedge_resp or {}).get('order_id')}"
        )
        return tgt_resp is not None, (hedge_resp is not None if hedge_qty > 0 else False)

    async def _submit_single(
        self,
        deal_id: str,
        ticker: str,
        action: str,
        qty: int,
        bid: float,
        ask: float,
        reason: str,
    ) -> bool:
        if qty <= 0:
            return False
        px = self._marketable_limit_price(action, bid, ask)
        try:
            resp = await self.client.place_order(
                ticker=ticker,
                action=action,
                quantity=qty,
                order_type="LIMIT",
                price=px,
            )
            await self._track_order(resp, ticker, reason, action, qty)
            await log(f"TRADE {deal_id} reason={reason} side={action} single={ticker} qty={qty}@{px:.2f} id={resp.get('order_id')}")
            return True
        except Exception as exc:
            await log(
                f"ORDER_FAIL {deal_id} reason={reason} single={ticker} side={action} qty={qty} px={px:.2f} err={exc}",
                level="WARN",
            )
            return False

    def _cap_target_qty_by_hedge_liquidity(
        self,
        action: str,
        ratio: float,
        target_qty: int,
        hedge_qty: int,
        a_bid_qty: int,
        a_ask_qty: int,
    ) -> Tuple[int, int]:
        if ratio <= 0 or hedge_qty <= 0 or target_qty <= 0:
            return target_qty, hedge_qty

        hedge_side = "SELL" if action == "BUY" else "BUY"
        top_qty = a_bid_qty if hedge_side == "SELL" else a_ask_qty
        if top_qty < HEDGE_MIN_TOP_SIZE:
            return 0, 0

        max_hedge_by_top = int(top_qty * max(0.2, HEDGE_TOP_BOOK_MULT))
        max_hedge_by_top = max(0, max_hedge_by_top - (max_hedge_by_top % ORDER_STEP))
        if max_hedge_by_top < ORDER_STEP:
            return 0, 0

        capped_hedge = min(hedge_qty, max_hedge_by_top)
        max_target_by_hedge = int(capped_hedge / max(1e-9, ratio))
        max_target_by_hedge = max(0, max_target_by_hedge - (max_target_by_hedge % ORDER_STEP))
        capped_target = min(target_qty, max_target_by_hedge)
        if capped_target < MIN_ORDER_QTY:
            return 0, 0

        return capped_target, int(round(ratio * capped_target))

    def _update_prob_live(self, st: DealState, deal: dict, t_mid: float, a_mid: float, now: float) -> Tuple[float, float]:
        k_mid = deal_value(deal, a_mid)
        denom = k_mid - st.standalone_value
        if abs(denom) < 1e-9:
            p_impl = st.prob_news
        else:
            p_impl = clamp((t_mid - st.standalone_value) / denom, 0.0, 1.0)

        if st.last_news_update_ts > 0:
            age = max(0.0, now - st.last_news_update_ts)
            recency = math.exp(-age / max(1e-6, NEWS_HALF_LIFE_SECS))
        else:
            recency = 0.0

        w_news = clamp(
            P_NEWS_BASE + P_NEWS_EVENT_WEIGHT * st.last_event_strength + P_NEWS_RECENCY_WEIGHT * recency,
            P_NEWS_MIN,
            P_NEWS_MAX,
        )
        p_target = clamp(w_news * st.prob_news + (1.0 - w_news) * p_impl, 0.0, 1.0)
        p_live = clamp((1.0 - PROB_SMOOTH_ALPHA) * st.prob_live + PROB_SMOOTH_ALPHA * p_target, 0.0, 1.0)
        return p_live, p_impl

    async def news_worker(self) -> None:
        err_count = 0
        while self.running:
            try:
                news = await self.client.get_news(since=self.last_news_id, limit=40)
                if not news:
                    err_count = 0
                    await asyncio.sleep(NEWS_POLL_SECS)
                    continue

                for item in sorted(news, key=lambda x: x.get("news_id", 0)):
                    news_id = int(item.get("news_id", 0))
                    if news_id <= self.last_news_id:
                        continue

                    headline = str(item.get("headline") or "")
                    body = str(item.get("body") or "")
                    api_ticker = str(item.get("ticker") or "")
                    text = (headline + " " + body).strip()

                    refs_text, ref_text_meta = extract_referenced_deals(text, self.deal_ticker_to_id, include_entities=True)
                    refs_api, ref_api_meta = extract_referenced_deals(api_ticker, self.deal_ticker_to_id, include_entities=False)
                    refs = sorted(set(refs_text) | set(refs_api))

                    category = classify_category(text)
                    category_source = "explicit_or_keyword" if category else "none"
                    if category is None and FINBERT_CATEGORY_FALLBACK in CATEGORY_MULT:
                        category = FINBERT_CATEGORY_FALLBACK
                        category_source = "fallback"

                    direction, severity, strength, finbert_meta = await self._finbert_infer(text)
                    if finbert_meta is not None and "error" in finbert_meta:
                        await log(f"FINBERT inference error: {finbert_meta['error']}", level="WARN")

                    classifier_meta = {
                        "mode": "finbert_jump_blend_async",
                        "finbert": {
                            "enabled": self.finbert_enabled,
                            "direction": direction,
                            "severity": severity,
                            "strength": round(strength, 6),
                            "meta": finbert_meta,
                        },
                        "references": {
                            "from_text": ref_text_meta,
                            "from_api_ticker": {"raw": api_ticker, **ref_api_meta},
                            "combined_refs": refs,
                        },
                        "category_source": category_source,
                    }

                    self.last_news_id = max(self.last_news_id, news_id)

                    if not refs:
                        if RUN_RECORDER is not None:
                            await RUN_RECORDER.add_news(item, [], category, direction, severity, [], True, "NO_DEAL_REFERENCE", classifier_meta)
                        continue

                    if phrase_hits(text, FLOW_DISLOCATION_PHRASES):
                        if RUN_RECORDER is not None:
                            await RUN_RECORDER.add_news(item, refs, category, direction, severity, [], True, "FLOW_DISLOCATION_FILTER", classifier_meta)
                        continue

                    if phrase_hits(text, AMBIGUOUS_PHRASES):
                        if RUN_RECORDER is not None:
                            await RUN_RECORDER.add_news(item, refs, category, direction, severity, [], True, "AMBIGUOUS_FILTER", classifier_meta)
                        continue

                    if category is None or direction is None:
                        if RUN_RECORDER is not None:
                            await RUN_RECORDER.add_news(item, refs, category, direction, severity, [], True, "CLASSIFICATION_INCOMPLETE", classifier_meta)
                        continue

                    now = time.time()
                    applied: List[dict] = []
                    async with self.state_lock:
                        for did in refs:
                            deal = DEALS[did]
                            st = self.deal_states[did]
                            old_p = st.prob_news
                            delta = self._news_delta(direction, severity, strength, category, float(deal["deal_mult"]), old_p)
                            st.prob_news = clamp(old_p + delta, 0.0, 1.0)
                            st.last_news_update_ts = now
                            st.last_news_id = news_id
                            st.last_news_delta_abs = abs(delta)
                            st.last_event_strength = max(st.last_event_strength * 0.55, strength)
                            if category == "ALT" and direction == "POS":
                                st.alt_short_lock_until_ts = max(
                                    st.alt_short_lock_until_ts,
                                    now + max(0.0, ALT_POS_SHORT_LOCKOUT_SECS),
                                )
                            lock_left = max(0.0, st.alt_short_lock_until_ts - now)
                            applied.append(
                                {
                                    "deal_id": did,
                                    "old_p": round(old_p, 6),
                                    "delta": round(delta, 6),
                                    "new_p": round(st.prob_news, 6),
                                    "alt_short_lock_left_sec": round(lock_left, 3),
                                }
                            )

                    for row in applied:
                        lock_note = (
                            f" alt_short_lock={row['alt_short_lock_left_sec']:.1f}s"
                            if row.get("alt_short_lock_left_sec", 0.0) > 0
                            else ""
                        )
                        await log(
                            f"NEWS id={news_id} deal={row['deal_id']} cat={category} dir={direction} sev={severity} "
                            f"strength={strength:.3f} delta={row['delta']:+.4f} p:{row['old_p']:.4f}->{row['new_p']:.4f}"
                            f"{lock_note}"
                        )

                    if RUN_RECORDER is not None:
                        await RUN_RECORDER.add_news(item, refs, category, direction, severity, applied, False, None, classifier_meta)

                err_count = 0
                await asyncio.sleep(NEWS_POLL_SECS)
            except Exception as exc:
                err_count += 1
                sleep_s = min(1.0, 0.05 * (2 ** min(err_count, 4)))
                await log(f"NEWS_ERR count={err_count} sleep={sleep_s:.2f}s err={exc}", level="WARN")
                await asyncio.sleep(sleep_s)

    async def manual_override_worker(self) -> None:
        await log("Manual override ready: 'D1 P 0.72' or 'D2 POS L REG'")
        while self.running:
            try:
                line = await asyncio.to_thread(input)
                line = line.strip()
            except Exception:
                await asyncio.sleep(0.1)
                continue

            if not line:
                continue
            parts = line.upper().split()
            if len(parts) == 3 and parts[1] == "P":
                did = parts[0]
                if did not in DEALS:
                    await log(f"MANUAL_SKIP invalid deal {did}")
                    continue
                try:
                    p_new = clamp(float(parts[2]), 0.0, 1.0)
                except ValueError:
                    await log("MANUAL_SKIP invalid p")
                    continue
                async with self.state_lock:
                    st = self.deal_states[did]
                    old = st.prob_news
                    st.prob_news = p_new
                    st.last_news_update_ts = time.time()
                    st.last_news_id = max(st.last_news_id, self.last_news_id)
                    st.last_news_delta_abs = abs(p_new - old)
                    st.last_event_strength = max(0.2, st.last_event_strength)
                await log(f"MANUAL_SET deal={did} p:{old:.4f}->{p_new:.4f}")
                continue

            if len(parts) not in {3, 4}:
                await log("MANUAL_SKIP format")
                continue

            did, direction, severity = parts[0], parts[1], parts[2]
            category = parts[3] if len(parts) == 4 else "FIN"
            if did not in DEALS or direction not in {"POS", "NEG"} or severity not in {"S", "M", "L"} or category not in CATEGORY_MULT:
                await log("MANUAL_SKIP invalid args")
                continue

            async with self.state_lock:
                st = self.deal_states[did]
                old = st.prob_news
                delta = self._news_delta(direction, severity, 0.75, category, float(DEALS[did]["deal_mult"]), old)
                st.prob_news = clamp(old + delta, 0.0, 1.0)
                st.last_news_update_ts = time.time()
                st.last_news_id = max(st.last_news_id, self.last_news_id)
                st.last_news_delta_abs = abs(delta)
                st.last_event_strength = max(st.last_event_strength, 0.65)
            await log(f"MANUAL_DELTA deal={did} delta={delta:+.4f} p:{old:.4f}->{st.prob_news:.4f}")

    async def _periodic_snapshot(self, books: Dict[str, Tuple[float, float, float, int, int]], positions: Dict[str, int]) -> None:
        now = time.time()
        if now - self.last_snapshot_ts < SNAPSHOT_SECS:
            return
        self.last_snapshot_ts = now

        gross, net = compute_gross_net(positions)
        await log(f"RISK gross={gross}/{GROSS_LIMIT} net={net}/{NET_LIMIT}")

        async with self.state_lock:
            for did, deal in DEALS.items():
                target = deal["target"].upper()
                acq = deal["acquirer"].upper()
                if target not in books or acq not in books:
                    continue
                t_bid, t_ask, t_mid, _, _ = books[target]
                _, _, a_mid, _, _ = books[acq]
                st = self.deal_states[did]
                k_mid = deal_value(deal, a_mid)
                denom = k_mid - st.standalone_value
                p_impl = st.prob_news if abs(denom) < 1e-9 else clamp((t_mid - st.standalone_value) / denom, 0.0, 1.0)
                p_star = st.prob_live * k_mid + (1.0 - st.prob_live) * st.standalone_value
                lock_left = max(0.0, st.alt_short_lock_until_ts - now)
                lock_note = f" alt_short_lock={lock_left:.1f}s" if lock_left > 0 else ""
                await log(
                    f"MODEL {did} p_news={st.prob_news:.4f} p_live={st.prob_live:.4f} p_impl={p_impl:.4f} "
                    f"V={st.standalone_value:.2f} Kmid={k_mid:.2f} P*={p_star:.2f} bid/ask={t_bid:.2f}/{t_ask:.2f}"
                    f"{lock_note}"
                )

    async def _capacity_recycler(
        self,
        now: float,
        books: Dict[str, Tuple[float, float, float, int, int]],
        positions: Dict[str, int],
        states_copy: Dict[str, DealState],
        gross_used: int,
    ) -> int:
        trigger_util = max(0.50, min(0.995, CAPACITY_RECYCLE_TRIGGER_UTIL))
        target_util = max(0.40, min(trigger_util - 0.01, CAPACITY_RECYCLE_TARGET_UTIL))
        trigger_gross = int(GROSS_LIMIT * trigger_util)
        target_gross = int(GROSS_LIMIT * target_util)
        if gross_used < trigger_gross:
            return gross_used

        candidates: List[Tuple[float, str]] = []
        for did, deal in DEALS.items():
            target = deal["target"].upper()
            acq = deal["acquirer"].upper()
            if target not in books or acq not in books:
                continue
            t_pos = int(positions.get(target, 0))
            if abs(t_pos) < ORDER_STEP:
                continue
            t_bid, t_ask, t_mid, _, _ = books[target]
            _, _, a_mid, _, _ = books[acq]
            st = states_copy[did]
            k_mid = deal_value(deal, a_mid)
            p_star = st.prob_live * k_mid + (1.0 - st.prob_live) * st.standalone_value
            candidates.append((abs(t_mid - p_star), did))

        for _, did in sorted(candidates, key=lambda x: x[0]):
            if gross_used <= target_gross:
                break

            deal = DEALS[did]
            target = deal["target"].upper()
            acq = deal["acquirer"].upper()
            if target not in books or acq not in books:
                continue

            t_pos = int(positions.get(target, 0))
            a_pos = int(positions.get(acq, 0))
            if abs(t_pos) < ORDER_STEP:
                continue

            t_bid, t_ask, _, _, _ = books[target]
            a_bid, a_ask, _, a_bid_qty, a_ask_qty = books[acq]
            ratio = float(deal["ratio"]) if deal["structure"] in {"stock", "mixed"} else 0.0
            action = "SELL" if t_pos > 0 else "BUY"

            close_qty = scaled_close_qty(abs(t_pos), CAPACITY_RECYCLE_REDUCE_FRACTION)
            hedge_close_qty = compute_hedge_close_qty(action, ratio, close_qty, a_pos)
            close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity(
                action, ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty
            )
            if close_qty < ORDER_STEP:
                continue

            st = states_copy[did]
            p_star = st.prob_live * deal_value(deal, a_ask if action == "SELL" else a_bid) + (1.0 - st.prob_live) * st.standalone_value
            edge = abs(((t_bid + t_ask) / 2.0) - p_star)
            target_ok, hedge_ok = await self._submit_pair(
                did,
                target,
                acq,
                action,
                close_qty,
                hedge_close_qty,
                edge,
                p_star,
                t_bid,
                t_ask,
                a_bid,
                a_ask,
                "CAPACITY_RECYCLE",
            )
            if target_ok:
                positions[target] = t_pos - close_qty if t_pos > 0 else t_pos + close_qty
            if hedge_close_qty > 0 and hedge_ok:
                positions[acq] = a_pos + hedge_close_qty if action == "SELL" else a_pos - hedge_close_qty
            if target_ok or hedge_ok:
                gross_used, _ = compute_gross_net(positions)
                async with self.state_lock:
                    live = self.deal_states[did]
                    live.last_trade_ts = now
                    live.last_risk_reduce_ts = now
                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else now

        return gross_used

    async def _state_snapshot(self) -> Dict[str, DealState]:
        async with self.state_lock:
            return {
                did: DealState(
                    prob_news=st.prob_news,
                    prob_live=st.prob_live,
                    standalone_value=st.standalone_value,
                    last_news_update_ts=st.last_news_update_ts,
                    last_news_id=st.last_news_id,
                    last_news_delta_abs=st.last_news_delta_abs,
                    last_event_strength=st.last_event_strength,
                    last_trade_ts=st.last_trade_ts,
                    hold_start_ts=st.hold_start_ts,
                    last_risk_reduce_ts=st.last_risk_reduce_ts,
                    last_stop_ts=st.last_stop_ts,
                    last_entry_news_id=st.last_entry_news_id,
                    alt_short_lock_until_ts=st.alt_short_lock_until_ts,
                )
                for did, st in self.deal_states.items()
            }

    async def _sync_hold_starts(self, now: float, positions: Dict[str, int]) -> None:
        async with self.state_lock:
            for did, deal in DEALS.items():
                target = deal["target"].upper()
                t_pos = int(positions.get(target, 0))
                st = self.deal_states[did]
                if t_pos != 0 and st.hold_start_ts <= 0:
                    st.hold_start_ts = now
                elif t_pos == 0 and st.hold_start_ts != 0:
                    st.hold_start_ts = 0.0
                    st.last_risk_reduce_ts = 0.0

    async def trade_loop(self) -> None:
        loop_errors = 0
        while self.running:
            try:
                case = await self.client.get_case()
                if case.get("status") != "ACTIVE":
                    await log(f"Case status={case.get('status')} stopping")
                    self.running = False
                    break

                live_positions = await self._safe_positions()
                books = await self._fetch_books_parallel()
                if len(books) < len(self.trade_tickers):
                    missing = sorted(set(self.trade_tickers) - set(books.keys()))
                    if missing:
                        await log(f"BOOK_WARN missing={','.join(missing)}", level="WARN")

                now = time.time()
                await self._cancel_stale_orders(now)
                positions = await self._effective_positions(live_positions)
                await self._sync_hold_starts(now, positions)
                await self._periodic_snapshot(books, positions)

                states_copy = await self._state_snapshot()
                gross_used, net_used = compute_gross_net(positions)
                gross_used = await self._capacity_recycler(now, books, positions, states_copy, gross_used)
                gross_used, net_used = compute_gross_net(positions)
                states_copy = await self._state_snapshot()

                for did, deal in DEALS.items():
                    target = deal["target"].upper()
                    acq = deal["acquirer"].upper()
                    if target not in books or acq not in books:
                        continue

                    st = states_copy[did]
                    can_enter = (now - st.last_trade_ts) >= TRADE_COOLDOWN_SECS

                    t_bid, t_ask, t_mid, _, _ = books[target]
                    a_bid, a_ask, a_mid, a_bid_qty, a_ask_qty = books[acq]
                    ratio = float(deal["ratio"]) if deal["structure"] in {"stock", "mixed"} else 0.0

                    p_live, p_impl = self._update_prob_live(st, deal, t_mid, a_mid, now)
                    async with self.state_lock:
                        self.deal_states[did].prob_live = p_live

                    k_buy = deal_value(deal, a_bid)
                    k_sell = deal_value(deal, a_ask)
                    p_star_buy = p_live * k_buy + (1.0 - p_live) * st.standalone_value
                    p_star_sell = p_live * k_sell + (1.0 - p_live) * st.standalone_value

                    target_pos = int(positions.get(target, 0))
                    acq_pos = int(positions.get(acq, 0))
                    held_secs = (now - st.hold_start_ts) if st.hold_start_ts > 0 else 0.0
                    commission_cost = COMMISSION_PER_SHARE * (1.0 + ratio)

                    # Stop loss exits
                    if target_pos > 0 and t_bid <= p_star_buy - commission_cost - STOP_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("SELL", ratio, close_qty, acq_pos)
                        close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity("SELL", ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty)
                        if close_qty >= ORDER_STEP:
                            target_ok, hedge_ok = await self._submit_pair(did, target, acq, "SELL", close_qty, hedge_close_qty, p_star_buy - t_bid, p_star_buy, t_bid, t_ask, a_bid, a_ask, "STOP_LOSS_LONG")
                            if target_ok:
                                positions[target] = target_pos - close_qty
                            if hedge_close_qty > 0 and hedge_ok:
                                positions[acq] = acq_pos + hedge_close_qty
                            if target_ok or hedge_ok:
                                async with self.state_lock:
                                    live = self.deal_states[did]
                                    live.last_trade_ts = now
                                    live.last_stop_ts = now
                                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else now
                        continue

                    if target_pos < 0 and t_ask >= p_star_sell + commission_cost + STOP_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("BUY", ratio, close_qty, acq_pos)
                        close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity("BUY", ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty)
                        if close_qty >= ORDER_STEP:
                            target_ok, hedge_ok = await self._submit_pair(did, target, acq, "BUY", close_qty, hedge_close_qty, t_ask - p_star_sell, p_star_sell, t_bid, t_ask, a_bid, a_ask, "STOP_LOSS_SHORT")
                            if target_ok:
                                positions[target] = target_pos + close_qty
                            if hedge_close_qty > 0 and hedge_ok:
                                positions[acq] = acq_pos - hedge_close_qty
                            if target_ok or hedge_ok:
                                async with self.state_lock:
                                    live = self.deal_states[did]
                                    live.last_trade_ts = now
                                    live.last_stop_ts = now
                                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else now
                        continue

                    # Take profit exits
                    if target_pos > 0 and t_bid >= p_star_buy - commission_cost - EXIT_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("SELL", ratio, close_qty, acq_pos)
                        close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity("SELL", ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty)
                        if close_qty >= ORDER_STEP:
                            target_ok, hedge_ok = await self._submit_pair(did, target, acq, "SELL", close_qty, hedge_close_qty, t_bid - p_star_buy, p_star_buy, t_bid, t_ask, a_bid, a_ask, "TAKE_PROFIT_LONG")
                            if target_ok:
                                positions[target] = target_pos - close_qty
                            if hedge_close_qty > 0 and hedge_ok:
                                positions[acq] = acq_pos + hedge_close_qty
                            if target_ok or hedge_ok:
                                async with self.state_lock:
                                    live = self.deal_states[did]
                                    live.last_trade_ts = now
                                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else live.hold_start_ts
                        continue

                    if target_pos < 0 and t_ask <= p_star_sell + commission_cost + EXIT_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("BUY", ratio, close_qty, acq_pos)
                        close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity("BUY", ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty)
                        if close_qty >= ORDER_STEP:
                            target_ok, hedge_ok = await self._submit_pair(did, target, acq, "BUY", close_qty, hedge_close_qty, p_star_sell - t_ask, p_star_sell, t_bid, t_ask, a_bid, a_ask, "TAKE_PROFIT_SHORT")
                            if target_ok:
                                positions[target] = target_pos + close_qty
                            if hedge_close_qty > 0 and hedge_ok:
                                positions[acq] = acq_pos - hedge_close_qty
                            if target_ok or hedge_ok:
                                async with self.state_lock:
                                    live = self.deal_states[did]
                                    live.last_trade_ts = now
                                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else live.hold_start_ts
                        continue

                    # Time stop reduction
                    can_risk_reduce = (now - st.last_risk_reduce_ts) >= RISK_REDUCE_COOLDOWN_SECS
                    if abs(target_pos) >= ORDER_STEP and held_secs >= MAX_HOLD_SECS and can_risk_reduce:
                        close_qty = scaled_close_qty(abs(target_pos), TIME_REDUCE_FRACTION)
                        close_action = "SELL" if target_pos > 0 else "BUY"
                        hedge_close_qty = compute_hedge_close_qty(close_action, ratio, close_qty, acq_pos)
                        close_qty, hedge_close_qty = self._cap_target_qty_by_hedge_liquidity(close_action, ratio, close_qty, hedge_close_qty, a_bid_qty, a_ask_qty)
                        if close_qty >= ORDER_STEP:
                            p_star_close = p_star_sell if close_action == "SELL" else p_star_buy
                            target_ok, hedge_ok = await self._submit_pair(did, target, acq, close_action, close_qty, hedge_close_qty, abs(((t_bid + t_ask) / 2.0) - p_star_close), p_star_close, t_bid, t_ask, a_bid, a_ask, "TIME_STOP")
                            if target_ok:
                                positions[target] = target_pos - close_qty if target_pos > 0 else target_pos + close_qty
                            if hedge_close_qty > 0 and hedge_ok:
                                positions[acq] = acq_pos + hedge_close_qty if close_action == "SELL" else acq_pos - hedge_close_qty
                            if target_ok or hedge_ok:
                                async with self.state_lock:
                                    live = self.deal_states[did]
                                    live.last_trade_ts = now
                                    live.last_risk_reduce_ts = now
                                    live.hold_start_ts = 0.0 if int(positions.get(target, 0)) == 0 else now
                        continue

                    # Orphan hedge unwind
                    if target_pos == 0 and ratio > 0 and abs(acq_pos) >= ORDER_STEP:
                        unwind_qty = close_qty_for_position(abs(acq_pos))
                        unwind_action = "SELL" if acq_pos > 0 else "BUY"
                        ok = await self._submit_single(did, acq, unwind_action, unwind_qty, a_bid, a_ask, "ORPHAN_HEDGE_UNWIND")
                        if ok:
                            positions[acq] = acq_pos - unwind_qty if acq_pos > 0 else acq_pos + unwind_qty
                            async with self.state_lock:
                                live = self.deal_states[did]
                                live.last_trade_ts = now
                                live.hold_start_ts = 0.0
                        continue

                    if not can_enter:
                        continue

                    # Entry gating
                    if st.last_news_id <= 0:
                        continue
                    if (now - st.last_news_update_ts) > EVENT_WINDOW_SECS:
                        continue
                    if st.last_news_delta_abs < MIN_NEWS_DELTA:
                        continue
                    if (now - st.last_stop_ts) < STOP_REENTRY_COOLDOWN_SECS:
                        continue
                    if st.last_entry_news_id >= st.last_news_id:
                        continue

                    dislocation = abs(st.prob_news - p_impl)
                    if st.last_event_strength < EVENT_MIN_STRENGTH and dislocation < DISLOCATION_GATE:
                        continue

                    long_edge = p_star_buy - t_ask
                    short_edge = t_bid - p_star_sell
                    action = None
                    edge = 0.0
                    p_star_action = 0.0
                    if long_edge > short_edge and long_edge > 0:
                        action = "BUY"
                        edge = long_edge
                        p_star_action = p_star_buy
                    elif short_edge > 0:
                        action = "SELL"
                        edge = short_edge
                        p_star_action = p_star_sell

                    if action is None:
                        continue

                    alt_short_lock_active = st.alt_short_lock_until_ts > now
                    if action == "SELL" and alt_short_lock_active and target_pos <= 0:
                        continue

                    friction = compute_transaction_friction(ratio, t_bid, t_ask, a_bid, a_ask)
                    base_threshold = max(MIN_ENTRY_THRESHOLD, friction + ENTRY_MARGIN_PER_SHARE)
                    dyn_threshold = inventory_adjusted_threshold(base_threshold, target_pos, action, gross_used, net_used)
                    if edge <= dyn_threshold:
                        continue

                    edge_mult = max(1.0, min(4.0, edge / max(0.01, dyn_threshold)))
                    seed_qty = int(BASE_ORDER_QTY * edge_mult)

                    target_cap = PER_DEAL_TARGET_MAX
                    acq_cap = per_deal_acq_cap(ratio)
                    max_target_by_target_cap = max_qty_for_position_cap(target_pos, action, target_cap)
                    max_target_qty = max_target_by_target_cap
                    if ratio > 0:
                        hedge_side = "SELL" if action == "BUY" else "BUY"
                        max_hedge_by_cap = max_qty_for_position_cap(acq_pos, hedge_side, acq_cap)
                        max_target_by_acq_cap = int(max_hedge_by_cap / max(1e-9, ratio))
                        max_target_qty = min(max_target_qty, max_target_by_acq_cap)
                    if action == "SELL" and alt_short_lock_active:
                        max_target_qty = min(max_target_qty, max(0, target_pos))
                    if max_target_qty <= 0:
                        continue

                    target_qty, hedge_qty = scale_target_qty(ratio, seed_qty, action, target, acq, positions, max_target_qty)
                    if target_qty < MIN_ORDER_QTY:
                        continue

                    target_qty, hedge_qty = self._cap_target_qty_by_hedge_liquidity(action, ratio, target_qty, hedge_qty, a_bid_qty, a_ask_qty)
                    if target_qty < MIN_ORDER_QTY:
                        continue

                    if ratio > 0 and hedge_qty < ORDER_STEP:
                        continue

                    target_ok, hedge_ok = await self._submit_pair(
                        did,
                        target,
                        acq,
                        action,
                        target_qty,
                        hedge_qty,
                        edge,
                        p_star_action,
                        t_bid,
                        t_ask,
                        a_bid,
                        a_ask,
                        "ENTRY",
                    )

                    if target_ok:
                        positions[target] = int(positions.get(target, 0) + (target_qty if action == "BUY" else -target_qty))
                    if hedge_qty > 0 and hedge_ok:
                        hedge_delta = -hedge_qty if action == "BUY" else hedge_qty
                        positions[acq] = int(positions.get(acq, 0) + hedge_delta)
                    if target_ok or hedge_ok:
                        gross_used, net_used = compute_gross_net(positions)
                        async with self.state_lock:
                            live = self.deal_states[did]
                            live.last_trade_ts = now
                            live.hold_start_ts = now if int(positions.get(target, 0)) != 0 else 0.0
                            live.last_entry_news_id = max(live.last_entry_news_id, st.last_news_id)

                loop_errors = 0
                await asyncio.sleep(TRADE_LOOP_SECS)
            except Exception as exc:
                loop_errors += 1
                sleep_s = min(1.0, 0.05 * (2 ** min(loop_errors, 4)))
                await log(f"TRADE_ERR count={loop_errors} sleep={sleep_s:.2f}s err={exc}", level="WARN")
                await asyncio.sleep(sleep_s)

    async def run(self) -> None:
        if not API_KEY:
            raise RuntimeError("Set RIT_API_KEY before running")

        if RUN_RECORDER is not None:
            await RUN_RECORDER.set_context(
                {
                    "deals": DEALS,
                    "config": {
                        "BASE_URL": BASE_URL,
                        "TRADE_LOOP_SECS": TRADE_LOOP_SECS,
                        "NEWS_POLL_SECS": NEWS_POLL_SECS,
                        "GROSS_LIMIT": GROSS_LIMIT,
                        "NET_LIMIT": NET_LIMIT,
                        "BASE_ORDER_QTY": BASE_ORDER_QTY,
                        "MIN_ORDER_QTY": MIN_ORDER_QTY,
                        "MAX_ORDER_SIZE": MAX_ORDER_SIZE,
                        "MIN_ENTRY_THRESHOLD": MIN_ENTRY_THRESHOLD,
                        "ENTRY_MARGIN_PER_SHARE": ENTRY_MARGIN_PER_SHARE,
                        "ALT_POS_SHORT_LOCKOUT_SECS": ALT_POS_SHORT_LOCKOUT_SECS,
                        "EXIT_BUFFER": EXIT_BUFFER,
                        "STOP_BUFFER": STOP_BUFFER,
                        "MAX_HOLD_SECS": MAX_HOLD_SECS,
                        "USE_FINBERT": USE_FINBERT,
                        "FINBERT_ONNX_MODEL": FINBERT_ONNX_MODEL,
                        "FINBERT_TOKENIZER_DIR": FINBERT_TOKENIZER_DIR,
                    },
                }
            )

        await self._initialize_finbert()
        await self.initialize()
        await log(
            "Async alpha started "
            f"loop={TRADE_LOOP_SECS:.3f}s news={NEWS_POLL_SECS:.3f}s "
            f"min_edge={MIN_ENTRY_THRESHOLD:.3f} margin={ENTRY_MARGIN_PER_SHARE:.3f} "
            f"stop={STOP_BUFFER:.3f} hold={MAX_HOLD_SECS:.1f}s finbert={'ON' if self.finbert_enabled else 'OFF'}"
        )

        news_task = asyncio.create_task(self.news_worker(), name="news-worker")
        manual_task = None
        if ENABLE_MANUAL_OVERRIDE:
            manual_task = asyncio.create_task(self.manual_override_worker(), name="manual-override-worker")

        try:
            await self.trade_loop()
        finally:
            self.running = False
            for task in [news_task, manual_task]:
                if task is not None:
                    task.cancel()
            await asyncio.gather(*[t for t in [news_task, manual_task] if t is not None], return_exceptions=True)
            await log("Stopped")

            if RUN_RECORDER is not None:
                final_case = {}
                final_positions = {}
                final_open_orders: List[dict] = []
                try:
                    final_case = await self.client.get_case()
                except Exception:
                    pass
                try:
                    final_positions = await self._safe_positions()
                except Exception:
                    pass
                try:
                    final_open_orders = await self.client.get_orders(status="OPEN")
                except Exception:
                    pass

                async with self.state_lock:
                    deal_state_summary = {
                        did: {
                            "prob_news": round(st.prob_news, 6),
                            "prob_live": round(st.prob_live, 6),
                            "standalone_value": round(st.standalone_value, 6),
                            "last_news_update_ts": st.last_news_update_ts,
                            "last_news_id": st.last_news_id,
                            "last_news_delta_abs": st.last_news_delta_abs,
                            "last_event_strength": st.last_event_strength,
                            "last_trade_ts": st.last_trade_ts,
                            "hold_start_ts": st.hold_start_ts,
                            "last_risk_reduce_ts": st.last_risk_reduce_ts,
                            "last_stop_ts": st.last_stop_ts,
                            "last_entry_news_id": st.last_entry_news_id,
                            "alt_short_lock_until_ts": st.alt_short_lock_until_ts,
                        }
                        for did, st in self.deal_states.items()
                    }
                async with self.order_lock:
                    pending = dict(self.pending_pos_deltas)
                    tracked_open = len(self.open_order_meta)

                summary = {
                    "final_case": final_case,
                    "last_news_id": self.last_news_id,
                    "deal_states": deal_state_summary,
                    "final_positions": final_positions,
                    "open_order_count": len(final_open_orders),
                    "tracked_open_order_count": tracked_open,
                    "pending_delta_count": len(pending),
                    "pending_pos_deltas": pending,
                    "finbert_enabled": self.finbert_enabled,
                    "finbert_model_path": self.finbert_model_path,
                    "finbert_tokenizer_path": self.finbert_tokenizer_path,
                }

                try:
                    out_path = await RUN_RECORDER.flush(summary)
                    print(f"{now_ts()} | RUN_LOG_JSON saved to {out_path}", flush=True)
                except Exception as exc:
                    print(f"{now_ts()} | RUN_LOG_JSON write failed: {exc}", flush=True)


async def _async_main() -> None:
    global RUN_RECORDER
    if WRITE_RUN_JSON:
        RUN_RECORDER = RunRecorder(base_url=BASE_URL)

    async with AsyncRITClient(api_key=API_KEY, base_url=BASE_URL, timeout_s=1.2, pool_limit=64,
                               use_dma_auth=USE_DMA_AUTH, dma_user=DMA_USER, dma_pass=DMA_PASS) as client:
        bot = MergerArbAlphaAsyncBot(client)
        await bot.run()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
