Merger Arbitrage Case

Primary guide
- See `MERGER_ARBITRAGE_CASE_GUIDE.md` for full architecture, file map, defaults, and run instructions.

Big picture
- Trade spreads on five live M&A deals; news shifts completion probabilities.
- Use deal structure (cash vs stock vs mixed) to hedge acquirer exposure.

Strategy (baseline)
- Keep a probability estimate per deal and update it from news.
- Compute intrinsic target value: p * deal_value + (1-p) * standalone_value.
- Trade when market price deviates from intrinsic by a threshold.
- Hedge acquirer risk for stock or mixed deals.

Code
- merger_arb_bot.py: news parsing + intrinsic value trading with basic hedging.

Run
1) Set API_KEY in the script.
2) Start the RIT Client.
3) python merger_arb_bot.py
