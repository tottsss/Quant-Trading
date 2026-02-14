# Ready Bots (Standalone)

These files are standalone versions meant for quick testing on a Windows machine.

## API base URL
- Use `http://localhost:9999/v1` for Client REST API on the same machine.
- `v1` is the API version prefix used by endpoints like `/v1/case`, `/v1/orders`, `/v1/news`.

## Common run steps (API bots)
1. Install dependency:
   - `pip install requests`
2. Set env vars:
   - PowerShell:
     - `$env:RIT_API_KEY="YOUR_KEY"`
     - `$env:RIT_BASE_URL="http://localhost:9999/v1"`
     - `$env:RIT_FIXED_ONLY="1"` (recommended)
     - `$env:RIT_MIN_GROSS_PNL="300"` (tighten/loosen selectivity)
3. Run the script:
   - `python <bot_file>.py`

## Files
- `00_social_outcry_pnl_tracker_standalone.py`
- `01_liquidity_apex_standalone.py` (recommended for Liquidity Risk)
  - Uses anti-fine safeguards: immediate decline of bad fixed tenders, no hedge trading on tickers with unresolved tenders, and endgame flatten.
- `01_liquidity_depth_twap_hybrid_standalone.py`
- `02_volatility_news_arb_standalone.py`
- `03_gbe_planner_standalone.py`
- `04_merger_arb_standalone.py`
- `05_algo_mm_standalone.py`
