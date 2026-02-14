"""
Liquidity Risk VWAP & Limit Order Auto-Trader
Methodology:
- Calculates true VWAP of the order book up to the tender quantity.
- Requires a strict profit margin per share to accept/bid.
- Hedges inventory strictly using LIMIT orders at the best bid/ask to capture the spread.
- Actively manages open orders (cancel/replace) to stay at the top of the book.
- Cancels open orders and stops trading on a ticker if an unresolved tender appears (avoids fines).
- Force-flattens via MARKET orders only in the final 30 seconds to avoid $10/share penalties.
"""

import os
import time
import requests

# --- Configuration ---
API_KEY = os.environ.get("RIT_API_KEY", "BNWI101Y") # Defaulting to your known key
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
SESSION = requests.Session()
SESSION.headers.update({"X-API-key": API_KEY})

# Tuning Parameters
PROFIT_MARGIN_PER_SHARE = 0.02  # $0.02 minimum profit per share required
MAX_ORDER_QTY = 10000.0         # Standard RIT liquidity case constraint
ENDGAME_TICKS = 30              # Seconds left to start force-flattening

def get_api(endpoint, params=None):
    resp = SESSION.get(BASE_URL + endpoint, params=params, timeout=3.0)
    return resp.json() if resp.ok else None

def post_api(endpoint, payload=None):
    resp = SESSION.post(BASE_URL + endpoint, json=payload, timeout=3.0)
    return resp.json() if resp.ok else None

def delete_api(endpoint):
    resp = SESSION.delete(BASE_URL + endpoint, timeout=3.0)
    return resp.json() if resp.ok else None

def best_bid_ask(book: dict):
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    return best_bid, best_ask

def calculate_vwap(book: dict, side: str, qty: float):
    """Calculates the exact average price if we were to sweep 'qty' from the book."""
    levels = book.get(side, [])
    if not levels: return None
    
    # Bids are sorted desc (highest first), Asks are sorted asc (lowest first)
    levels.sort(key=lambda x: x["price"], reverse=(side == "bids"))
    
    rem = qty
    used = 0.0
    notional = 0.0
    
    for level in levels:
        if rem <= 0: break
        take = min(rem, level["quantity"])
        rem -= take
        used += take
        notional += take * level["price"]
        
    if rem > 0 or used <= 0:
        return None # Not enough liquidity in the book
    return notional / used

def evaluate_and_bid_tenders(tenders, book_by_ticker, valid_tickers):
    for t in tenders:
        if t["status"] != "OFFERED":
            continue
            
        tid = t["tender_id"]
        ticker = t["ticker"]
        qty = t["quantity"]
        action = t["action"] # Action the CUSTOMER wants to do
        is_fixed = t["is_fixed_bid"]
        fixed_price = t.get("price", 0)
        
        if ticker not in valid_tickers or ticker not in book_by_ticker:
            continue
            
        book = book_by_ticker[ticker]
        
        # Determine our hedging side. If customer BUYS, we SELL to them, so we must BUY to hedge.
        hedge_side_in_book = "asks" if action == "BUY" else "bids"
        hedge_action = "BUY" if action == "BUY" else "SELL"
        
        # Calculate true cost of hedging
        vwap_price = calculate_vwap(book, hedge_side_in_book, min(qty, MAX_ORDER_QTY * 2))
        if vwap_price is None:
            continue # Book too thin
            
        # Calculate fair value based on required margin
        if action == "BUY": # We are selling to them
            my_bid_price = vwap_price + PROFIT_MARGIN_PER_SHARE
            acceptable = (fixed_price >= my_bid_price) if is_fixed else True
        else:               # We are buying from them
            my_bid_price = vwap_price - PROFIT_MARGIN_PER_SHARE
            acceptable = (fixed_price <= my_bid_price) if is_fixed else True

        if is_fixed:
            if acceptable:
                print(f"ACCEPT Fixed Tender {tid} | Ticker: {ticker} | Expected Edge based on VWAP.")
                post_api(f"/tenders/{tid}")
            else:
                delete_api(f"/tenders/{tid}")
        else:
            # It's an auction. Submit our calculated profitable bid.
            print(f"BID Auction Tender {tid} | Ticker: {ticker} | Bid: ${my_bid_price:.2f}")
            post_api(f"/tenders/{tid}", {"price": round(my_bid_price, 2)})

