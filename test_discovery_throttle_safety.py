"""A temporary discovery 429 must not cancel durable manual take-profits."""

import pytest

from kalshi import KalshiAPIError
from manual_limit_exits import ManualExitBlocked
from request_coordinator import RequestDeferred
from test_manual_stop_loss import setup
from test_manual_limit_exits import T


@pytest.mark.parametrize("failure", [
    RequestDeferred(4),
    KalshiAPIError(429, "rate limited"),
])
def test_discovery_throttle_keeps_active_tp_and_journal(tmp_path, monkeypatch, failure):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(5, ".42")
    assert service.run_once()
    oid = next(oid for oid, row in exchange.remote.items()
               if row["client_order_id"].startswith("manual5c-"))
    assert exchange.remote[oid]["status"] == "resting"
    before = service.worker.ledger(T)["resting_intents"].copy()
    discover = service.worker.discover

    def unavailable():
        raise failure

    monkeypatch.setattr(service.worker, "discover", unavailable)
    with pytest.raises(type(failure)):
        service.run_once()

    assert exchange.remote[oid]["status"] == "resting"
    assert not exchange.cancelled
    assert len(exchange.sent) == 1
    assert service.worker.ledger(T)["resting_intents"] == before
    assert any(event == "MANUAL_DISCOVERY_DEFERRED" for event, _ in events)
    assert not any(event == "MANUAL_CANCEL_REQUESTED" for event, _ in events)

    monkeypatch.setattr(service.worker, "discover", discover)
    assert service.run_once()
    assert not exchange.cancelled and len(exchange.sent) == 1


def test_unexpected_discovery_error_preserves_existing_fail_closed_cancel(tmp_path, monkeypatch):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(5, ".42")
    assert service.run_once()
    oid = next(oid for oid, row in exchange.remote.items()
               if row["client_order_id"].startswith("manual5c-"))

    def invalid():
        raise ManualExitBlocked("Ownership journal failed validation")

    monkeypatch.setattr(service.worker, "discover", invalid)
    with pytest.raises(ManualExitBlocked):
        service.run_once()
    assert oid in exchange.cancelled
    assert exchange.remote[oid]["status"] == "canceled"
    assert not any(event == "MANUAL_DISCOVERY_DEFERRED" for event, _ in events)
