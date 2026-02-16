"""Event-driven high-frequency merger arbitrage bot for RIT REST API.

Run:
    RIT_API_KEY=... python merger_arb_event_driven_hft.py
"""

from __future__ import annotations

import json
import importlib.util
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")


def env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _find_repo_root(start: Path) -> Path:
    for p in [start] + list(start.parents):
        if (p / ".git").exists():
            return p
    return start.parent


def _looks_like_hf_model_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    # Minimal indicator for a local Hugging Face checkpoint/tokenizer directory.
    return (path / "config.json").exists()


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
REPO_ROOT = _find_repo_root(_THIS_FILE)
DEFAULT_FINBERT_ONNX_MODEL = REPO_ROOT / "ready_bots" / "finbert_hft" / "model_opt_int8.onnx"
DEFAULT_FINBERT_TOKENIZER_DIR = REPO_ROOT / "ready_bots" / "finbert_hft" / "local_finbert"


def _detect_finbert_assets(onnx_hint: str, tokenizer_hint: str) -> Tuple[Optional[Path], Optional[Path], str]:
    onnx_candidates = _dedupe_paths(
        [
            Path(onnx_hint).expanduser(),
            DEFAULT_FINBERT_ONNX_MODEL,
            REPO_ROOT / "ready_bots" / "finbert_hft" / "output" / "model_opt_int8.onnx",
            REPO_ROOT / "ready_bots" / "finbert_hft" / "onnx" / "model_opt_int8.onnx",
        ]
    )
    onnx_path = next((p for p in onnx_candidates if p.exists() and p.is_file()), None)
    if onnx_path is None:
        return None, None, (
            "No ONNX model found. Checked: "
            + ", ".join(str(p) for p in onnx_candidates)
        )

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
            REPO_ROOT / "ready_bots" / "finbert_hft" / "model",
            REPO_ROOT / "ready_bots" / "finbert_hft",
        ]
    )
    tokenizer_candidates = _dedupe_paths(tokenizer_candidates)
    tokenizer_path = next((p for p in tokenizer_candidates if _looks_like_hf_model_dir(p)), None)
    if tokenizer_path is None:
        return onnx_path, None, (
            f"ONNX model found at {onnx_path}, but tokenizer/model directory was not found. Checked: "
            + ", ".join(str(p) for p in tokenizer_candidates)
        )

    return onnx_path, tokenizer_path, "ok"


WRITE_RUN_JSON = env_bool("RIT_MA_WRITE_RUN_JSON", "1")
RUN_LOG_DIR = os.environ.get("RIT_MA_LOG_DIR", "logs")
RUN_LOG_JSON_PATH = os.environ.get("RIT_MA_LOG_JSON_PATH", "").strip()
RUN_RECORDER: Optional["RunRecorder"] = None

# Timing / execution
NEWS_POLL_SECS = float(os.environ.get("RIT_MA_NEWS_POLL_SECS", "0.15"))
TRADE_LOOP_SECS = float(os.environ.get("RIT_MA_TRADE_LOOP_SECS", "0.12"))
BOOK_WORKERS = int(os.environ.get("RIT_MA_BOOK_WORKERS", "10"))
CASE_POLL_SECS = float(os.environ.get("RIT_MA_CASE_POLL_SECS", "0.25"))
TRADE_COOLDOWN_SECS = float(os.environ.get("RIT_MA_TRADE_COOLDOWN_SECS", "0.20"))
SNAPSHOT_SECS = float(os.environ.get("RIT_MA_SNAPSHOT_SECS", "2.0"))

# Thresholds / size
MISPRICING_THRESHOLD = float(os.environ.get("RIT_MA_THRESHOLD", "0.18"))
DESIRED_PROFIT_MARGIN = float(os.environ.get("RIT_MA_PROFIT_MARGIN", "0.03"))
COMMISSION_PER_SHARE = float(os.environ.get("RIT_MA_COMMISSION_PER_SHARE", "0.02"))
MARKETABLE_LIMIT_OFFSET = float(os.environ.get("RIT_MA_LIMIT_OFFSET", "0.02"))
BASE_ORDER_QTY = int(os.environ.get("RIT_MA_BASE_ORDER_QTY", "2000"))
MIN_ORDER_QTY = int(os.environ.get("RIT_MA_MIN_ORDER_QTY", "500"))
ORDER_STEP = int(os.environ.get("RIT_MA_ORDER_STEP", "100"))
MAX_ORDER_SIZE = 5000

# Initialization robustness
INIT_WARMUP_SECS = float(os.environ.get("RIT_MA_INIT_WARMUP_SECS", "5.0"))
INIT_SNAPSHOTS = int(os.environ.get("RIT_MA_INIT_SNAPSHOTS", "10"))
INIT_SAMPLE_INTERVAL_SECS = float(os.environ.get("RIT_MA_INIT_SAMPLE_INTERVAL_SECS", "0.35"))

# Exit / inventory behavior
TAKE_PROFIT_BUFFER = float(os.environ.get("RIT_MA_TAKE_PROFIT_BUFFER", "0.01"))
ADD_INVENTORY_SLOPE = float(os.environ.get("RIT_MA_ADD_INVENTORY_SLOPE", "1.80"))
ADD_GLOBAL_SLOPE = float(os.environ.get("RIT_MA_ADD_GLOBAL_SLOPE", "1.25"))
REDUCE_THRESHOLD_MULT = float(os.environ.get("RIT_MA_REDUCE_THRESHOLD_MULT", "0.40"))
MIN_REDUCE_THRESHOLD = float(os.environ.get("RIT_MA_MIN_REDUCE_THRESHOLD", "0.03"))

# Execution controls
SIMULTANEOUS_LEGS = env_bool("RIT_MA_SIMULTANEOUS_LEGS", "1")
ENABLE_MANUAL_OVERRIDE = env_bool("RIT_MA_ENABLE_MANUAL_OVERRIDE", "1")
USE_FINBERT = env_bool("RIT_MA_USE_FINBERT", "1")
FINBERT_ONNX_MODEL = os.environ.get("RIT_MA_FINBERT_ONNX_MODEL", str(DEFAULT_FINBERT_ONNX_MODEL)).strip()
FINBERT_TOKENIZER_DIR = os.environ.get("RIT_MA_FINBERT_TOKENIZER_DIR", str(DEFAULT_FINBERT_TOKENIZER_DIR)).strip()
FINBERT_MAX_LENGTH = int(os.environ.get("RIT_MA_FINBERT_MAX_LENGTH", "128"))
FINBERT_POS_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_POS_THRESHOLD", "0.56"))
FINBERT_NEG_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_NEG_THRESHOLD", "0.56"))
FINBERT_GAP_THRESHOLD = float(os.environ.get("RIT_MA_FINBERT_GAP_THRESHOLD", "0.08"))
FINBERT_OVERRIDE_GAP = float(os.environ.get("RIT_MA_FINBERT_OVERRIDE_GAP", "0.22"))
FINBERT_SEV_MEDIUM = float(os.environ.get("RIT_MA_FINBERT_SEV_MEDIUM", "0.65"))
FINBERT_SEV_LARGE = float(os.environ.get("RIT_MA_FINBERT_SEV_LARGE", "0.80"))
FINBERT_CATEGORY_FALLBACK = os.environ.get("RIT_MA_FINBERT_CATEGORY_FALLBACK", "FIN").strip().upper()

# Risk limits from case package
GROSS_LIMIT = 100_000
NET_LIMIT = 50_000
RISK_BUFFER = float(os.environ.get("RIT_MA_RISK_BUFFER", "0.98"))