def manage_limit_hedges(positions, open_orders, book_by_ticker, blocked_tickers):
    """
    Maintains LIMIT orders at the top of the book to unwind inventory.
    Cancels orders if they are not at the top of the book or if the ticker is blocked.
    """
    for ticker, pos in positions.items():
        if abs(pos) == 0:
            continue
            
        ticker_orders = [o for o in open_orders if o["ticker"] == ticker]
        
        # If there's an active tender for this ticker, CANCEL ALL orders to avoid front-running fines.
        if ticker in blocked_tickers:
            for o in ticker_orders:
                delete_api(f"/orders/{o['order_id']}")
                print(f"CANCEL Hedge {o['order_id']} on {ticker}: Unresolved tender active.")
            continue
            
        book = book_by_ticker.get(ticker)
        if not book: continue
        
        best_bid, best_ask = best_bid_ask(book)
        if best_bid is None or best_ask is None: continue
        
        # Determine what we need to do
        action = "SELL" if pos > 0 else "BUY"
        qty_to_hedge = min(abs(pos), MAX_ORDER_QTY)
        target_price = best_ask if action == "SELL" else best_bid
        
        has_optimal_order = False
        
        for o in ticker_orders:
            # If our order is for the right action and is exactly at the best price, keep it
            if o["action"] == action and o["price"] == target_price:
                has_optimal_order = True
            else:
                # Cancel if we are off the top of the book or wrong direction
                delete_api(f"/orders/{o['order_id']}")
                
        # If we don't have an order at the best price, place one
        if not has_optimal_order and qty_to_hedge > 0:
            payload = {
                "ticker": ticker,
                "type": "LIMIT",
                "action": action,
                "quantity": qty_to_hedge,
                "price": target_price
            }
            post_api("/orders", payload)
            print(f"PLACED LIMIT HEDGE: {action} {qty_to_hedge} {ticker} @ {target_price}")

def execute_endgame_flattening(positions):
    """Strictly use MARKET orders to wipe all inventory to 0 in the final seconds."""
    for ticker, pos in positions.items():
        if abs(pos) > 0:
            action = "SELL" if pos > 0 else "BUY"
            qty = min(abs(pos), MAX_ORDER_QTY)
            post_api("/orders", {
                "ticker": ticker,
                "type": "MARKET",
                "action": action,
                "quantity": float(qty)
            })
            print(f"ENDGAME MARKET FLATTEN: {action} {qty} {ticker}")

def main():
    print("Starting VWAP & Limit Order Auto-Trader...")
    while True:
        try:
            case = get_api("/case")
            if not case or case.get("status") != "ACTIVE":
                time.sleep(1)
                continue
                
            ticks_left = case.get("ticks_per_period", 300) - case.get("tick", 0)
            
            securities = get_api("/securities")
            valid_tickers = [s["ticker"] for s in securities]
            positions = {s["ticker"]: s["position"] for s in securities}
            
            tenders = get_api("/tenders") or []
            open_orders = get_api("/orders", {"status": "OPEN"}) or []
            
            # Identify tickers with active tenders to avoid front-running
            blocked_tickers = {t["ticker"] for t in tenders if t["status"] not in ("ACCEPTED", "DECLINED", "REJECTED")}

            if ticks_left <= ENDGAME_TICKS:
                # Cancel all open limit orders before dumping via market
                for o in open_orders:
                    delete_api(f"/orders/{o['order_id']}")
                execute_endgame_flattening(positions)
                time.sleep(0.3)
                continue

            # Pre-fetch order books for speed
            book_by_ticker = {}
            for tk in valid_tickers:
                book = get_api("/securities/book", {"ticker": tk, "limit": 40})
                if book: book_by_ticker[tk] = book

            evaluate_and_bid_tenders(tenders, book_by_ticker, valid_tickers)
            manage_limit_hedges(positions, open_orders, book_by_ticker, blocked_tickers)
            
            # Print Fine Watch every few cycles
            if int(ticks_left) % 10 == 0:
                trader_data = get_api("/trader") or {}
                fines = sum(v for k, v in trader_data.items() if "fine" in str(k).lower() and isinstance(v, (int, float)))
                print(f"--- FINE WATCH: ${fines:,.2f} ---")

            time.sleep(0.1) # Fast iteration loop for market making
            
        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
