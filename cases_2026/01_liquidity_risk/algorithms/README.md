Liquidity Risk — 5 baseline algorithms

These are practical baselines you can run on the simulator. Each is intentionally simple, safe, and tunable.
All scripts use the RIT REST API via cases_2026/_shared/rit_api.py.

Common setup
- Set API key in each script (API_KEY) or export: RIT_API_KEY=... and leave the default.
- Run from this folder with the RIT Client running or DMA connection active.

Algorithms
1) fixed_edge_hedge.py
   - Big picture: accept only tenders with a clean edge vs mid, and hedge immediately.
   - Logic: fixed tenders require price edge; auctions use a shaded bid/offer.

2) time_decay_edge.py
   - Big picture: reduce required edge as tender expiry approaches (don’t miss decent fills late).
   - Logic: dynamic edge based on seconds-to-expiry.

3) inventory_aware.py
   - Big picture: enforce risk limits; skip tenders if they push you near gross/net caps.
   - Logic: uses /limits and current positions.

4) liquidity_unwind.py
   - Big picture: accept favorable tenders, then unwind with passive orders before using markets.
   - Logic: limit unwind + fallback market order.

5) volatility_shading.py
   - Big picture: demand larger edge when recent trade volatility is high.
   - Logic: uses recent time-and-sales to set edge.

Notes
- These are not “optimal.” They are safe baselines you can improve.
- Tune EDGE_BPS / thresholds per sub-heat.
