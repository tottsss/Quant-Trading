import os
import time
import requests

# --- Configuration ---
API_KEY = os.environ.get("RIT_API_KEY", "BNWI101Y")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
SESSION = requests.Session()
SESSION.headers.update({"X-API-key": API_KEY})

# Tuning Parameters
PROFIT_MARGIN_PER_SHARE = 0.02  
MAX_ORDER_QTY = 10000.0         
ENDGAME_TICKS = 30              

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
    best_bid = bids[0].get("price") if bids else None
    best_ask = asks[0].get("price") if asks else None
    return best_bid, best_ask

def calculate_vwap(book: dict, side: str, qty: float):
    levels = book.get(side, [])
    if not levels: return None
    
    levels.sort(key=lambda x: x.get("price", 0), reverse=(side == "bids"))
    
    rem = qty
    used = 0.0
    notional = 0.0
    
    for level in levels:
        if rem <= 0: break
        level_qty = level.get("quantity", 0)
        level_px = level.get("price", 0)
        take = min(rem, level_qty)
        rem -= take
        used += take
        notional += take * level_px
        
    if rem > 0 or used <= 0:
        return None 
    return notional / used

def evaluate_and_bid_tenders(tenders, book_by_ticker, valid_tickers):
    for t in tenders:
        if t.get("status") != "OFFERED":
            continue
            
        tid = t.get("tender_id")
        ticker = t.get("ticker")
        qty = t.get("quantity", 0)
        action = t.get("action") 
        is_fixed = t.get("is_fixed_bid", False)
        fixed_price = t.get("price", 0)
        
        if not ticker or ticker not in valid_tickers or ticker not in book_by_ticker:
            continue
            
        book = book_by_ticker[ticker]
        
        hedge_side_in_book = "asks" if action == "BUY" else "bids"
        hedge_action = "BUY" if action == "BUY" else "SELL"
        
        vwap_price = calculate_vwap(book, hedge_side_in_book, min(qty, MAX_ORDER_QTY * 2))
        if vwap_price is None:
            continue 
            
        if action == "BUY": 
            my_bid_price = vwap_price + PROFIT_MARGIN_PER_SHARE
            acceptable = (fixed_price >= my_bid_price) if is_fixed else True
        else:               
            my_bid_price = vwap_price - PROFIT_MARGIN_PER_SHARE
            acceptable = (fixed_price <= my_bid_price) if is_fixed else True

        if is_fixed:
            if acceptable:
                print(f"ACCEPT Fixed Tender {tid} | Ticker: {ticker} | Expected Edge based on VWAP.")
                post_api(f"/tenders/{tid}")
            else:
                delete_api(f"/tenders/{tid}")
        else:
            print(f"BID Auction Tender {tid} | Ticker: {ticker} | Bid: ${my_bid_price:.2f}")
            post_api(f"/tenders/{tid}", {"price": round(my_bid_price, 2)})

def manage_limit_hedges(positions, open_orders, book_by_ticker, blocked_tickers):
    for ticker, pos in positions.items():
        if abs(pos) == 0:
            continue
            
        ticker_orders = [o for o in open_orders if o.get("ticker") == ticker]
        
        if ticker in blocked_tickers:
            for o in ticker_orders:
                order_id = o.get("order_id")
                if order_id:
                    delete_api(f"/orders/{order_id}")
                    print(f"CANCEL Hedge {order_id} on {ticker}: Unresolved tender active.")
            continue
            
        book = book_by_ticker.get(ticker)
        if not book: continue
        
        best_bid, best_ask = best_bid_ask(book)
        if best_bid is None or best_ask is None: continue
        
        action = "SELL" if pos > 0 else "BUY"
        qty_to_hedge = min(abs(pos), MAX_ORDER_QTY)
        target_price = best_ask if action == "SELL" else best_bid
        
        has_optimal_order = False
        
        for o in ticker_orders:
            if o.get("action") == action and o.get("price") == target_price:
                has_optimal_order = True
            else:
                order_id = o.get("order_id")
                if order_id:
                    delete_api(f"/orders/{order_id}")
                
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
            
            securities = get_api("/securities") or []
            valid_tickers = [s.get("ticker") for s in securities if s.get("ticker")]
            positions = {s.get("ticker"): s.get("position", 0) for s in securities if s.get("ticker")}
            
            tenders = get_api("/tenders") or []
            open_orders = get_api("/orders", {"status": "OPEN"}) or []
            
            # Use .get() defensively here
            blocked_tickers = {
                t.get("ticker") 
                for t in tenders 
                if t.get("status") not in ("ACCEPTED", "DECLINED", "REJECTED") and t.get("ticker")
            }

            if ticks_left <= ENDGAME_TICKS:
                for o in open_orders:
                    order_id = o.get("order_id")
                    if order_id:
                        delete_api(f"/orders/{order_id}")
                execute_endgame_flattening(positions)
                time.sleep(0.3)
                continue

            book_by_ticker = {}
            for tk in valid_tickers:
                book = get_api("/securities/book", {"ticker": tk, "limit": 40})
                if book: book_by_ticker[tk] = book

            evaluate_and_bid_tenders(tenders, book_by_ticker, valid_tickers)
            manage_limit_hedges(positions, open_orders, book_by_ticker, blocked_tickers)
            
            if int(ticks_left) % 10 == 0:
                trader_data = get_api("/trader") or {}
                fines = sum(v for k, v in trader_data.items() if "fine" in str(k).lower() and isinstance(v, (int, float)))
                print(f"--- FINE WATCH: ${fines:,.2f} ---")

            time.sleep(0.1) 
            
        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
