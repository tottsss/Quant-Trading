"""Social Outcry standalone PnL tracker.

Usage:
python 00_social_outcry_pnl_tracker_standalone.py --trades trades.csv --close 101.25 --mult 10
"""

import argparse
import csv


def compute_pnl(trades, close_price, multiplier=10.0):
    pnl = 0.0
    for action, qty, price in trades:
        sign = 1 if action.upper() == "BUY" else -1
        pnl += sign * (close_price - price) * qty * multiplier
    return pnl


def load_trades(path):
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            action = row["action"].strip().upper()
            qty = float(row["qty"])
            price = float(row["price"])
            trades.append((action, qty, price))
    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", required=True, help="CSV columns: action,qty,price")
    parser.add_argument("--close", required=True, type=float, help="Closing spot price")
    parser.add_argument("--mult", type=float, default=10.0, help="Contract multiplier")
    args = parser.parse_args()

    trades = load_trades(args.trades)
    pnl = compute_pnl(trades, args.close, args.mult)
    print(f"P&L: {pnl:.2f}")


if __name__ == "__main__":
    main()
