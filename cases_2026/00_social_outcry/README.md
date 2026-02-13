Social Outcry Case

Big picture
- Open-outcry futures trading used to warm up the competition.
- No API trading; it is manual and not part of final scoring.

Strategy (baseline)
- Focus on speed and discipline rather than model complexity.
- Trade small, avoid chasing, and keep a tight sense of your average price.
- Use the news flow as a bias, not as certainty.

Code
- pnl_tracker.py: simple P&L calculator for your trades.
- trades_example.csv: example format for the input file.

Run
python pnl_tracker.py --trades trades_example.csv --close 1000
