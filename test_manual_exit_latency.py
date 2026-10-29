"""Offline regressions for the October 29 manual-exit latency repair."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kalshi import KalshiAPIError, KalshiClient
from manual_exit_runner import ManualExitService
from request_coordinator import RequestDeferred
from resting_manual_exits import RestingExitOnlyClient
from test_manual_limit_exits import T
from test_resting_manual_exits import setup

OLD = "KXBTC15M-OLD-EXPIRED"


def terminal_market(e, status="finalized"):
    return {"ticker": OLD, "status": status,
            "close_time": datetime.fromtimestamp(e.clock() - 86400, timezone.utc).isoformat()}


@pytest.mark.parametrize("status", ["finalized", "settled"])
@pytest.mark.parametrize("intent_status", ["pending_ack", "resting"])
def test_terminal_market_preserves_unknown_orders_without_history_retry(tmp_path, status, intent_status):
    e, service, config, events = setup(tmp_path)
    saved = {"client_id": "manual5c-old", "status": intent_status,
             "order_id": "missing-old-order", "quantity": "10", "remaining": "10"}
    service.worker.ledger(OLD)["resting_intents"] = {"manual5c-old": copy.deepcopy(saved)}
    service.worker.save()
    original = e.market
    calls = []
    def market(ticker):
        calls.append(ticker)
        return terminal_market(e, status) if ticker == OLD else original(ticker)
    e.market = market
    e.buy(price=".42")
    assert service.run_once()
    assert e.sent[0]["price"] == "0.4700"
    assert calls[0] == T  # New exposure goes before old cleanup.
    ledger = service.worker.ledger(OLD)
    assert ledger["venue_terminal_status"] == status and ledger["window_closed"]
    assert ledger["resting_intents"]["manual5c-old"] == saved
    assert not e.cancelled
    assert not any(params.get("ticker") == OLD for _, params in e.reads)
    calls.clear()
    assert service.run_once() and OLD not in calls and len(e.sent) == 1
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.worker.ledger(OLD)["resting_intents"]["manual5c-old"] == saved
    assert restarted.run_once() and OLD not in calls and len(e.sent) == 1
    assert sum(event == "MANUAL_TERMINAL_MARKET_RETIRED" for event, _ in events) == 1


def test_market_404_does_not_mean_terminal_or_introduce_cleanup_delay(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(price=".42")
    original = e.market
    e.market = Mock(side_effect=KalshiAPIError(404, "not found"))
    assert not service.run_once()
    assert not service.worker.ledger(T).get("window_closed")
    assert T not in service._closed_retry_at and not e.sent
    e.market = original
    # Existing API-error backoff still applies; the optimization must not skip it.
    assert service.run_once() and not e.sent
    e.clock_value = service.worker.ledger(T)["retry_after"] + .01
    assert service.run_once() and len(e.sent) == 1


def test_closed_unsettled_cleanup_is_deferred_not_assumed_complete(tmp_path):
    e, service, _, _ = setup(tmp_path)
    service.worker.ledger(OLD)
    original_market, original_close = e.market, service.worker.window_closed
    cleanup = []
    e.market = lambda t: terminal_market(e, "closed") if t == OLD else original_market(t)
    def close(ticker, when):
        if ticker == OLD:
            cleanup.append(ticker)
            return False  # Still no terminal order acknowledgement.
        return original_close(ticker, when)
    service.worker.window_closed = close
    e.buy(price=".42")
    assert not service.run_once() and cleanup == [OLD] and len(e.sent) == 1
    assert not service.worker.ledger(OLD).get("window_closed")
    e.clock_value += 1
    e.buy(count=1, price=".43")
    assert service.run_once() and cleanup == [OLD] and len(e.sent) == 2
    e.clock_value += 30
    assert not service.run_once() and cleanup == [OLD, OLD]


def test_contradictory_future_close_is_not_archived(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(price=".42")
    e.status = "finalized"  # Fake still reports its future close time.
    assert service.run_once()
    assert not service.worker.ledger(T).get("window_closed") and not e.sent


def test_no_extra_market_lookup_for_fully_reserved_fills(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(price=".42")
    assert service.run_once() and len(e.sent) == 1
    e.market = Mock(side_effect=AssertionError("Redundant market lookup"))
    assert not service.worker.maybe_submit(T, {}, {})
    e.market.assert_not_called()


def test_no_market_lookup_without_a_sell_plan(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.market = Mock(side_effect=AssertionError("Redundant market lookup"))
    assert not service.worker.maybe_submit(T, {}, {})
    e.market.assert_not_called()


def response(status=200):
    r = requests.Response()
    r.status_code = status
    r._content = b'{}'
    return r


def test_optional_persistent_transport_is_reused(monkeypatch):
    transport = Mock(return_value=response())
    session = SimpleNamespace(request=transport)
    monkeypatch.setattr(requests, "request", Mock(side_effect=AssertionError("No one-shot requests")))
    client = KalshiClient(session=session)
    client.request("GET", "/first")
    client.request("GET", "/second")
    assert transport.call_count == 2 and client.http_stats["requests"] == 2
    assert client.http_stats["elapsed_ms"] >= 0


@pytest.mark.parametrize("failure", [requests.Timeout("Lost acknowledgement"), requests.ConnectionError("Lost connection")])
def test_pooled_transport_never_replays_a_write(failure):
    transport = Mock(side_effect=failure)
    client = KalshiClient(session=SimpleNamespace(request=transport))
    with pytest.raises(type(failure)):
        client.request("POST", "/portfolio/events/orders", body={})
    assert transport.call_count == 1 and client.http_stats["order_posts"] == 1
    assert client.http_stats["last_order_post_ms"] >= 0


def test_pooled_transport_retains_shared_rate_limit_backoff():
    transport = Mock(return_value=response(429))
    client = KalshiClient(session=SimpleNamespace(request=transport))
    with pytest.raises(KalshiAPIError):
        client.request("GET", "/first")
    with pytest.raises(RequestDeferred):
        client.request("POST", "/portfolio/events/orders", body={})
    assert transport.call_count == 1 and client.http_stats["order_posts"] == 0


def test_pooling_does_not_bypass_exit_write_authorization():
    transport = Mock(return_value=response())
    client = RestingExitOnlyClient(session=SimpleNamespace(request=transport), dry_run=False)
    with pytest.raises(RuntimeError, match="unauthorized write"):
        client.request("POST", "/portfolio/events/orders", body={"side": "bid"})
    transport.assert_not_called()
