import os
import time
import re
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.getenv("RIT_API_KEY", "YOUR_API_KEY")
POLL_SECS = 0.5
PRICE_THRESHOLD = 0.25
ORDER_QTY = 1000

DEALS = {
    "D1": {"target": "TGX", "acquirer": "PHR", "structure": "CASH", "cash": 50.0, "ratio": 0.0, "p0": 0.70, "t_start": 43.70, "a_start": 47.50, "deal_mult": 1.00},
    "D2": {"target": "BYL", "acquirer": "CLD", "structure": "STOCK", "cash": 0.0, "ratio": 0.75, "p0": 0.55, "t_start": 43.50, "a_start": 79.30, "deal_mult": 1.05},
    "D3": {"target": "GGD", "acquirer": "PNR", "structure": "MIXED", "cash": 33.0, "ratio": 0.20, "p0": 0.50, "t_start": 31.50, "a_start": 59.80, "deal_mult": 1.10},
    "D4": {"target": "FSR", "acquirer": "ATB", "structure": "CASH", "cash": 40.0, "ratio": 0.0, "p0": 0.38, "t_start": 30.50, "a_start": 62.20, "deal_mult": 1.30},
    "D5": {"target": "SPK", "acquirer": "EEC", "structure": "STOCK", "cash": 0.0, "ratio": 1.20, "p0": 0.45, "t_start": 52.80, "a_start": 48.00, "deal_mult": 1.15},
}

CATEGORY_MULT = {"REG": 1.25, "FIN": 1.00}
BASE_IMPACT = {
    ("POS", "S"): 0.03,
    ("POS", "M"): 0.07,
    ("POS", "L"): 0.14,
    ("NEG", "S"): -0.04,
    ("NEG", "M"): -0.09,
    ("NEG", "L"): -0.18,
}

REG_WORDS = ["regulator", "antitrust", "doj", "cma", "competition", "approval", "remedy", "litigation"]
FIN_WORDS = ["financing", "funding", "credit", "debt", "loan", "capital", "leverage", "liquidity"]
POS_WORDS = ["approve", "approved", "clear", "cleared", "positive", "support", "agreement", "progress"]
NEG_WORDS = ["block", "blocked", "reject", "rejected", "lawsuit", "terminate", "withdraw", "delay"]
SEV_L = ["major", "significant", "material", "terminate", "blocked"]
SEV_M = ["concern", "review", "challenge", "risk"]


def detect_category(text):
    if any(w in text for w in REG_WORDS):
        return "REG"
    if any(w in text for w in FIN_WORDS):
        return "FIN"
    return None


def detect_direction(text):
    if any(w in text for w in POS_WORDS):
        return "POS"
    if any(w in text for w in NEG_WORDS):
        return "NEG"
    return None


def detect_severity(text):
    if any(w in text for w in SEV_L):
        return "L"
    if any(w in text for w in SEV_M):
        return "M"
    return "S"


def deal_value(deal, acq_price):
    if deal["structure"] == "CASH":
        return deal["cash"]
    if deal["structure"] == "STOCK":
        return deal["ratio"] * acq_price
    return deal["cash"] + deal["ratio"] * acq_price


def infer_standalone_value(deal):
    k0 = deal_value(deal, deal["a_start"])
    p0 = deal["p0"]
    return (deal["t_start"] - p0 * k0) / (1.0 - p0)


def best_bid_ask(client, ticker):
    book = client.get_book(ticker)
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    return bids[0]["price"], asks[0]["price"]


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    state = {}
    for d, info in DEALS.items():
        state[d] = {"p": info["p0"], "V": infer_standalone_value(info)}

    last_news_id = 0

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        news = client.get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            for n in news:
                text = ((n.get("headline") or "") + " " + (n.get("body") or "")).lower()
                for d, info in DEALS.items():
                    if info["target"].lower() in text or info["acquirer"].lower() in text:
                        cat = detect_category(text)
                        direction = detect_direction(text)
                        severity = detect_severity(text)
                        if cat and direction:
                            delta = BASE_IMPACT[(direction, severity)] * CATEGORY_MULT[cat] * info["deal_mult"]
                            state[d]["p"] = min(1.0, max(0.0, state[d]["p"] + delta))

        sec = {s["ticker"]: s for s in client.get_securities()}

        for d, info in DEALS.items():
            target = info["target"]
            acq = info["acquirer"]
            if target not in sec or acq not in sec:
                continue

            t_bid, t_ask = best_bid_ask(client, target)
            a_bid, a_ask = best_bid_ask(client, acq)
            if t_bid is None or t_ask is None or a_bid is None or a_ask is None:
                continue

            t_mid = (t_bid + t_ask) / 2.0
            a_mid = (a_bid + a_ask) / 2.0
            k = deal_value(info, a_mid)
            p = state[d]["p"]
            v = state[d]["V"]
            intrinsic = p * k + (1.0 - p) * v

            if t_mid < intrinsic - PRICE_THRESHOLD:
                client.place_order(target, "LIMIT", ORDER_QTY, "BUY", price=t_bid)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "SELL")
            elif t_mid > intrinsic + PRICE_THRESHOLD:
                client.place_order(target, "LIMIT", ORDER_QTY, "SELL", price=t_ask)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "BUY")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
