cases_2026

This folder contains per-case notes and starter code for the RITC 2026 cases.
Each case has its own subfolder with:
- README.md: big-picture explanation and baseline strategy
- One Python script: runnable starter code

Notes
- API-based cases use the RIT Client REST API (localhost:9999) with X-API-key.
- Some cases (Social Outcry, GBE Electricity) are manual or API order submission is disabled.
  The code there is decision-support or P&L tooling, not an auto-trader.

Quick start (API cases)
1) Put your API key into the script (API_KEY variable).
2) Start the RIT Client.
3) Run the script from its case folder.
