Algorithmic Market Making Case

Big picture
- Quote both sides in four stocks and manage inventory risk.
- Aggregate position limit is enforced each minute.

Strategy (baseline)
- Quote near the best bid/ask with a small inventory skew.
- Cancel and replace frequently.
- Stop adding risk when near the aggregate limit.

Code
- algo_mm_bot.py: a cleaned up version of the base script with risk checks.

Run
1) Set API_KEY in the script.
2) Start the RIT Client.
3) python algo_mm_bot.py
