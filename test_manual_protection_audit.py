"""Offline coverage audit: open positions need a verified TP and live SL monitor."""
from decimal import Decimal as D

import pytest

from test_manual_limit_exits import T
from test_manual_stop_loss import setup


def checks(events):
    return [details for event, details in events
            if event == "MANUAL_PROTECTION_CHECK"]


@pytest.mark.parametrize("sign", [1, -1])
def test_new_manual_fill_reports_verified_tp_and_software_stop(tmp_path, sign):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42", sign)
    assert service.run_once()
    reports = checks(events)
    assert reports
    report = reports[-1]
    assert report["tp_status"] == "active"
    assert report["tp_resting_quantity"] == "10"
    assert report["tp_target"] == "0.47"
    assert report["sl_status"] == "monitoring"
    assert report["sl_trigger"] == "0.31"
    assert report["sl_order_type"] == "local_bid_monitor"
    assert report["side"] == ("YES" if sign == 1 else "NO")
    assert len(e.sent) == 1
    assert service.run_once() and len(e.sent) == 1


def test_protection_rechecks_each_cycle_and_heartbeat(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(2, ".42")
    assert service.run_once()
    n = len(checks(events))
    assert service.run_once() and len(checks(events)) == n
    e.clock_value += 31
    assert service.run_once() and len(checks(events)) == n + 1
    assert checks(events)[-1]["sl_status"] == "monitoring"
    assert len(e.sent) == 1


def test_manual_tp_cancel_is_visible_but_stop_stays_active(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42")
    assert service.run_once()
    oid = next(oid for oid, row in e.remote.items()
               if row["client_order_id"].startswith("manual5c-"))
    e.request("DELETE", "/portfolio/events/orders/" + oid)
    assert service.run_once()
    assert len(e.sent) == 1  # Explicit manual TP cancel is respected.
    report = checks(events)[-1]
    assert report["tp_status"] == "suppressed"
    assert report["sl_status"] == "monitoring"
    assert any(event == "MANUAL_PROTECTION_ALERT" for event, _ in events)
    e.bid = D(".30")
    assert service.run_once()
    assert e.held[T] == 0
    assert e.sent[-1]["time_in_force"] == "immediate_or_cancel"


def test_failed_resting_tp_is_retried_without_duplicate(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42")
    from kalshi import KalshiAPIError
    e.fail_post = KalshiAPIError(422, "definite rejection")
    assert not service.run_once()
    assert not e.sent
    e.fail_post = None
    e.clock_value += 20
    assert service.run_once()
    assert len(e.sent) == 1
    assert checks(events)[-1]["tp_status"] == "active"
    assert checks(events)[-1]["sl_status"] == "monitoring"


def test_partial_tp_fill_only_audits_open_quantity(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42")
    assert service.run_once()
    oid = next(oid for oid, row in e.remote.items()
               if row["client_order_id"].startswith("manual5c-"))
    e.fill(oid, 3, ".47")
    assert service.run_once()
    report = checks(events)[-1]
    assert report["quantity"] == "7"
    assert report["tp_resting_quantity"] == "7"
    assert report["tp_status"] == "active"
    assert report["sl_status"] == "monitoring"
    assert len(e.sent) == 1


def test_multiple_open_fills_are_all_checked_even_when_one_stop_triggers(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42")
    e.buy(5, ".50")
    assert service.run_once()
    entries = {r["entry"]: r for r in checks(events) if r["tp_status"] == "active"}
    assert set(entries) == {"0.42", "0.50"}
    e.bid = D(".30")
    assert service.run_once()
    stops = service.worker.ledger(T)["stop_armed"]
    assert len(stops) == 2
    assert any(r["sl_status"] == "triggered" for r in checks(events))


def test_invalid_tp_grid_is_reconciled_without_placing_duplicate(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42")
    assert service.run_once()
    original = next(oid for oid, row in e.remote.items()
                    if row["client_order_id"].startswith("manual5c-"))
    e.price_ranges = [{"start": "0", "end": "1", "step": ".02"}]
    assert service.run_once()
    assert original in e.cancelled
    assert e.sent[-1]["price"] == "0.4800"
    assert checks(events)[-1]["tp_status"] == "active"


def test_too_cheap_entry_is_reported_not_pretended_protected(tmp_path):
    e, service, _, events = setup(tmp_path)
    e.buy(1, ".07")
    assert service.run_once()
    report = checks(events)[-1]
    assert report["sl_status"] == "unavailable"
    assert report["sl_trigger"] is None
    assert any(event == "MANUAL_PROTECTION_ALERT" for event, _ in events)
