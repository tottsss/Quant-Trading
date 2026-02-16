import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


class RuntimeTelemetry:
    DEFAULT_COUNTERS = {
        "loops": 0,
        "tenders_seen": 0,
        "tenders_processed": 0,
        "tenders_accepted": 0,
        "tenders_declined": 0,
        "tenders_limit_blocked": 0,
        "tenders_submit_failed": 0,
        "hedge_blocks": 0,
        "hedge_tickets": 0,
        "hedge_market_fallback_tickets": 0,
        "take_profit_orders": 0,
        "stop_loss_orders": 0,
        "limit_orders_submitted": 0,
        "market_orders_submitted": 0,
        "portfolio_logs": 0,
        "exceptions": 0,
    }

    def __init__(
        self,
        event_cap=6000,
        tender_log_cap=5000,
        hedge_log_cap=5000,
        portfolio_log_cap=2000,
        order_log_cap=6000,
        error_log_cap=2000,
    ):
        self.event_cap = max(100, int(event_cap))
        self.tender_log_cap = max(100, int(tender_log_cap))
        self.hedge_log_cap = max(100, int(hedge_log_cap))
        self.portfolio_log_cap = max(50, int(portfolio_log_cap))
        self.order_log_cap = max(100, int(order_log_cap))
        self.error_log_cap = max(50, int(error_log_cap))
        self.start_iso = utc_now_iso()
        self.start_mono = time.monotonic()
        self.events = []
        self.tender_log = []
        self.hedge_log = []
        self.portfolio_log = []
        self.order_log = []
        self.errors = []
        self.counters = dict(self.DEFAULT_COUNTERS)

    @staticmethod
    def _capped_append(lst, item, cap):
        lst.append(item)
        if len(lst) > cap:
            del lst[: len(lst) - cap]

    def bump_counter(self, key, inc=1):
        self.counters[key] = int(self.counters.get(key, 0)) + int(inc)

    def record_event(self, kind, message=None, **fields):
        payload = {"ts": utc_now_iso(), "kind": str(kind)}
        if message is not None:
            payload["message"] = str(message)
        for k, v in fields.items():
            payload[k] = v
        self._capped_append(self.events, payload, self.event_cap)
        return payload

    def record_error(self, where, exc, **context):
        self.bump_counter("exceptions", 1)
        payload = {
            "ts": utc_now_iso(),
            "where": str(where),
            "error": str(exc),
            "context": context,
        }
        self._capped_append(self.errors, payload, self.error_log_cap)
        self.record_event("error", where=str(where), error=str(exc), context=context)
        return payload

    def record_tender_log(self, action, **fields):
        payload = {"ts": utc_now_iso(), "action": str(action)}
        payload.update(fields)
        self._capped_append(self.tender_log, payload, self.tender_log_cap)
        return payload

    def record_hedge_log(self, event, **fields):
        payload = {"ts": utc_now_iso(), "event": str(event)}
        payload.update(fields)
        self._capped_append(self.hedge_log, payload, self.hedge_log_cap)
        return payload

    def record_portfolio_log(self, **fields):
        payload = {"ts": utc_now_iso()}
        payload.update(fields)
        self._capped_append(self.portfolio_log, payload, self.portfolio_log_cap)
        return payload

    def record_order_log(self, order_type, **fields):
        payload = {"ts": utc_now_iso(), "order_type": str(order_type)}
        payload.update(fields)
        self._capped_append(self.order_log, payload, self.order_log_cap)
        return payload

    def snapshot(self):
        return {
            "counters": dict(self.counters),
            "events": list(self.events),
            "tender_log": list(self.tender_log),
            "hedge_log": list(self.hedge_log),
            "portfolio_log": list(self.portfolio_log),
            "order_log": list(self.order_log),
            "errors": list(self.errors),
        }


def write_run_report(
    script_path,
    report_prefix,
    base_url,
    reason,
    run_error,
    config,
    case_info,
    trader_info,
    limits_info,
    securities,
    position_summary,
    tenders,
    orders_all,
    orders_open,
    orders_transacted,
    orders_cancelled,
    telemetry,
):
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%d_%H%M%S")
    out_path = Path(script_path).resolve().parent / f"{report_prefix}_{stamp}.json"

    report = {
        "saved_at_utc": ts.isoformat(),
        "script": str(Path(script_path).resolve()),
        "base_url": base_url,
        "exit_reason": reason,
        "run_error": run_error,
        "config": config,
        "runtime": {
            "run_started_utc": telemetry.start_iso,
            "run_saved_utc": ts.isoformat(),
            "elapsed_seconds": round(max(0.0, time.monotonic() - telemetry.start_mono), 3),
            "python_version": sys.version,
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
        },
        "case": case_info,
        "trader": trader_info,
        "limits": limits_info,
        "securities": securities,
        "position_summary": position_summary,
        "tenders_active": tenders,
        "orders": {
            "all": orders_all,
            "open": orders_open,
            "transacted": orders_transacted,
            "cancelled": orders_cancelled,
        },
        "telemetry": telemetry.snapshot(),
    }

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    return out_path
