import re
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parents[1] / "_shared"))
from rit_api import RITClient, wait_until_active

API_KEY = "YOUR_API_KEY"
POLL_SECS = 1.0


def demand_from_temp(at_c):
    return 200.0 - 15.0 * at_c + 0.8 * at_c ** 2 - 0.01 * at_c ** 3


def solar_from_sun(hours):
    return 6.0 * hours


def parse_forecasts(news_items):
    avg_temp = None
    sun_hours = None
    for n in news_items:
        text = (n.get("headline") or "") + " " + (n.get("body") or "")
        m = re.search(r"average temperature[^\d-]*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if m:
            avg_temp = float(m.group(1))
        m = re.search(r"temperature[^\d-]*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if m and avg_temp is None:
            avg_temp = float(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)\s*hours? of sunshine", text, re.IGNORECASE)
        if m:
            sun_hours = float(m.group(1))
    return avg_temp, sun_hours


def main():
    client = RITClient(API_KEY)
    wait_until_active(client)

    last_news_id = 0
    avg_temp = None
    sun_hours = None

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            break

        news = client.get_news(since=last_news_id)
        if news:
            last_news_id = max(n["news_id"] for n in news)
            t, s = parse_forecasts(news)
            avg_temp = t if t is not None else avg_temp
            sun_hours = s if s is not None else sun_hours

        if avg_temp is None or sun_hours is None:
            time.sleep(POLL_SECS)
            continue

        demand = max(0.0, demand_from_temp(avg_temp))
        solar = max(0.0, solar_from_sun(sun_hours))
        net_needed = max(0.0, demand - solar)

        print("\nForecasts")
        print(f"  avg_temp_c: {avg_temp:.2f}")
        print(f"  sun_hours: {sun_hours:.2f}")
        print("Recommendations (contracts)")
        print(f"  Distributor buy (ELEC-F): {demand:.1f}")
        print(f"  Producer solar output: {solar:.1f}")
        print(f"  Producer net needed via gas: {net_needed:.1f}")
        print("  Note: 8 NG contracts -> 1 ELEC-dayX contract")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