# Position / order hygiene
PER_DEAL_TARGET_MAX = int(os.environ.get("RIT_MA_PER_DEAL_TARGET_MAX", "25000"))
PER_DEAL_ACQ_CAP_MULT = float(os.environ.get("RIT_MA_PER_DEAL_ACQ_CAP_MULT", "1.30"))
HEDGE_REBALANCE_TRIGGER = int(os.environ.get("RIT_MA_HEDGE_REBALANCE_TRIGGER", "1200"))
STALE_ORDER_SECS = float(os.environ.get("RIT_MA_STALE_ORDER_SECS", "0.90"))
STALE_CANCEL_CHECK_SECS = float(os.environ.get("RIT_MA_STALE_CANCEL_CHECK_SECS", "0.20"))

# Deal definitions requested by user
DEALS = {
    "D1": {
        "target": "TGX",
        "acquirer": "PHR",
        "structure": "cash",
        "cash_terms": 50.0,
        "ratio": 0.0,
        "p0": 0.70,
        "deal_mult": 1.00,
    },
    "D2": {
        "target": "BYL",
        "acquirer": "CLD",
        "structure": "stock",
        "cash_terms": 0.0,
        "ratio": 0.75,
        "p0": 0.55,
        "deal_mult": 1.05,
    },
    "D3": {
        "target": "GGD",
        "acquirer": "PNR",
        "structure": "mixed",
        "cash_terms": 33.0,
        "ratio": 0.20,
        "p0": 0.50,
        "deal_mult": 1.10,
    },
    "D4": {
        "target": "FSR",
        "acquirer": "ATB",
        "structure": "cash",
        "cash_terms": 40.0,
        "ratio": 0.0,
        "p0": 0.38,
        "deal_mult": 1.30,
    },
    "D5": {
        "target": "SPK",
        "acquirer": "EEC",
        "structure": "stock",
        "cash_terms": 0.0,
        "ratio": 1.20,
        "p0": 0.45,
        "deal_mult": 1.15,
    },
}

BASE_CHANGE = {
    ("POS", "S"): 0.03,
    ("POS", "M"): 0.07,
    ("POS", "L"): 0.14,
    ("NEG", "S"): -0.04,
    ("NEG", "M"): -0.09,
    ("NEG", "L"): -0.18,
}

CATEGORY_MULT = {"REG": 1.25, "FIN": 1.00, "SHR": 0.90, "ALT": 1.40, "PRC": 0.70}

CATEGORY_KEYWORDS = {
    "REG": [
        "regulator",
        "regulatory",
        "antitrust",
        "doj",
        "cma",
        "competition",
        "clearance",
        "approval",
        "approved",
        "blocked",
        "injunction",
        "ftc",
    ],
    "FIN": [
        "financing",
        "financing package",
        "financing secured",
        "debt",
        "credit",
        "loan",
        "bridge loan",
        "covenant",
        "liquidity",
        "capital raise",
        "funding",
    ],
    "SHR": [
        "shareholder",
        "board",
        "proxy",
        "vote",
        "investor",
        "activist",
        "recommendation",
        "special committee",
    ],
    "ALT": [
        "competing bid",
        "rival bid",
        "topping bid",
        "alternative proposal",
        "go-shop",
        "renegotiate",
        "higher offer",
        "counterbid",
    ],
    "PRC": [
        "timeline",
        "delay",
        "extended",
        "extension",
        "process",
        "closing date",
        "condition",
        "termination right",
        "deadline",
    ],
}

POSITIVE_WORDS = [
    "approve",
    "approved",
    "clear",
    "cleared",
    "support",
    "favorable",
    "progress",
    "secured",
    "confident",
    "raised bid",
    "sweetened",
]
NEGATIVE_WORDS = [
    "block",
    "blocked",
    "reject",
    "rejected",
    "sue",
    "lawsuit",
    "terminate",
    "termination",
    "withdraw",
    "delay",
    "challenge",
    "concern",
]

LARGE_WORDS = [
    "major",
    "significant",
    "material",
    "definitive",
    "terminated",
    "blocked",
    "injunction granted",
    "deal canceled",
]
MEDIUM_WORDS = [
    "review",
    "investigation",
    "concern",
    "hearing",
    "vote scheduled",
    "financing risk",
    "renegotiate",
]

NEGATION_WORDS = ["not", "no", "never", "without"]
NEGATIVE_WORD_REVERSAL = ["dismissed", "dropped", "denied", "cleared", "rejected"]

# Explicit code parsing (works if RIT headline includes tags like "REG", "NEG", "L").
CATEGORY_RE = re.compile(r"\b(REG|FIN|SHR|ALT|PRC)\b", re.IGNORECASE)
DIRECTION_RE = re.compile(r"\b(POS|NEG|POSITIVE|NEGATIVE)\b", re.IGNORECASE)
SEVERITY_WORD_RE = re.compile(r"\b(SMALL|MEDIUM|LARGE)\b", re.IGNORECASE)
SEVERITY_TAG_RE = re.compile(r"\bSEV(?:ERITY)?\s*[:=]\s*(S|M|L)\b", re.IGNORECASE)
DEAL_RE = re.compile(r"\bD([1-5])\b", re.IGNORECASE)
NEG_BEFORE_TEMPLATE = rf"\b(?:{'|'.join(NEGATION_WORDS)})\b(?:\W+\w+){{0,3}}\W+{{}}"
REVERSAL_AFTER_TEMPLATE = rf"{{}}\W+(?:\w+\W+){{0,3}}\b(?:{'|'.join(NEGATIVE_WORD_REVERSAL)})\b"


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def utc_iso_now() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


