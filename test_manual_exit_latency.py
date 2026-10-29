"""Offline regression checks for active fills ahead of stale-market cleanup."""
import copy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kalshi import KalshiAPIError
from manual_exit_runner import ManualExitService, Settings
from test_resting_manual_exits import RestingExchange, exit_ids


class TracedExchange(RestingExchange):
    def __init__(self):
        super().__init__()
        self.trace = []
        self.closes = {}
        self.fail_market = set()

    def market(self, ticker):
        self.trace.append(("market", ticker))
        if ticker in self.fail_market:
            raise KalshiAPIError(503, "offline fixture")
        result = super().market(ticker)
        if ticker in self.closes:
            result["close_time"] = datetime.fromtimestamp(self.closes[ticker], timezone.utc).isoformat()
        return result

    def request(self, method, path, params=None, body=None, auth=False):
        if method == "POST":
            self.trace.append(("submit", body["ticker"]))
        elif method == "GET" and path == "/portfolio/orders":
            self.trace.append(("orders", (params or {}).get("ticker")))
        return super().request(method, path, params=params, body=body, auth=auth)


def setup(tmp_path):
    exchange = TracedExchange()
    path = tmp_path / "state.json"
    path.write_text('{"markets": {}}')
    path.with_name("state_take_profit.json").write_text('{"markets": {}}')
    settings = replace(Settings.read({"STATE_PATH": str(path),
        "MANUAL_EXIT_SERIES": "KXBTC15M,KXETH15M,KXWTI15M"}), dry_run=False)
    events = []
    service = ManualExitService(exchange, settings, clock=exchange.clock,
        emit=lambda event, **data: events.append((event, data)))
    return exchange, service, settings, events


def expired_backlog(exchange, service, count=8):
    tickers = [f"KXBTC15M-OLD-{i:02d}" for i in range(count)]
    for ticker in tickers:
        exchange.buy(1, ".42", ticker=ticker)
    assert service.run_once() and len(exchange.sent) == count
    for ticker in tickers:
        exchange.closes[ticker] = exchange.close
    exchange.clock_value = exchange.close + 1
    exchange.close += 2000
    return tickers


@pytest.mark.parametrize("ticker", ["KXBTC15M-NEW", "KXETH15M-NEW", "KXWTI15M-NEW"])
def test_new_fill_is_posted_before_expired_failures_even_after_restart(tmp_path, ticker):
    exchange, service, settings, _ = setup(tmp_path)
    old = expired_backlog(exchange, service)
    for oid in exit_ids(exchange):
        exchange.remote[oid]["yes_price_dollars"] = ".48"  # Cannot prove original terms.
    # No in-memory cache survives. The old intents still have their expiries.
    service = ManualExitService(exchange, settings, clock=exchange.clock, emit=lambda *a, **k: None)
    exchange.buy("2.75", ".42", ticker=ticker)
    exchange.trace.clear()
    assert not service.run_once()  # Historical failures remain visible.
    submitted = exchange.trace.index(("submit", ticker))
    historical_reads = [i for i, (kind, t) in enumerate(exchange.trace) if t in old]
    assert historical_reads and min(historical_reads) > submitted
    assert sum(kind == "market" and t in old for kind, t in exchange.trace) == 1
    assert exchange.sent[-1]["ticker"] == ticker and exchange.sent[-1]["count"] == "2.75"
    assert exchange.sent[-1]["price"] == "0.4700"
    assert all(not service.worker.ledger(t).get("window_closed") for t in old)
    before = len(exchange.sent)
    exchange.clock_value += 1
    exchange.trace.clear()
    assert not service.run_once()
    assert len(exchange.sent) == before  # No duplicate exit on the new market.
    assert sum(kind == "market" and t in old for kind, t in exchange.trace) == 1
    assert len(service._closed_attempted) == 2  # A failing first record does not starve the next.


