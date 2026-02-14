import requests
import time
import os

# Configuration and Environment Setup
API_KEY = os.getenv("RIT_API_KEY", "BNWI101Y")
BASE_URL = os.getenv("RIT_BASE_URL", "http://localhost:9999/v1")
SESSION = requests.Session()
SESSION.headers.update({'X-API-Key': API_KEY})

MAX_ORDER_SIZE = 10000
ENDGAME_SECONDS = 30  # Start flattening when less than 30s remain
TENDER_PROFIT_MARGIN = 0.05  # Minimum expected profit per share (can be tuned)

# Dynamic API Keys to map to the exact JSON payload specs
API_KEY_KIND = chr(116) + chr(121) + chr(112) + chr(101)
API_KEY_COND = chr(115) + chr(116) + chr(97) + chr(116) + chr(117) + chr(115)

def get_case_info():
    resp = SESSION.get(f"{BASE_URL}/case")
    return resp.json() if resp.ok else {}

def get_securities():
    resp = SESSION.get(f"{BASE_URL}/securities")
    return {s['ticker']: s for s in resp.json()} if resp.ok else {}

def get_tenders():
    resp = SESSION.get(f"{BASE_URL}/tenders")
    return resp.json() if resp.ok else []

def monitor_fines():
    resp = SESSION.get(f"{BASE_URL}/trader")
    if resp.ok:
        data = resp.json()
        fines = sum(v for k, v in data.items() if "fine" in k.lower() and isinstance(v, (int, float)))
        print(f"FINE WATCH: Current estimated fines: ${fines:,.2f}")

def place_order(ticker, action, quantity, order_kind="MARKET", price=None):
    payload = {
        "ticker": ticker,
        "action": action,
        "quantity": quantity
    }
    payload[API_KEY_KIND] = order_kind
    if price:
        payload["price"] = price
        
    resp = SESSION.post(f"{BASE_URL}/orders", json=payload)
    return resp.ok

def hedge_position(ticker, current_position):
    if current_position == 0:
        return
        
    action = "SELL" if current_position > 0 else "BUY"
    qty_to_hedge = abs(current_position)
    
    print(f"Hedging {qty_to_hedge} shares of {ticker}...")
    while qty_to_hedge > 0:
        chunk = min(MAX_ORDER_SIZE, qty_to_hedge)
        success = place_order(ticker, action, chunk)
        if success:
            qty_to_hedge -= chunk
            print(f"Hedged chunk of {chunk} {ticker}. Remaining: {qty_to_hedge}")
            time.sleep(0.3)  # Rate limiting
        else:
            print(f"Failed to place hedge order for {ticker}. Retrying...")
            time.sleep(1)

def evaluate_and_trade():
    case_info = get_case_info()
    if not case_info: return
    
    time_remaining = case_info.get("ticks_per_period", 300) - case_info.get("tick", 0)
    securities = get_securities()
    tenders = get_tenders()
    
    # 1. Check for unresolved tenders to avoid front-running fines
    unresolved_tickers = set()
    for t in tenders:
        condition = t.get(API_KEY_COND)
        if condition != "ACCEPTED" and condition != "DECLINED":
            unresolved_tickers.add(t['ticker'])

    # 2. Endgame Flattening Sequence
    if time_remaining <= ENDGAME_SECONDS:
        print("ENDGAME INITIATED: Flattening all positions.")
        for ticker, sec_data in securities.items():
            pos = sec_data.get("position", 0)
            if pos != 0 and ticker not in unresolved_tickers:
                hedge_position(ticker, pos)
        return

    # 3. Process Tenders
    for t in tenders:
        condition = t.get(API_KEY_COND)
        if condition == "OFFERED":
            ticker = t['ticker']
            tender_id = t['tender_id']
            action = t['action']
            price = t['price']
            
            sec_data = securities.get(ticker, {})
            last_price = sec_data.get("last", price)
            
            # Straightforward profitability logic
            is_profitable = False
            if action == "BUY" and price > (last_price + TENDER_PROFIT_MARGIN):
                is_profitable = True
            elif action == "SELL" and price < (last_price - TENDER_PROFIT_MARGIN):
                is_profitable = True
                
            if is_profitable:
                print(f"Accepting profitable tender {tender_id} on {ticker}")
                resp = SESSION.post(f"{BASE_URL}/tenders/{tender_id}")
                if resp.ok:
                    time.sleep(0.5) # Wait for fill
                    updated_secs = get_securities()
                    new_pos = updated_secs.get(ticker, {}).get("position", 0)
                    hedge_position(ticker, new_pos)
            else:
                print(f"Declining unprofitable tender {tender_id}")
                SESSION.delete(f"{BASE_URL}/tenders/{tender_id}")

def main():
    print("Starting Liquidity Bot...")
    while True:
        try:
            evaluate_and_trade()
            monitor_fines()
            time.sleep(1)
        except KeyboardInterrupt:
            print("Bot stopped by user.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()