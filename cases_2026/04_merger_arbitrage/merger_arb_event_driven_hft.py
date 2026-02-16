"""Event-driven high-frequency merger arbitrage bot for RIT REST API.

Run:
    RIT_API_KEY=... python merger_arb_event_driven_hft.py
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import requests

BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")

# Timing / execution
NEWS_POLL_SECS = float(os.environ.get("RIT_MA_NEWS_POLL_SECS", "0.15"))
TRADE_LOOP_SECS = float(os.environ.get("RIT_MA_TRADE_LOOP_SECS", "0.12"))
BOOK_WORKERS = int(os.environ.get("RIT_MA_BOOK_WORKERS", "10"))
CASE_POLL_SECS = float(os.environ.get("RIT_MA_CASE_POLL_SECS", "0.25"))
TRADE_COOLDOWN_SECS = float(os.environ.get("RIT_MA_TRADE_COOLDOWN_SECS", "0.20"))
SNAPSHOT_SECS = float(os.environ.get("RIT_MA_SNAPSHOT_SECS", "2.0"))

# Thresholds / size
MISPRICING_THRESHOLD = float(os.environ.get("RIT_MA_THRESHOLD", "0.18"))
BASE_ORDER_QTY = int(os.environ.get("RIT_MA_BASE_ORDER_QTY", "2000"))
MIN_ORDER_QTY = int(os.environ.get("RIT_MA_MIN_ORDER_QTY", "500"))
ORDER_STEP = int(os.environ.get("RIT_MA_ORDER_STEP", "100"))
MAX_ORDER_SIZE = 5000

# Risk limits from case package
GROSS_LIMIT = 100_000
NET_LIMIT = 50_000
RISK_BUFFER = float(os.environ.get("RIT_MA_RISK_BUFFER", "0.98"))

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
        "target": "EEC",
        "acquirer": "SPK",
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

# Explicit code parsing (works if RIT headline includes tags like "REG", "NEG", "L").
CATEGORY_RE = re.compile(r"\b(REG|FIN|SHR|ALT|PRC)\b", re.IGNORECASE)
DIRECTION_RE = re.compile(r"\b(POS|NEG|POSITIVE|NEGATIVE)\b", re.IGNORECASE)
SEVERITY_WORD_RE = re.compile(r"\b(SMALL|MEDIUM|LARGE)\b", re.IGNORECASE)
SEVERITY_TAG_RE = re.compile(r"\bSEV(?:ERITY)?\s*[:=]\s*(S|M|L)\b", re.IGNORECASE)
DEAL_RE = re.compile(r"\bD([1-5])\b", re.IGNORECASE)


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def log(msg: str) -> None:
    print(f"{now_ts()} | {msg}", flush=True)


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


def classify_direction(text_lower: str) -> Optional[str]:
    code_match = DIRECTION_RE.search(text_lower.upper())
    if code_match:
        token = code_match.group(1).upper()
        return "POS" if token.startswith("POS") else "NEG"
    pos_score = sum(1 for w in POSITIVE_WORDS if w in text_lower)
    neg_score = sum(1 for w in NEGATIVE_WORDS if w in text_lower)
    if pos_score == neg_score == 0:
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


def scale_target_qty(
    ratio: float,
    seed_qty: int,
    action: str,
    target_ticker: str,
    acquirer_ticker: str,
    positions: Dict[str, int],
) -> Tuple[int, int]:
    """Return target_qty, hedge_qty that satisfy order size and risk limits."""
    max_by_order = MAX_ORDER_SIZE
    if ratio > 0:
        max_by_order = min(max_by_order, int(MAX_ORDER_SIZE / ratio))
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

        mids = snapshot_mid(self.client, self.trade_tickers)
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
    ) -> None:
        # Execute target leg first, then hedge.
        start = time.perf_counter()
        try:
            tgt_resp = self.client.place_order(
                ticker=target,
                action=signal_action,
                quantity=target_qty,
                order_type="MARKET",
            )
        except Exception as exc:
            log(f"ORDER_FAIL {deal_id} target={target} side={signal_action} qty={target_qty} err={exc}")
            return

        hedge_side = None
        hedge_resp = None
        if hedge_qty > 0:
            hedge_side = "SELL" if signal_action == "BUY" else "BUY"
            try:
                hedge_resp = self.client.place_order(
                    ticker=acquirer,
                    action=hedge_side,
                    quantity=hedge_qty,
                    order_type="MARKET",
                )
            except Exception as exc:
                log(
                    f"HEDGE_FAIL {deal_id} acq={acquirer} side={hedge_side} qty={hedge_qty} "
                    f"after target fill err={exc}"
                )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        log(
            f"TRADE {deal_id} side={signal_action} tgt={target} qty={target_qty} "
            f"hedge={acquirer}:{hedge_side or 'NONE'}:{hedge_qty} edge={edge:.3f} "
            f"p*={p_star:.3f} bid/ask={t_bid:.2f}/{t_ask:.2f} "
            f"lat={elapsed_ms:.1f}ms tgt_id={tgt_resp.get('order_id')} "
            f"hedge_id={(hedge_resp or {}).get('order_id')}"
        )

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
                        continue

                    cat = classify_category(text_lower)
                    direction = classify_direction(text_lower)
                    severity = classify_severity(text_lower)
                    if cat is None or direction is None:
                        log(
                            f"NEWS_SKIP id={news_id} refs={','.join(refs)} "
                            f"cat={cat} dir={direction} sev={severity} head='{headline[:90]}'"
                        )
                        continue

                    base = BASE_CHANGE[(direction, severity)]
                    with self.lock:
                        for deal_id in refs:
                            deal = DEALS[deal_id]
                            st = self.deal_states[deal_id]
                            old_p = st.probability
                            delta = base * CATEGORY_MULT[cat] * float(deal["deal_mult"])
                            st.probability = clamp(st.probability + delta, 0.0, 1.0)
                            log(
                                f"NEWS id={news_id} deal={deal_id} cat={cat} dir={direction} sev={severity} "
                                f"delta={delta:+.4f} p:{old_p:.4f}->{st.probability:.4f} "
                                f"head='{headline[:100]}'"
                            )

                idle_errors = 0
                time.sleep(NEWS_POLL_SECS)
            except Exception as exc:
                idle_errors += 1
                sleep_s = min(1.0, 0.05 * (2 ** min(idle_errors, 4)))
                log(f"NEWS_ERR count={idle_errors} sleep={sleep_s:.2f}s err={exc}")
                time.sleep(sleep_s)

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
                with self.lock:
                    states_copy = {
                        deal_id: DealState(
                            probability=st.probability,
                            standalone_value=st.standalone_value,
                            last_trade_ts=st.last_trade_ts,
                        )
                        for deal_id, st in self.deal_states.items()
                    }

                for deal_id, deal in DEALS.items():
                    target = deal["target"].upper()
                    acquirer = deal["acquirer"].upper()
                    if target not in books or acquirer not in books:
                        continue

                    st = states_copy[deal_id]
                    if now - st.last_trade_ts < TRADE_COOLDOWN_SECS:
                        continue

                    t_bid, t_ask, _ = books[target]
                    _, _, a_mid = books[acquirer]
                    k = deal_value(deal, a_mid)
                    p_star = st.probability * k + (1.0 - st.probability) * st.standalone_value

                    action = None
                    edge = 0.0
                    if t_ask < p_star - MISPRICING_THRESHOLD:
                        action = "BUY"
                        edge = p_star - t_ask
                    elif t_bid > p_star + MISPRICING_THRESHOLD:
                        action = "SELL"
                        edge = t_bid - p_star
                    if action is None:
                        continue

                    edge_mult = max(1.0, min(4.0, edge / max(0.01, MISPRICING_THRESHOLD)))
                    seed_qty = int(BASE_ORDER_QTY * edge_mult)
                    ratio = float(deal["ratio"]) if deal["structure"] in {"stock", "mixed"} else 0.0
                    target_qty, hedge_qty = scale_target_qty(
                        ratio=ratio,
                        seed_qty=seed_qty,
                        action=action,
                        target_ticker=target,
                        acquirer_ticker=acquirer,
                        positions=positions,
                    )

                    if target_qty < MIN_ORDER_QTY:
                        continue

                    self._submit_pair(
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
                    )

                    # Project local position for this loop so multiple signals do not over-allocate.
                    positions[target] = int(positions.get(target, 0) + (target_qty if action == "BUY" else -target_qty))
                    if hedge_qty > 0:
                        hedge_delta = -hedge_qty if action == "BUY" else hedge_qty
                        positions[acquirer] = int(positions.get(acquirer, 0) + hedge_delta)

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

        self.initialize()
        log(
            "Bot started. "
            f"threshold={MISPRICING_THRESHOLD:.3f} base_qty={BASE_ORDER_QTY} "
            f"max_order={MAX_ORDER_SIZE} gross/net={GROSS_LIMIT}/{NET_LIMIT}"
        )

        news_thread = threading.Thread(target=self.news_worker, name="news-worker", daemon=True)
        news_thread.start()

        try:
            self.trade_loop()
        finally:
            self.running = False
            news_thread.join(timeout=1.0)
            if self.book_executor is not None:
                self.book_executor.shutdown(wait=False, cancel_futures=True)
            log("Stopped.")


def main() -> None:
    client = RITClient(api_key=API_KEY, base_url=BASE_URL)
    bot = MergerArbBot(client)
    bot.run()


if __name__ == "__main__":
    main()