def test_failed_cleanup_waits_then_reconciles_without_forgetting_reservations(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    ticker = expired_backlog(exchange, service, count=1)[0]
    oid = exit_ids(exchange)[0]
    exchange.remote[oid]["yes_price_dollars"] = ".48"
    exchange.trace.clear()
    assert not service.run_once()
    intent = copy.deepcopy(next(iter(service.worker.intents(ticker).values())))
    assert not service.worker.ledger(ticker).get("window_closed")
    exchange.trace.clear()
    exchange.clock_value += service.CLOSED_RETRY_SECONDS - 1
    assert not service.run_once()
    assert not exchange.trace  # Even the old market metadata is not polled.
    assert next(iter(service.worker.intents(ticker).values())) == intent
    exchange.remote[oid]["yes_price_dollars"] = ".47"
    exchange.clock_value += 1
    assert service.run_once()  # The prior best-effort cancellation is now verifiable.
    assert exchange.cancelled == [oid]
    assert service.worker.ledger(ticker)["window_closed"]
    assert len(exchange.sent) == 1
    assert all(kind != "market" for kind, _ in exchange.trace)


def test_historical_market_lookup_failure_is_throttled_and_does_not_starve_cleanup(tmp_path):
    exchange, service, settings, _ = setup(tmp_path)
    old = expired_backlog(exchange, service, count=2)
    service = ManualExitService(exchange, settings, clock=exchange.clock, emit=lambda *a, **k: None)
    exchange.fail_market.add(old[0])
    exchange.trace.clear()
    assert not service.run_once()
    assert ("market", old[0]) in exchange.trace
    exchange.trace.clear()
    assert not service.run_once()
    assert ("market", old[0]) not in exchange.trace
    assert ("market", old[1]) in exchange.trace
    assert not service.worker.ledger(old[0]).get("window_closed")


def test_successful_expiry_cleanup_gets_prompt_confirmation_without_market_refetch(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    ticker = expired_backlog(exchange, service, count=1)[0]
    exchange.trace.clear()
    assert not service.run_once()
    assert len(exchange.cancelled) == 1
    assert service.run_once() and service.worker.ledger(ticker)["window_closed"]
    assert exchange.trace.count(("market", ticker)) == 1
    exchange.trace.clear()
    assert service.run_once() and not exchange.trace


def test_missing_order_is_still_unresolved_after_retry_throttling(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    ticker = expired_backlog(exchange, service, count=1)[0]
    oid = exit_ids(exchange)[0]
    exchange.hidden_orders.add(oid)
    assert not service.run_once()
    assert not service.run_once()
    exchange.trace.clear()
    assert not service.run_once() and not exchange.trace
    intent = next(iter(service.worker.intents(ticker).values()))
    assert intent["order_id"] == oid and intent["quantity"] == "1"
    assert not service.worker.ledger(ticker).get("window_closed")
    assert len(exchange.sent) == 1


def test_cancel_retry_timer_alone_cannot_starve_other_expired_markets(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    old = expired_backlog(exchange, service, count=2)
    exchange.hidden_orders.update(exit_ids(exchange))
    assert not service.run_once()
    # A slow polling interval allows the cancellation timer to advance on
    # every pass; that is still not new evidence about the unresolved order.
    exchange.clock_value += 10
    assert not service.run_once()
    exchange.trace.clear()
    assert not service.run_once()
    assert ("market", old[1]) in exchange.trace
    assert ("orders", old[0]) not in exchange.trace
    assert all(not service.worker.ledger(t).get("window_closed") for t in old)


def test_first_observation_of_many_closed_markets_also_limits_reconciliation(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    old = [f"KXBTC15M-EMPTY-{i}" for i in range(5)]
    for ticker in old:
        service.worker.ledger(ticker)["managed_fills"] = {}
        exchange.closes[ticker] = exchange.clock_value - 1
    exchange.buy(1, ".42", ticker="KXWTI15M-NEW")
    assert not service.run_once()
    assert exchange.sent[-1]["ticker"] == "KXWTI15M-NEW"
    assert sum(bool(service.worker.ledger(t).get("window_closed")) for t in old) == 1
    assert not service.run_once()
    assert sum(bool(service.worker.ledger(t).get("window_closed")) for t in old) == 2


@pytest.mark.parametrize("expiry", ["NaN", "invalid", None])
def test_invalid_saved_expiry_hint_does_not_suspend_an_unrelated_new_fill(tmp_path, expiry):
    exchange, service, settings, _ = setup(tmp_path)
    old = expired_backlog(exchange, service, count=1)[0]
    next(iter(service.worker.intents(old).values()))["expires_at"] = expiry
    service.worker.save()
    service = ManualExitService(exchange, settings, clock=exchange.clock, emit=lambda *a, **k: None)
    exchange.buy(1, ".42", ticker="KXWTI15M-NEW")
    exchange.trace.clear()
    assert service.close_hint(old) is None
    assert not service.run_once()  # Old cancellation still needs confirmation.
    assert exchange.sent[-1]["ticker"] == "KXWTI15M-NEW"
    assert exchange.trace.index(("submit", "KXWTI15M-NEW")) < exchange.trace.index(("market", old))
