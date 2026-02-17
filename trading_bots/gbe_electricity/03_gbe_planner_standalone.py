"""GBE Electricity planner (standalone decision support)."""

import os
import re
import time

import requests

API_KEY = os.environ.get("RIT_API_KEY", "YOUR_API_KEY")
BASE_URL = os.environ.get("RIT_BASE_URL", "http://localhost:9999/v1")
POLL_SECS = 1.0


class RITClient:
    def __init__(self, api_key: str, base_url: str = "http://localhost:9999/v1", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-key": api_key})

    def _get(self, path: str, params: dict | None = None):
        return self.session.get(self.base_url + path, params=params, timeout=self.timeout)

    def get_case(self):
        r = self._get("/case")
        r.raise_for_status()
        return r.json()

    def get_news(self, since: int | None = None):
        params = {"since": since} if since is not None else None
        r = self._get("/news", params=params)
        r.raise_for_status()
        return r.json()


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return
        time.sleep(poll_s)


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
    if API_KEY == "YOUR_API_KEY":
        raise RuntimeError("Set RIT_API_KEY before running.")

    client = RITClient(API_KEY, base_url=BASE_URL)
    wait_until_active(client)

    last_news_id = 0
    avg_temp = None
    sun_hours = None

    print(f"Connected to {BASE_URL}. Running GBE planner...")

    while True:
        case = client.get_case()
        if case.get("status") != "ACTIVE":
            print("Case no longer ACTIVE. Exiting.")
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
