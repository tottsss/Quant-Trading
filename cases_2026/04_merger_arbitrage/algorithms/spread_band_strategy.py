import os
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[2] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = os.getenv("RIT_API_KEY", "YOUR_API_KEY")
POLL_SECS = 0.5
SPREAD_BUY = 0.50
SPREAD_SELL = 0.20
ORDER_QTY = 1000

DEALS = {
    "D1": {"target": "TGX", "acquirer": "PHR", "structure": "CASH", "cash": 50.0, "ratio": 0.0},
    "D2": {"target": "BYL", "acquirer": "CLD", "structure": "STOCK", "cash": 0.0, "ratio": 0.75},
    "D3": {"target": "GGD", "acquirer": "PNR", "structure": "MIXED", "cash": 33.0, "ratio": 0.20},
    "D4": {"target": "FSR", "acquirer": "ATB", "structure": "CASH", "cash": 40.0, "ratio": 0.0},
    "D5": {"target": "SPK", "acquirer": "EEC", "structure": "STOCK", "cash": 0.0, "ratio": 1.20},
}


def deal_value(deal, acq_price):
    if deal["structure"] == "CASH":
        return deal["cash"]
    if deal["structure"] == "STOCK":
        return deal["ratio"] * acq_price
    return deal["cash"] + deal["ratio"] * acq_price


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

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        sec = {s["ticker"]: s for s in client.get_securities()}

        for _, info in DEALS.items():
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
            deal_val = deal_value(info, a_mid)
            spread = deal_val - t_mid

            if spread >= SPREAD_BUY:
                client.place_order(target, "LIMIT", ORDER_QTY, "BUY", price=t_bid)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "SELL")
            elif spread <= SPREAD_SELL:
                client.place_order(target, "LIMIT", ORDER_QTY, "SELL", price=t_ask)
                if info["structure"] in ("STOCK", "MIXED"):
                    hedge_qty = int(round(info["ratio"] * ORDER_QTY))
                    if hedge_qty > 0:
                        client.place_order(acq, "MARKET", hedge_qty, "BUY")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
