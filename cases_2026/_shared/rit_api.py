'''Small helper for the RIT Client REST API.'''

from __future__ import annotations

import time
import requests


class RITClient:
    def __init__(self, api_key: str, base_url: str = "http://localhost:9999/v1", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-key": api_key})

    def _get(self, path: str, params: dict | None = None):
        return self.session.get(self.base_url + path, params=params, timeout=self.timeout)

    def _post(self, path: str, params: dict | None = None):
        return self.session.post(self.base_url + path, params=params, timeout=self.timeout)

    def _delete(self, path: str, params: dict | None = None):
        return self.session.delete(self.base_url + path, params=params, timeout=self.timeout)

    def get_case(self):
        r = self._get("/case")
        r.raise_for_status()
        return r.json()

    def get_limits(self):
        r = self._get("/limits")
        r.raise_for_status()
        return r.json()

    def get_news(self, since: int | None = None, limit: int | None = None):
        params = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        r = self._get("/news", params=params)
        r.raise_for_status()
        return r.json()

    def get_securities(self, ticker: str | None = None):
        params = {"ticker": ticker} if ticker else None
        r = self._get("/securities", params=params)
        r.raise_for_status()
        return r.json()

    def get_book(self, ticker: str, limit: int | None = None):
        params = {"ticker": ticker}
        if limit is not None:
            params["limit"] = limit
        r = self._get("/securities/book", params=params)
        r.raise_for_status()
        return r.json()

    def get_tas(self, ticker: str, after: int | None = None, period: int | None = None, limit: int | None = None):
        params = {"ticker": ticker}
        if after is not None:
            params["after"] = after
        if period is not None:
            params["period"] = period
        if limit is not None:
            params["limit"] = limit
        r = self._get("/securities/tas", params=params)
        r.raise_for_status()
        return r.json()

    def get_orders(self, status: str | None = None):
        params = {"status": status} if status else None
        r = self._get("/orders", params=params)
        r.raise_for_status()
        return r.json()

    def get_order(self, order_id: int):
        r = self._get(f"/orders/{order_id}")
        r.raise_for_status()
        return r.json()

    def place_order(self, ticker: str, order_type: str, quantity: float, action: str, price: float | None = None, dry_run: int | None = None):
        params = {
            "ticker": ticker,
            "type": order_type,
            "quantity": quantity,
            "action": action,
        }
        if price is not None:
            params["price"] = price
        if dry_run is not None:
            params["dry_run"] = dry_run
        r = self._post("/orders", params=params)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: int):
        r = self._delete(f"/orders/{order_id}")
        r.raise_for_status()
        return r.json()

    def cancel_all(self, ticker: str | None = None, all_flag: int | None = None, ids: str | None = None, query: str | None = None):
        params = {}
        if ticker is not None:
            params["ticker"] = ticker
        if all_flag is not None:
            params["all"] = all_flag
        if ids is not None:
            params["ids"] = ids
        if query is not None:
            params["query"] = query
        r = self._post("/commands/cancel", params=params)
        r.raise_for_status()
        return r.json()

    def get_tenders(self):
        r = self._get("/tenders")
        r.raise_for_status()
        return r.json()

    def accept_tender(self, tender_id: int, price: float | None = None):
        params = {}
        if price is not None:
            params["price"] = price
        r = self._post(f"/tenders/{tender_id}", params=params)
        r.raise_for_status()
        return r.json()

    def decline_tender(self, tender_id: int):
        r = self._delete(f"/tenders/{tender_id}")
        r.raise_for_status()
        return r.json()


def wait_until_active(client: RITClient, poll_s: float = 0.5):
    while True:
        case = client.get_case()
        if case.get("status") == "ACTIVE":
            return case
        time.sleep(poll_s)
