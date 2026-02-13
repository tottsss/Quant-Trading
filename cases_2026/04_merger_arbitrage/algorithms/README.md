Merger Arbitrage — 5 baseline algorithms

These are practical baselines for the RITC Merger Arb case.
Each script uses the case package deal terms and a clear, documented trading rule.

Common setup
- Set API key in each script (API_KEY) or export: RIT_API_KEY=... and leave the default.
- Run from this folder with the RIT Client running or DMA connection active.

Algorithms
1) news_mult_model.py
   - Big picture: the case package probability model (category + severity + deal multipliers).
   - Trades when market price deviates from intrinsic value.

2) implied_p_vs_model.py
   - Big picture: compare market-implied p vs internal p; trade when they diverge.

3) spread_band_strategy.py
   - Big picture: simple spread bands by deal structure; hedge stock/mixed deals.

4) regulatory_focus.py
   - Big picture: only trade on REG/FIN news; more conservative signal quality.

5) risk_parity_multi_deal.py
   - Big picture: allocate order size inversely to recent volatility to balance risk.

Notes
- These are starting points; tune thresholds for your team and practice sessions.
