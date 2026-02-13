import os
import time
import statistics
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.getenv("RIT_API_KEY", "YOUR_API_KEY")
POLL_SECS = 0.6
BASE_QTY = 1000
PRICE_THRESHOLD = 0.20
TAS_WINDOW = 30

DEALS = {
    "D1": {"target": "TGX", "acquirer": "PHR", "structure": "CASH", "cash": 50.0, "ratio": 0.0, "p0": 0.70, "t_start": 43.70, "a_start": 47.50},
    "D2": {"target": "BYL", "acquirer": "CLD", "structure": "STOCK", "cash": 0.0, "ratio": 0.75, "p0": 0.55, "t_start": 43.50, "a_start": 79.30},
    "D3": {"target": "GGD", "acquirer": "PNR", "structure": "MIXED", "cash": 33.0, "ratio": 0.20, "p0": 0.50, "t_start": 31.50, "a_start": 59.80},
    "D4": {"target": "FSR", "acquirer": "ATB", "structure": "CASH", "cash": 40.0, "ratio": 0.0, "p0": 0.38, "t_start": 30.50, "a_start": 62.20},
    "D5": {"target": "SPK", "acquirer": "EEC", "structure": "STOCK", "cash": 0.0, "ratio": 1.20, "p0": 0.45, "t_start": 52.80, "a_start": 48.00},
}


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


def recent_vol(client, ticker):
    try:
        trades = client.get_tas(ticker, limit=TAS_WINDOW)
    except Exception:
        return 0.0
    prices = [t.get("price") for t in trades if t.get("price") is not None]
    if len(prices) < 5:
        return 0.0
    return statistics.pstdev(prices)


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    state = {d: {"p": info["p0"], "V": infer_standalone_value(info)} for d, info in DEALS.items()}

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

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

            vol = recent_vol(client, target)
            qty = max(200, int(BASE_QTY / (1.0 + 20.0 * vol)))

            if t_mid < intrinsic - PRICE_THRESHOLD:
                client.place_order(target, "LIMIT", qty, "BUY", price=t_bid)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * qty))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "SELL")
            elif t_mid > intrinsic + PRICE_THRESHOLD:
                client.place_order(target, "LIMIT", qty, "SELL", price=t_ask)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * qty))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "BUY")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
