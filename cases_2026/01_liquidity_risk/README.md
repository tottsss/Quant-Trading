Liquidity Risk Case

Big picture
- You are paid to evaluate tender offers and unwind them efficiently.
- The scoring penalizes speculative trading; trade mainly to hedge accepted tenders.

Strategy (baseline)
- Only accept fixed-price tenders if the price is clearly better than the mid.
- For auctions/winner-take-all, submit prices that still leave a safe edge.
- Immediately hedge accepted tenders with market orders to avoid speculative penalties.

Code
- liquidity_tender_bot.py: monitors /tenders and accepts/declines using a simple edge rule.

Run
1) Set API_KEY in the script.
2) Start the RIT Client.
3) python liquidity_tender_bot.py
