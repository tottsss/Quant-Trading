GBE Energy Electricity Trading Case

Big picture
- Role-based team case with Producers, Distributors, and Traders.
- Orders are manual (API order submission is disabled), but data is available.

Strategy (baseline)
- Use news to forecast sunshine hours and temperature.
- Convert those forecasts into expected production and demand.
- Coordinate team roles: Producers set output, Distributors set purchase volume, Traders fill factory tenders.

Code
- gbe_planner.py: parses news and prints recommended quantities for the next day.

Run
1) Set API_KEY in the script.
2) Start the RIT Client.
3) python gbe_planner.py
