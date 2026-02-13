import os
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.getenv("RIT_API_KEY", "YOUR_API_KEY")
POLL_SECS = 0.5
P_GAP = 0.08
ORDER_QTY = 1000

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


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    # internal p stays at p0 (simple baseline); you can add news updates here
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
            v = state[d]["V"]

            # implied p from market price
            if k != v:
                p_impl = (t_mid - v) / (k - v)
            else:
                p_impl = state[d]["p"]

            p_impl = max(0.0, min(1.0, p_impl))
            p_model = state[d]["p"]

            if p_impl < p_model - P_GAP:
                client.place_order(target, "LIMIT", ORDER_QTY, "BUY", price=t_bid)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "SELL")
            elif p_impl > p_model + P_GAP:
                client.place_order(target, "LIMIT", ORDER_QTY, "SELL", price=t_ask)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "BUY")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