class RunRecorder:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.started_at_utc = utc_iso_now()
        self.started_epoch = time.time()
        self.lock = threading.Lock()
        self.events: List[dict] = []
        self.news: List[dict] = []
        self.context: Dict[str, object] = {}
        self.finalized = False
        self.output_path = self._resolve_output_path()

    def _resolve_output_path(self) -> Path:
        if RUN_LOG_JSON_PATH:
            return Path(RUN_LOG_JSON_PATH).expanduser()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Path(RUN_LOG_DIR).expanduser() / f"merger_arb_heat_{stamp}.json"

    def set_context(self, context: Dict[str, object]) -> None:
        with self.lock:
            self.context = context

    def add_event(self, level: str, message: str, extra: Optional[dict] = None) -> None:
        with self.lock:
            self.events.append(
                {
                    "ts_utc": utc_iso_now(),
                    "ts_epoch": round(time.time(), 6),
                    "level": level,
                    "message": message,
                    "extra": extra or {},
                }
            )

    def add_news(
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
        with self.lock:
            self.news.append(
                {
                    "ts_utc": utc_iso_now(),
                    "news_id": item.get("news_id"),
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

    def flush(self, summary: Dict[str, object]) -> str:
        with self.lock:
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


def log(msg: str, level: str = "INFO", extra: Optional[dict] = None) -> None:
    print(f"{now_ts()} | {msg}", flush=True)
    if RUN_RECORDER is not None:
        RUN_RECORDER.add_event(level=level, message=msg, extra=extra)


def _resolve_finbert_module_path() -> Optional[Path]:
    here = Path(__file__).resolve()
    candidates: List[Path] = [
        here.parent / "finbert_hft" / "fast_inference.py",
    ]
    if len(here.parents) >= 2:
        candidates.append(here.parents[1] / "ready_bots" / "finbert_hft" / "fast_inference.py")
    if len(here.parents) >= 3:
        candidates.append(here.parents[2] / "ready_bots" / "finbert_hft" / "fast_inference.py")
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_finbert_trader_class():
    mod_path = _resolve_finbert_module_path()
    if mod_path is None:
        return None, "Could not find finbert_hft/fast_inference.py"
    try:
        spec = importlib.util.spec_from_file_location("finbert_hft_fast_inference", str(mod_path))
        if spec is None or spec.loader is None:
            return None, f"Failed to create import spec for {mod_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        trader_cls = getattr(module, "FinBERTTrader", None)
        if trader_cls is None:
            return None, "FinBERTTrader class not found in fast_inference.py"
        return trader_cls, None
    except Exception as exc:
        return None, f"FinBERT module import failed: {exc}"


class RITClient:
    def __init__(self, api_key: str, base_url: str, timeout: float = 1.5) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-key": api_key})

    def _request(self, method: str, path: str, params: Optional[dict] = None, retries: int = 4):
        backoff = 0.05
        for attempt in range(retries):
            try:
                r = self.session.request(
                    method=method,
                    url=self.base_url + path,
                    params=params,
                    timeout=self.timeout,
                )
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    sleep_s = float(retry_after) if retry_after else backoff
                    time.sleep(max(0.02, sleep_s))
                    backoff = min(0.5, backoff * 2.0)
                    continue
                if 500 <= r.status_code < 600:
                    time.sleep(backoff)
                    backoff = min(0.6, backoff * 1.8)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(backoff)
                backoff = min(0.6, backoff * 1.8)
        raise RuntimeError("Unreachable request flow")

    def get_case(self) -> dict:
        return self._request("GET", "/case")

    def get_news(self, since: Optional[int] = None, limit: Optional[int] = None) -> List[dict]:
        params = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        return self._request("GET", "/news", params=params)

    def get_securities(self) -> List[dict]:
        return self._request("GET", "/securities")

    def get_book(self, ticker: str) -> dict:
        return self._request("GET", "/securities/book", params={"ticker": ticker, "limit": 1})

    def get_orders(self, status: Optional[str] = None) -> List[dict]:
        params = {"status": status} if status else None
        return self._request("GET", "/orders", params=params)

    def cancel_order(self, order_id: int) -> dict:
        return self._request("DELETE", f"/orders/{order_id}")

    def cancel_all(self, ticker: Optional[str] = None) -> dict:
        params = {"ticker": ticker} if ticker else {"all": 1}
        return self._request("POST", "/commands/cancel", params=params)

    def place_order(
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
        return self._request("POST", "/orders", params=params, retries=3)


def best_bid_ask_from_book(book: dict) -> Tuple[Optional[float], Optional[float]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None, None
    bid = bids[0].get("price")
    ask = asks[0].get("price")
    if bid is None or ask is None:
        return None, None
    return float(bid), float(ask)


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


def classify_category(text_lower: str) -> Optional[str]:
    code_match = CATEGORY_RE.search(text_lower.upper())
    if code_match:
        return code_match.group(1).upper()
    best_cat = None
    best_score = 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(1 for w in words if w in text_lower)
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat


def _count_keyword_occurrences(text_lower: str, keyword: str) -> int:
    return len(re.findall(rf"\b{re.escape(keyword)}\b", text_lower))


def _count_negated_keyword_occurrences(text_lower: str, keyword: str) -> int:
    pattern = NEG_BEFORE_TEMPLATE.format(rf"\b{re.escape(keyword)}\b")
    return len(re.findall(pattern, text_lower, flags=re.IGNORECASE))


def _count_reversed_negative_occurrences(text_lower: str, keyword: str) -> int:
    pattern = REVERSAL_AFTER_TEMPLATE.format(rf"\b{re.escape(keyword)}\b")
    return len(re.findall(pattern, text_lower, flags=re.IGNORECASE))


def classify_direction(text_lower: str) -> Optional[str]:
    code_match = DIRECTION_RE.search(text_lower.upper())
    if code_match:
        token = code_match.group(1).upper()
        return "POS" if token.startswith("POS") else "NEG"

    pos_score = 0.0
    neg_score = 0.0

    for kw in POSITIVE_WORDS:
        total = _count_keyword_occurrences(text_lower, kw)
        if total == 0:
            continue
        negated = _count_negated_keyword_occurrences(text_lower, kw)
        pos_score += max(0, total - negated)
        neg_score += 1.20 * negated

    for kw in NEGATIVE_WORDS:
        total = _count_keyword_occurrences(text_lower, kw)
        if total == 0:
            continue
        negated = _count_negated_keyword_occurrences(text_lower, kw)
        reversed_after = _count_reversed_negative_occurrences(text_lower, kw)
        effective_negated = negated + reversed_after
        neg_score += max(0, total - effective_negated)
        pos_score += 1.25 * effective_negated

    # Extra explicit phrase handling for double negatives like "not to block".
    if re.search(r"\b(?:not|never|no)\b(?:\W+\w+){0,3}\W+\b(?:block|reject|terminate|withdraw)\b", text_lower):
        pos_score += 2.0
    if re.search(r"\b(?:not|never|no)\b(?:\W+\w+){0,3}\W+\b(?:approve|clear|support|secure)\b", text_lower):
        neg_score += 2.0

    if pos_score == neg_score == 0:
        return None
    if abs(pos_score - neg_score) < 0.2:
        return None
    if pos_score >= neg_score:
        return "POS"
    return "NEG"


def classify_severity(text_lower: str) -> str:
    code_match = SEVERITY_TAG_RE.search(text_lower.upper())
    if code_match:
        token = code_match.group(1).upper()
        if token == "L":
            return "L"
        if token == "M":
            return "M"
        return "S"
    code_match = SEVERITY_WORD_RE.search(text_lower.upper())
    if code_match:
        token = code_match.group(1).upper()
        if token == "LARGE":
            return "L"
        if token == "MEDIUM":
            return "M"
        return "S"
    if any(w in text_lower for w in LARGE_WORDS):
        return "L"
    if any(w in text_lower for w in MEDIUM_WORDS):
        return "M"
    return "S"


def extract_referenced_deals(text: str, deal_tickers: Dict[str, str]) -> List[str]:
    text_upper = text.upper()
    refs = set()

    for m in DEAL_RE.finditer(text_upper):
        refs.add(f"D{m.group(1)}")

    for ticker, deal_id in deal_tickers.items():
        if ticker in text_upper:
            refs.add(deal_id)

    return sorted(refs)


def compute_gross_net(positions: Dict[str, int]) -> Tuple[int, int]:
    gross = int(sum(abs(v) for v in positions.values()))
    net = int(abs(sum(positions.values())))
    return gross, net


def project_limits_ok(
    positions: Dict[str, int],
    deltas: Dict[str, int],
    gross_cap: int,
    net_cap: int,
) -> bool:
    projected = dict(positions)
    for ticker, delta in deltas.items():
        projected[ticker] = int(projected.get(ticker, 0) + delta)
    gross, net = compute_gross_net(projected)
    if gross > int(gross_cap * RISK_BUFFER):
        return False
    if net > int(net_cap * RISK_BUFFER):
        return False
    return True


def compute_transaction_friction(
    ratio: float,
    t_bid: float,
    t_ask: float,
    a_bid: float,
    a_ask: float,
) -> float:
    target_half_spread = max(0.0, (t_ask - t_bid) / 2.0)
    acq_half_spread = max(0.0, (a_ask - a_bid) / 2.0)
    commission = COMMISSION_PER_SHARE * (1.0 + ratio)
    marketable_limit_penalty = MARKETABLE_LIMIT_OFFSET * (1.0 + ratio)
    return commission + target_half_spread + ratio * acq_half_spread + marketable_limit_penalty


def inventory_adjusted_threshold(
    base_threshold: float,
    target_pos: int,
    action: str,
    gross_used: int,
    net_used: int,
) -> float:
    direction = 1 if action == "BUY" else -1
    reducing = target_pos * direction < 0
    if reducing:
        return max(MIN_REDUCE_THRESHOLD, base_threshold * REDUCE_THRESHOLD_MULT)

    inv_util = min(1.0, abs(target_pos) / max(1.0, NET_LIMIT * 0.5))
    gross_util = min(1.0, gross_used / max(1.0, GROSS_LIMIT))
    net_util = min(1.0, net_used / max(1.0, NET_LIMIT))
    global_util = max(gross_util, net_util)
    return base_threshold * (1.0 + ADD_INVENTORY_SLOPE * inv_util + ADD_GLOBAL_SLOPE * global_util)


def close_qty_for_position(position_abs: int) -> int:
    qty = min(MAX_ORDER_SIZE, position_abs)
    qty = max(ORDER_STEP, qty - (qty % ORDER_STEP))
    return min(position_abs, qty)


def compute_hedge_close_qty(
    target_close_action: str,
    ratio: float,
    target_close_qty: int,
    acquirer_position: int,
) -> int:
    desired = int(round(ratio * target_close_qty))
    if desired <= 0:
        return 0
    if target_close_action == "SELL":
        # Closing long target; if hedge left us short acquirer, buy it back.
        if acquirer_position < 0:
            return min(desired, abs(acquirer_position), MAX_ORDER_SIZE)
        return 0
    # Closing short target; if hedge left us long acquirer, sell it down.
    if acquirer_position > 0:
        return min(desired, acquirer_position, MAX_ORDER_SIZE)
    return 0


def per_deal_acq_cap(ratio: float) -> int:
    return max(
        PER_DEAL_TARGET_MAX,
        int(round(PER_DEAL_TARGET_MAX * max(1.0, ratio) * PER_DEAL_ACQ_CAP_MULT)),
    )


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
    """Return target_qty, hedge_qty that satisfy order size and risk limits."""
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
        hedge_delta = 0
        if hedge_qty > 0:
            # If target BUY (deal spread tightening), hedge by SELL acquirer.
            hedge_delta = -hedge_qty if action == "BUY" else hedge_qty
        deltas = {target_ticker: target_delta}
        if hedge_qty > 0:
            deltas[acquirer_ticker] = hedge_delta
        if project_limits_ok(positions, deltas, GROSS_LIMIT, NET_LIMIT):
            return qty, hedge_qty
        qty -= ORDER_STEP

    return 0, 0


def snapshot_mid(client: RITClient, tickers: Iterable[str]) -> Dict[str, float]:
    mids: Dict[str, float] = {}
    for ticker in tickers:
        book = client.get_book(ticker)
        bid, ask = best_bid_ask_from_book(book)
        if bid is None or ask is None:
            raise RuntimeError(f"Missing bid/ask for {ticker} during initialization.")
        mids[ticker] = (bid + ask) / 2.0
    return mids


def average_snapshot_mid(
    client: RITClient,
    tickers: Iterable[str],
    samples: int,
    sleep_s: float,
) -> Dict[str, float]:
    sums: Dict[str, float] = {t: 0.0 for t in tickers}
    count = 0
    for _ in range(max(1, samples)):
        mids = snapshot_mid(client, tickers)
        for ticker, mid in mids.items():
            sums[ticker] += mid
        count += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return {ticker: sums[ticker] / max(1, count) for ticker in sums}


@dataclass
class DealState:
    probability: float
    standalone_value: float
    last_trade_ts: float = 0.0


class MergerArbBot:
    def __init__(self, client: RITClient):
        self.client = client
        self.deal_states: Dict[str, DealState] = {}
        self.lock = threading.Lock()
        self.running = True
        self.last_news_id = 0
        self.last_snapshot_ts = 0.0
        self.deal_ticker_to_id: Dict[str, str] = {}
        self.trade_tickers: List[str] = []
        self.book_executor: Optional[ThreadPoolExecutor] = None
        self.order_executor: Optional[ThreadPoolExecutor] = None
        self.order_meta_lock = threading.Lock()
        self.open_order_meta: Dict[int, Tuple[float, str, str]] = {}
        self.last_stale_check_ts = 0.0
        self.finbert = None
        self.finbert_enabled = False
        self.finbert_model_path: Optional[str] = None
        self.finbert_tokenizer_path: Optional[str] = None

        for deal_id, deal in DEALS.items():
            self.deal_ticker_to_id[deal["target"].upper()] = deal_id
            self.deal_ticker_to_id[deal["acquirer"].upper()] = deal_id
        self.trade_tickers = sorted(self.deal_ticker_to_id.keys())

    def initialize(self) -> None:
        case = self.client.get_case()
        if case.get("status") != "ACTIVE":
            log(f"Case status is {case.get('status')}. Waiting for ACTIVE...")
            while True:
                time.sleep(CASE_POLL_SECS)
                case = self.client.get_case()
                if case.get("status") == "ACTIVE":
                    break

        if INIT_WARMUP_SECS > 0:
            log(f"INIT warmup for {INIT_WARMUP_SECS:.1f}s to stabilize opening spread.")
            time.sleep(INIT_WARMUP_SECS)

        mids = average_snapshot_mid(
            self.client,
            self.trade_tickers,
            samples=INIT_SNAPSHOTS,
            sleep_s=INIT_SAMPLE_INTERVAL_SECS,
        )
        with self.lock:
            for deal_id, deal in DEALS.items():
                target = deal["target"].upper()
                acquirer = deal["acquirer"].upper()
                p0 = float(deal["p0"])
                k0 = deal_value(deal, mids[acquirer])
                v0 = infer_standalone_value(mids[target], p0, k0)
                self.deal_states[deal_id] = DealState(probability=p0, standalone_value=v0)
                log(
                    f"INIT {deal_id} | target={target} mid0={mids[target]:.2f} "
                    f"acq={acquirer} mid0={mids[acquirer]:.2f} K0={k0:.2f} V0={v0:.2f} p0={p0:.3f}"
                )

    def _fetch_books_parallel(self) -> Dict[str, Tuple[float, float, float]]:
        books: Dict[str, Tuple[float, float, float]] = {}

        def one(ticker: str) -> Tuple[str, Optional[Tuple[float, float, float]]]:
            try:
                b = self.client.get_book(ticker)
                bid, ask = best_bid_ask_from_book(b)
                if bid is None or ask is None:
                    return ticker, None
                return ticker, (bid, ask, (bid + ask) / 2.0)
            except Exception:
                return ticker, None

        executor = self.book_executor
        if executor is None:
            max_workers = max(2, min(BOOK_WORKERS, len(self.trade_tickers)))
            executor = ThreadPoolExecutor(max_workers=max_workers)
            self.book_executor = executor
        for ticker, res in executor.map(one, self.trade_tickers):
            if res is not None:
                books[ticker] = res
        return books

    def _safe_positions(self) -> Dict[str, int]:
        sec = self.client.get_securities()
        return {s["ticker"].upper(): int(s.get("position", 0)) for s in sec}

    def _initialize_finbert(self) -> None:
        if not USE_FINBERT:
            return
        model_path, tokenizer_path, note = _detect_finbert_assets(FINBERT_ONNX_MODEL, FINBERT_TOKENIZER_DIR)
        if model_path is None or tokenizer_path is None:
            log(f"FINBERT disabled: {note}", level="WARN")
            return
        trader_cls, err = _load_finbert_trader_class()
        if trader_cls is None:
            log(f"FINBERT unavailable: {err}", level="WARN")
            return
        try:
            self.finbert = trader_cls(
                onnx_model_path=str(model_path),
                tokenizer_dir=str(tokenizer_path),
                max_length=FINBERT_MAX_LENGTH,
            )
            self.finbert_enabled = True
            self.finbert_model_path = str(model_path)
            self.finbert_tokenizer_path = str(tokenizer_path)
            log(
                f"FINBERT enabled model={model_path} tokenizer={tokenizer_path} "
                f"pos_thr={FINBERT_POS_THRESHOLD:.2f} neg_thr={FINBERT_NEG_THRESHOLD:.2f}",
            )
        except Exception as exc:
            self.finbert = None
            self.finbert_enabled = False
            self.finbert_model_path = None
            self.finbert_tokenizer_path = None
            log(f"FINBERT init failed: {exc}", level="WARN")

    def _finbert_infer_direction(self, text: str) -> Tuple[Optional[str], str, Optional[dict]]:
        if self.finbert is None:
            return None, "S", None
        try:
            probs = self.finbert.predict(text)
        except Exception as exc:
            log(f"FINBERT inference error: {exc}", level="WARN")
            return None, "S", {"error": str(exc)}

        p_pos = float(probs.get("positive_probability", 0.0))
        p_neg = float(probs.get("negative_probability", 0.0))
        gap = abs(p_pos - p_neg)

        direction = None
        if p_pos >= FINBERT_POS_THRESHOLD and (p_pos - p_neg) >= FINBERT_GAP_THRESHOLD:
            direction = "POS"
        elif p_neg >= FINBERT_NEG_THRESHOLD and (p_neg - p_pos) >= FINBERT_GAP_THRESHOLD:
            direction = "NEG"

        confidence = max(p_pos, p_neg)
        if confidence >= FINBERT_SEV_LARGE:
            severity = "L"
        elif confidence >= FINBERT_SEV_MEDIUM:
            severity = "M"
        else:
            severity = "S"

        return direction, severity, {
            "positive_probability": round(p_pos, 6),
            "negative_probability": round(p_neg, 6),
            "gap": round(gap, 6),
            "confidence": round(confidence, 6),
        }

    def _marketable_limit_price(self, action: str, bid: float, ask: float) -> float:
        if action == "BUY":
            return round(ask + MARKETABLE_LIMIT_OFFSET, 2)
        return round(max(0.01, bid - MARKETABLE_LIMIT_OFFSET), 2)

    def _track_order(self, response: Optional[dict], ticker: str, reason: str) -> None:
        if not response:
            return
        order_id = response.get("order_id")
        if order_id is None:
            return
        try:
            oid = int(order_id)
        except Exception:
            return
        with self.order_meta_lock:
            self.open_order_meta[oid] = (time.time(), ticker, reason)

    def _cancel_stale_orders(self, now: float) -> None:
        if now - self.last_stale_check_ts < STALE_CANCEL_CHECK_SECS:
            return
        self.last_stale_check_ts = now
        try:
            open_orders = self.client.get_orders(status="OPEN")
        except Exception as exc:
            log(f"ORDERS_WARN unable to poll open orders: {exc}")
            return

        open_ids = set()
        for order in open_orders:
            oid = order.get("order_id")
            if oid is None:
                continue
            try:
                open_ids.add(int(oid))
            except Exception:
                continue

        with self.order_meta_lock:
            tracked_ids = set(self.open_order_meta.keys())
            for oid in tracked_ids - open_ids:
                self.open_order_meta.pop(oid, None)
            stale = [(oid, meta) for oid, meta in self.open_order_meta.items() if oid in open_ids and now - meta[0] >= STALE_ORDER_SECS]

        for oid, (_, ticker, reason) in stale:
            try:
                self.client.cancel_order(oid)
                log(f"CANCEL stale order_id={oid} ticker={ticker} reason={reason} age>{STALE_ORDER_SECS:.2f}s")
            except Exception:
                pass
            finally:
                with self.order_meta_lock:
                    self.open_order_meta.pop(oid, None)

    def _submit_pair(
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

        if self.order_executor is None:
            self.order_executor = ThreadPoolExecutor(max_workers=4)

        start = time.perf_counter()
        tgt_resp = None
        hedge_resp = None

        if hedge_qty > 0 and SIMULTANEOUS_LEGS:
            f_target = self.order_executor.submit(
                self.client.place_order,
                ticker=target,
                action=signal_action,
                quantity=target_qty,
                order_type="LIMIT",
                price=target_px,
            )
            f_hedge = self.order_executor.submit(
                self.client.place_order,
                ticker=acquirer,
                action=hedge_side,
                quantity=hedge_qty,
                order_type="LIMIT",
                price=hedge_px,
            )
            try:
                tgt_resp = f_target.result()
                self._track_order(tgt_resp, target, reason)
            except Exception as exc:
                log(
                    f"ORDER_FAIL {deal_id} reason={reason} target={target} side={signal_action} "
                    f"qty={target_qty} px={target_px:.2f} err={exc}"
                )
            try:
                hedge_resp = f_hedge.result()
                self._track_order(hedge_resp, acquirer, reason)
            except Exception as exc:
                log(
                    f"HEDGE_FAIL {deal_id} reason={reason} acq={acquirer} side={hedge_side} "
                    f"qty={hedge_qty} px={hedge_px:.2f} err={exc}"
                )
        else:
            try:
                tgt_resp = self.client.place_order(
                    ticker=target,
                    action=signal_action,
                    quantity=target_qty,
                    order_type="LIMIT",
                    price=target_px,
                )
                self._track_order(tgt_resp, target, reason)
            except Exception as exc:
                log(
                    f"ORDER_FAIL {deal_id} reason={reason} target={target} side={signal_action} "
                    f"qty={target_qty} px={target_px:.2f} err={exc}"
                )
                return False, False
            if hedge_qty > 0:
                try:
                    hedge_resp = self.client.place_order(
                        ticker=acquirer,
                        action=hedge_side,
                        quantity=hedge_qty,
                        order_type="LIMIT",
                        price=hedge_px,
                    )
                    self._track_order(hedge_resp, acquirer, reason)
                except Exception as exc:
                    log(
                        f"HEDGE_FAIL {deal_id} reason={reason} acq={acquirer} side={hedge_side} "
                        f"qty={hedge_qty} px={hedge_px:.2f} err={exc}"
                    )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        log(
            f"TRADE {deal_id} reason={reason} side={signal_action} tgt={target} qty={target_qty}@{target_px:.2f} "
            f"hedge={acquirer}:{hedge_side if hedge_qty > 0 else 'NONE'}:{hedge_qty}"
            f"{f'@{hedge_px:.2f}' if hedge_qty > 0 and hedge_px is not None else ''} edge={edge:.3f} "
            f"p*={p_star:.3f} bid/ask={t_bid:.2f}/{t_ask:.2f} "
            f"lat={elapsed_ms:.1f}ms tgt_id={(tgt_resp or {}).get('order_id')} "
            f"hedge_id={(hedge_resp or {}).get('order_id')}"
        )
        return tgt_resp is not None, (hedge_resp is not None if hedge_qty > 0 else False)

    def _submit_single(
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
        start = time.perf_counter()
        try:
            resp = self.client.place_order(
                ticker=ticker,
                action=action,
                quantity=qty,
                order_type="LIMIT",
                price=px,
            )
            self._track_order(resp, ticker, reason)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            log(
                f"TRADE {deal_id} reason={reason} side={action} single={ticker} qty={qty}@{px:.2f} "
                f"lat={elapsed_ms:.1f}ms order_id={resp.get('order_id')}"
            )
            return True
        except Exception as exc:
            log(
                f"ORDER_FAIL {deal_id} reason={reason} single={ticker} side={action} qty={qty} px={px:.2f} err={exc}"
            )
            return False

    def news_worker(self) -> None:
        idle_errors = 0
        while self.running:
            try:
                news = self.client.get_news(since=self.last_news_id, limit=40)
                if not news:
                    idle_errors = 0
                    time.sleep(NEWS_POLL_SECS)
                    continue

                news_sorted = sorted(news, key=lambda x: x.get("news_id", 0))
                for item in news_sorted:
                    news_id = int(item.get("news_id", 0))
                    if news_id <= self.last_news_id:
                        continue

                    headline = str(item.get("headline") or "")
                    body = str(item.get("body") or "")
                    text = (headline + " " + body).strip()
                    text_lower = text.lower()
                    refs = extract_referenced_deals(text, self.deal_ticker_to_id)

                    self.last_news_id = max(self.last_news_id, news_id)
                    if not refs:
                        if RUN_RECORDER is not None:
                            RUN_RECORDER.add_news(
                                item=item,
                                refs=[],
                                category=None,
                                direction=None,
                                severity=None,
                                applied=[],
                                skipped=True,
                                skip_reason="NO_DEAL_REFERENCE",
                            )
                        continue

                    cat = classify_category(text_lower)
                    keyword_direction = classify_direction(text_lower)
                    keyword_severity = classify_severity(text_lower)
                    direction = keyword_direction
                    severity = keyword_severity

                    finbert_direction = None
                    finbert_severity = "S"
                    finbert_meta = None
                    if self.finbert_enabled:
                        finbert_direction, finbert_severity, finbert_meta = self._finbert_infer_direction(text)
                        if direction is None and finbert_direction is not None:
                            direction = finbert_direction
                        elif (
                            direction is not None
                            and finbert_direction is not None
                            and finbert_direction != direction
                            and finbert_meta is not None
                            and float(finbert_meta.get("gap", 0.0)) >= FINBERT_OVERRIDE_GAP
                        ):
                            # Allow high-confidence model prediction to override ambiguous keyword polarity.
                            direction = finbert_direction
                        if finbert_severity == "L" or (finbert_severity == "M" and severity == "S"):
                            severity = finbert_severity
                        if cat is None and direction is not None and FINBERT_CATEGORY_FALLBACK in CATEGORY_MULT:
                            cat = FINBERT_CATEGORY_FALLBACK

                    classifier_meta = {
                        "keyword": {
                            "direction": keyword_direction,
                            "severity": keyword_severity,
                        },
                        "finbert": {
                            "enabled": self.finbert_enabled,
                            "direction": finbert_direction,
                            "severity": finbert_severity,
                            "meta": finbert_meta,
                        },
                    }

                    if cat is None or direction is None:
                        log(
                            f"NEWS_SKIP id={news_id} refs={','.join(refs)} "
                            f"cat={cat} dir={direction} sev={severity} head='{headline[:90]}'"
                        )
                        if RUN_RECORDER is not None:
                            RUN_RECORDER.add_news(
                                item=item,
                                refs=refs,
                                category=cat,
                                direction=direction,
                                severity=severity,
                                applied=[],
                                skipped=True,
                                skip_reason="CLASSIFICATION_INCOMPLETE",
                                classifier_meta=classifier_meta,
                            )
                        continue

                    base = BASE_CHANGE[(direction, severity)]
                    applied_updates: List[dict] = []
                    with self.lock:
                        for deal_id in refs:
                            deal = DEALS[deal_id]
                            st = self.deal_states[deal_id]
                            old_p = st.probability
                            delta = base * CATEGORY_MULT[cat] * float(deal["deal_mult"])
                            st.probability = clamp(st.probability + delta, 0.0, 1.0)
                            applied_updates.append(
                                {
                                    "deal_id": deal_id,
                                    "old_p": round(old_p, 6),
                                    "delta": round(delta, 6),
                                    "new_p": round(st.probability, 6),
                                }
                            )
                            log(
                                f"NEWS id={news_id} deal={deal_id} cat={cat} dir={direction} sev={severity} "
                                f"delta={delta:+.4f} p:{old_p:.4f}->{st.probability:.4f} "
                                f"head='{headline[:100]}'"
                            )
                    if RUN_RECORDER is not None:
                        RUN_RECORDER.add_news(
                            item=item,
                            refs=refs,
                            category=cat,
                            direction=direction,
                            severity=severity,
                            applied=applied_updates,
                            skipped=False,
                            classifier_meta=classifier_meta,
                        )

                idle_errors = 0
                time.sleep(NEWS_POLL_SECS)
            except Exception as exc:
                idle_errors += 1
                sleep_s = min(1.0, 0.05 * (2 ** min(idle_errors, 4)))
                log(f"NEWS_ERR count={idle_errors} sleep={sleep_s:.2f}s err={exc}")
                time.sleep(sleep_s)

    def manual_override_worker(self) -> None:
        log(
            "Manual override ready. Examples: 'D1 POS L', 'D1 POS L REG', 'D3 P 0.72', 'HELP'."
        )
        while self.running:
            try:
                line = input().strip()
            except EOFError:
                return
            except Exception as exc:
                log(f"MANUAL_ERR input failure: {exc}")
                time.sleep(0.1)
                continue

            if not line:
                continue
            if line.upper() in {"HELP", "H", "?"}:
                log("Manual formats: D# POS|NEG S|M|L [REG|FIN|SHR|ALT|PRC]  OR  D# P <0..1>")
                continue

            parts = line.upper().split()
            if len(parts) == 3 and parts[1] == "P":
                deal_id = parts[0]
                if deal_id not in DEALS:
                    log(f"MANUAL_SKIP invalid deal '{deal_id}'.")
                    continue
                try:
                    p_new = clamp(float(parts[2]), 0.0, 1.0)
                except ValueError:
                    log("MANUAL_SKIP invalid probability. Use e.g. 'D2 P 0.67'.")
                    continue
                with self.lock:
                    old_p = self.deal_states[deal_id].probability
                    self.deal_states[deal_id].probability = p_new
                log(f"MANUAL_SET deal={deal_id} p:{old_p:.4f}->{p_new:.4f}")
                continue

            if len(parts) not in {3, 4}:
                log("MANUAL_SKIP unknown format. Type HELP.")
                continue

            deal_id, direction, severity = parts[0], parts[1], parts[2]
            category = parts[3] if len(parts) == 4 else "FIN"
            if deal_id not in DEALS:
                log(f"MANUAL_SKIP invalid deal '{deal_id}'.")
                continue
            if direction not in {"POS", "NEG"}:
                log("MANUAL_SKIP direction must be POS or NEG.")
                continue
            if severity not in {"S", "M", "L"}:
                log("MANUAL_SKIP severity must be S, M, or L.")
                continue
            if category not in CATEGORY_MULT:
                log("MANUAL_SKIP category must be REG/FIN/SHR/ALT/PRC.")
                continue

            delta = BASE_CHANGE[(direction, severity)] * CATEGORY_MULT[category] * float(DEALS[deal_id]["deal_mult"])
            with self.lock:
                old_p = self.deal_states[deal_id].probability
                new_p = clamp(old_p + delta, 0.0, 1.0)
                self.deal_states[deal_id].probability = new_p
            log(
                f"MANUAL_DELTA deal={deal_id} dir={direction} sev={severity} cat={category} "
                f"delta={delta:+.4f} p:{old_p:.4f}->{new_p:.4f}"
            )

    def _periodic_snapshot(
        self,
        books: Dict[str, Tuple[float, float, float]],
        positions: Dict[str, int],
    ) -> None:
        now = time.time()
        if now - self.last_snapshot_ts < SNAPSHOT_SECS:
            return
        self.last_snapshot_ts = now

        gross, net = compute_gross_net(positions)
        log(f"RISK gross={gross}/{GROSS_LIMIT} net={net}/{NET_LIMIT}")

        with self.lock:
            for deal_id, deal in DEALS.items():
                target = deal["target"].upper()
                acquirer = deal["acquirer"].upper()
                if target not in books or acquirer not in books:
                    continue
                st = self.deal_states[deal_id]
                _, _, a_mid = books[acquirer]
                t_bid, t_ask, _ = books[target]
                k = deal_value(deal, a_mid)
                p_star = st.probability * k + (1.0 - st.probability) * st.standalone_value
                log(
                    f"MODEL {deal_id} p={st.probability:.4f} V={st.standalone_value:.2f} "
                    f"K={k:.2f} P*={p_star:.2f} tgt_bid/ask={t_bid:.2f}/{t_ask:.2f}"
                )

    def trade_loop(self) -> None:
        loop_errors = 0
        # Per-loop aliases for fast tuning and readability.
        MIN_PROFIT_MARGIN = DESIRED_PROFIT_MARGIN
        EXIT_BUFFER = TAKE_PROFIT_BUFFER
        while self.running:
            try:
                case = self.client.get_case()
                if case.get("status") != "ACTIVE":
                    log(f"Case status={case.get('status')} - stopping.")
                    self.running = False
                    break

                positions = self._safe_positions()
                books = self._fetch_books_parallel()
                if len(books) < len(self.trade_tickers):
                    missing = sorted(set(self.trade_tickers) - set(books.keys()))
                    if missing:
                        log(f"BOOK_WARN missing={','.join(missing)}")

                self._periodic_snapshot(books, positions)

                now = time.time()
                self._cancel_stale_orders(now)
                with self.lock:
                    states_copy = {
                        deal_id: DealState(
                            probability=st.probability,
                            standalone_value=st.standalone_value,
                            last_trade_ts=st.last_trade_ts,
                        )
                        for deal_id, st in self.deal_states.items()
                    }

                gross_used, net_used = compute_gross_net(positions)

                for deal_id, deal in DEALS.items():
                    target = deal["target"].upper()
                    acquirer = deal["acquirer"].upper()
                    if target not in books or acquirer not in books:
                        continue

                    st = states_copy[deal_id]
                    can_enter = (now - st.last_trade_ts) >= TRADE_COOLDOWN_SECS

                    t_bid, t_ask, _ = books[target]
                    a_bid, a_ask, a_mid = books[acquirer]
                    k = deal_value(deal, a_mid)
                    p_star = st.probability * k + (1.0 - st.probability) * st.standalone_value
                    ratio = float(deal["ratio"]) if deal["structure"] in {"stock", "mixed"} else 0.0
                    commission_cost = COMMISSION_PER_SHARE * (1.0 + ratio)
                    target_pos = int(positions.get(target, 0))
                    acq_pos = int(positions.get(acquirer, 0))

                    # Exit logic: flatten when price converges back to model.
                    if target_pos > 0 and t_bid >= p_star - commission_cost - EXIT_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("SELL", ratio, close_qty, acq_pos)
                        target_ok, hedge_ok = self._submit_pair(
                            deal_id=deal_id,
                            target=target,
                            acquirer=acquirer,
                            signal_action="SELL",
                            target_qty=close_qty,
                            hedge_qty=hedge_close_qty,
                            edge=t_bid - p_star,
                            p_star=p_star,
                            t_bid=t_bid,
                            t_ask=t_ask,
                            a_bid=a_bid,
                            a_ask=a_ask,
                            reason="TAKE_PROFIT_LONG",
                        )
                        if target_ok:
                            positions[target] = target_pos - close_qty
                        if hedge_close_qty > 0 and hedge_ok:
                            positions[acquirer] = acq_pos + hedge_close_qty
                        if target_ok or hedge_ok:
                            gross_used, net_used = compute_gross_net(positions)
                            with self.lock:
                                self.deal_states[deal_id].last_trade_ts = now
                        continue

                    if target_pos < 0 and t_ask <= p_star + commission_cost + EXIT_BUFFER:
                        close_qty = close_qty_for_position(abs(target_pos))
                        hedge_close_qty = compute_hedge_close_qty("BUY", ratio, close_qty, acq_pos)
                        target_ok, hedge_ok = self._submit_pair(
                            deal_id=deal_id,
                            target=target,
                            acquirer=acquirer,
                            signal_action="BUY",
                            target_qty=close_qty,
                            hedge_qty=hedge_close_qty,
                            edge=p_star - t_ask,
                            p_star=p_star,
                            t_bid=t_bid,
                            t_ask=t_ask,
                            a_bid=a_bid,
                            a_ask=a_ask,
                            reason="TAKE_PROFIT_SHORT",
                        )
                        if target_ok:
                            positions[target] = target_pos + close_qty
                        if hedge_close_qty > 0 and hedge_ok:
                            positions[acquirer] = acq_pos - hedge_close_qty
                        if target_ok or hedge_ok:
                            gross_used, net_used = compute_gross_net(positions)
                            with self.lock:
                                self.deal_states[deal_id].last_trade_ts = now
                        continue

                    # If target is flat but a leftover hedge remains, unwind hedge leg.
                    if target_pos == 0 and ratio > 0 and abs(acq_pos) >= ORDER_STEP:
                        hedge_unwind_qty = close_qty_for_position(abs(acq_pos))
                        hedge_unwind_action = "SELL" if acq_pos > 0 else "BUY"
                        unwind_ok = self._submit_single(
                            deal_id=deal_id,
                            ticker=acquirer,
                            action=hedge_unwind_action,
                            qty=hedge_unwind_qty,
                            bid=a_bid,
                            ask=a_ask,
                            reason="ORPHAN_HEDGE_UNWIND",
                        )
                        if unwind_ok:
                            positions[acquirer] = acq_pos - hedge_unwind_qty if acq_pos > 0 else acq_pos + hedge_unwind_qty
                            gross_used, net_used = compute_gross_net(positions)
                            with self.lock:
                                self.deal_states[deal_id].last_trade_ts = now
                        continue

                    # Rebalance hedge drift when target leg exists but acquirer ratio has deviated.
                    if ratio > 0 and target_pos != 0:
                        desired_acq_pos = int(round(-ratio * target_pos))
                        hedge_gap = acq_pos - desired_acq_pos
                        if abs(hedge_gap) >= HEDGE_REBALANCE_TRIGGER:
                            rebalance_action = "SELL" if hedge_gap > 0 else "BUY"
                            rebalance_qty = min(MAX_ORDER_SIZE, abs(hedge_gap))
                            rebalance_qty = max(ORDER_STEP, rebalance_qty - (rebalance_qty % ORDER_STEP))
                            rebalance_qty = min(abs(hedge_gap), rebalance_qty)
                            if rebalance_qty > 0:
                                acq_delta = rebalance_qty if rebalance_action == "BUY" else -rebalance_qty
                                if project_limits_ok(positions, {acquirer: acq_delta}, GROSS_LIMIT, NET_LIMIT):
                                    rebalance_ok = self._submit_single(
                                        deal_id=deal_id,
                                        ticker=acquirer,
                                        action=rebalance_action,
                                        qty=rebalance_qty,
                                        bid=a_bid,
                                        ask=a_ask,
                                        reason="HEDGE_REBALANCE",
                                    )
                                    if rebalance_ok:
                                        positions[acquirer] = acq_pos + acq_delta
                                        gross_used, net_used = compute_gross_net(positions)
                                        with self.lock:
                                            self.deal_states[deal_id].last_trade_ts = now
                                    continue

                    if not can_enter:
                        continue

                    # Entry / add-reduce logic with dynamic friction-aware threshold.
                    action = None
                    edge = 0.0
                    if t_ask < p_star:
                        action = "BUY"
                        edge = p_star - t_ask
                    elif t_bid > p_star:
                        action = "SELL"
                        edge = t_bid - p_star
                    if action is None:
                        continue

                    friction = compute_transaction_friction(ratio, t_bid, t_ask, a_bid, a_ask)
                    base_threshold = max(MISPRICING_THRESHOLD, friction + MIN_PROFIT_MARGIN)
                    dyn_threshold = inventory_adjusted_threshold(
                        base_threshold=base_threshold,
                        target_pos=target_pos,
                        action=action,
                        gross_used=gross_used,
                        net_used=net_used,
                    )
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

                    target_qty, hedge_qty = scale_target_qty(
                        ratio=ratio,
                        seed_qty=seed_qty,
                        action=action,
                        target_ticker=target,
                        acquirer_ticker=acquirer,
                        positions=positions,
                        max_target_qty=max_target_qty,
                    )
                    if target_qty < MIN_ORDER_QTY:
                        continue

                    target_ok, hedge_ok = self._submit_pair(
                        deal_id=deal_id,
                        target=target,
                        acquirer=acquirer,
                        signal_action=action,
                        target_qty=target_qty,
                        hedge_qty=hedge_qty,
                        edge=edge,
                        p_star=p_star,
                        t_bid=t_bid,
                        t_ask=t_ask,
                        a_bid=a_bid,
                        a_ask=a_ask,
                        reason="ENTRY",
                    )

                    # Project local position for this loop so multiple signals do not over-allocate.
                    if target_ok:
                        positions[target] = int(positions.get(target, 0) + (target_qty if action == "BUY" else -target_qty))
                    if hedge_qty > 0 and hedge_ok:
                        hedge_delta = -hedge_qty if action == "BUY" else hedge_qty
                        positions[acquirer] = int(positions.get(acquirer, 0) + hedge_delta)
                    if target_ok or hedge_ok:
                        gross_used, net_used = compute_gross_net(positions)
                        with self.lock:
                            self.deal_states[deal_id].last_trade_ts = now

                loop_errors = 0
                time.sleep(TRADE_LOOP_SECS)
            except Exception as exc:
                loop_errors += 1
                sleep_s = min(1.0, 0.05 * (2 ** min(loop_errors, 4)))
                log(f"TRADE_ERR count={loop_errors} sleep={sleep_s:.2f}s err={exc}")
                time.sleep(sleep_s)

    def run(self) -> None:
        if API_KEY == "YOUR_API_KEY":
            raise RuntimeError("Set RIT_API_KEY before running.")

        if RUN_RECORDER is not None:
            RUN_RECORDER.set_context(
                {
                    "deals": DEALS,
                    "config": {
                        "MISPRICING_THRESHOLD": MISPRICING_THRESHOLD,
                        "DESIRED_PROFIT_MARGIN": DESIRED_PROFIT_MARGIN,
                        "COMMISSION_PER_SHARE": COMMISSION_PER_SHARE,
                        "MARKETABLE_LIMIT_OFFSET": MARKETABLE_LIMIT_OFFSET,
                        "BASE_ORDER_QTY": BASE_ORDER_QTY,
                        "MIN_ORDER_QTY": MIN_ORDER_QTY,
                        "MAX_ORDER_SIZE": MAX_ORDER_SIZE,
                        "GROSS_LIMIT": GROSS_LIMIT,
                        "NET_LIMIT": NET_LIMIT,
                        "PER_DEAL_TARGET_MAX": PER_DEAL_TARGET_MAX,
                        "HEDGE_REBALANCE_TRIGGER": HEDGE_REBALANCE_TRIGGER,
                        "STALE_ORDER_SECS": STALE_ORDER_SECS,
                        "TRADE_LOOP_SECS": TRADE_LOOP_SECS,
                        "NEWS_POLL_SECS": NEWS_POLL_SECS,
                        "USE_FINBERT": USE_FINBERT,
                        "FINBERT_ONNX_MODEL": FINBERT_ONNX_MODEL,
                        "FINBERT_TOKENIZER_DIR": FINBERT_TOKENIZER_DIR,
                        "FINBERT_MAX_LENGTH": FINBERT_MAX_LENGTH,
                        "FINBERT_POS_THRESHOLD": FINBERT_POS_THRESHOLD,
                        "FINBERT_NEG_THRESHOLD": FINBERT_NEG_THRESHOLD,
                        "FINBERT_GAP_THRESHOLD": FINBERT_GAP_THRESHOLD,
                        "FINBERT_OVERRIDE_GAP": FINBERT_OVERRIDE_GAP,
                        "FINBERT_CATEGORY_FALLBACK": FINBERT_CATEGORY_FALLBACK,
                    },
                }
            )

        self._initialize_finbert()
        self.initialize()
        log(
            "Bot started. "
            f"threshold={MISPRICING_THRESHOLD:.3f} margin={DESIRED_PROFIT_MARGIN:.3f} "
            f"base_qty={BASE_ORDER_QTY} max_order={MAX_ORDER_SIZE} "
            f"gross/net={GROSS_LIMIT}/{NET_LIMIT} limit_offset={MARKETABLE_LIMIT_OFFSET:.2f} "
            f"per_deal_target_cap={PER_DEAL_TARGET_MAX} stale_cancel={STALE_ORDER_SECS:.2f}s "
            f"finbert={'ON' if self.finbert_enabled else 'OFF'}"
        )

        news_thread = threading.Thread(target=self.news_worker, name="news-worker", daemon=True)
        news_thread.start()
        manual_thread = None
        if ENABLE_MANUAL_OVERRIDE:
            manual_thread = threading.Thread(
                target=self.manual_override_worker,
                name="manual-override-worker",
                daemon=True,
            )
            manual_thread.start()

        try:
            self.trade_loop()
        finally:
            self.running = False
            news_thread.join(timeout=1.0)
            if manual_thread is not None:
                manual_thread.join(timeout=0.2)
            if self.book_executor is not None:
                self.book_executor.shutdown(wait=False, cancel_futures=True)
            if self.order_executor is not None:
                self.order_executor.shutdown(wait=False, cancel_futures=True)
            log("Stopped.")
            if RUN_RECORDER is not None:
                final_case = {}
                final_positions = {}
                final_open_orders = []
                try:
                    final_case = self.client.get_case()
                except Exception:
                    pass
                try:
                    final_positions = self._safe_positions()
                except Exception:
                    pass
                try:
                    final_open_orders = self.client.get_orders(status="OPEN")
                except Exception:
                    pass

                deal_state_summary = {}
                with self.lock:
                    for deal_id, st in self.deal_states.items():
                        deal_state_summary[deal_id] = {
                            "probability": round(st.probability, 6),
                            "standalone_value": round(st.standalone_value, 6),
                            "last_trade_ts": st.last_trade_ts,
                        }
                summary = {
                    "final_case": final_case,
                    "last_news_id": self.last_news_id,
                    "deal_states": deal_state_summary,
                    "final_positions": final_positions,
                    "open_order_count": len(final_open_orders),
                    "tracked_open_order_count": len(self.open_order_meta),
                    "finbert_enabled": self.finbert_enabled,
                    "finbert_model_path": self.finbert_model_path,
                    "finbert_tokenizer_path": self.finbert_tokenizer_path,
                }
                try:
                    out_path = RUN_RECORDER.flush(summary=summary)
                    print(f"{now_ts()} | RUN_LOG_JSON saved to {out_path}", flush=True)
                except Exception as exc:
                    print(f"{now_ts()} | RUN_LOG_JSON write failed: {exc}", flush=True)


def main() -> None:
    global RUN_RECORDER
    if WRITE_RUN_JSON:
        RUN_RECORDER = RunRecorder(base_url=BASE_URL)
    client = RITClient(api_key=API_KEY, base_url=BASE_URL)
    bot = MergerArbBot(client)
    bot.run()


if __name__ == "__main__":
    main()
