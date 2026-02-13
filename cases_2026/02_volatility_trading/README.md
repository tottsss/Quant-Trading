Volatility Trading Case

Big picture
- Trade mispriced options on RTM while keeping delta exposure within limits.
- Analyst news provides realized volatility guidance; options are priced with stale volatility.

Strategy (baseline)
- Parse volatility forecast from news.
- Compare theoretical option prices (Black-Scholes) to market mid.
- Trade small size when mispricing exceeds a threshold.
- Hedge delta with RTM and keep within the announced delta limit.

Code
- vol_hedge_bot.py: basic vol-arb + delta-hedge loop.

Run
1) Set API_KEY in the script.
2) Start the RIT Client.
3) python vol_hedge_bot.py
