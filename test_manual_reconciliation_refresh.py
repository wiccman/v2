"""Offline integration checks for one fresh read after transient inconsistency."""
from decimal import Decimal as D

import pytest

from manual_limit_exits import ReconciliationPending
from manual_exit_runner import ManualExitService
from test_manual_wti_exits import ETH, WTI, setup
from test_resting_manual_exits import exit_ids


def refresh_events(events):
    return [data for event, data in events if event == "MANUAL_RECONCILIATION_REFRESH"]


def resting_exit(tmp_path, *, price=".42", sign=1):
    exchange, service, settings, events = setup(tmp_path)
    buy = exchange.buy(10, price, sign, ticker=WTI)
    assert service.run_once() and len(exchange.sent) == 1
    events.clear()
    return exchange, service, settings, events, buy, exit_ids(exchange)[0]


def test_one_lagging_exit_fill_read_keeps_valid_resting_remainder(tmp_path, monkeypatch):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    fill = exchange.fill(sell, 3, ".47")
    original_fills = exchange.all_fills
    original_reconcile = service.worker.reconcile
    fill_reads, reconciles = [], []

    def lag_once(ticker):
        rows = original_fills(ticker)
        fill_reads.append(ticker)
        return [r for r in rows if r["fill_id"] != fill] if len(fill_reads) == 1 else rows

    def reconcile(ticker):
        if reconciles:
            assert ticker not in service.worker._plans
            assert ticker not in service.worker._snapshots
            assert service.worker.intents(ticker)  # Durable reservations survive.
        reconciles.append(ticker)
        return original_reconcile(ticker)

    monkeypatch.setattr(exchange, "all_fills", lag_once)
    monkeypatch.setattr(service.worker, "reconcile", reconcile)
    assert service.run_once()
    assert reconciles == [WTI, WTI] and len(fill_reads) == 2
    assert len(exchange.sent) == 1 and not exchange.cancelled
    assert exchange.remote[sell]["remaining_count_fp"] == "7"
    assert len(refresh_events(events)) == 1
    assert refresh_events(events)[0]["stage"] == "verify_exit_fills"


@pytest.mark.parametrize("new_entry", [False, True])
def test_entry_count_lag_refreshes_without_canceling_existing_exit(tmp_path, monkeypatch, new_entry):
    exchange, service, _, events, buy, sell = resting_exit(tmp_path)
    if new_entry:
        buy = exchange.buy(2, ".44", ticker=WTI)
    original = exchange.all_orders
    calls = []

    def lagging_orders(ticker, status=None):
        rows = original(ticker, status)
        calls.append(ticker)
        # The first reconciliation/ownership/observe reads lag the fills feed.
        if len(calls) <= 3:
            for row in rows:
                if row["order_id"] == buy:
                    row["fill_count_fp"] = str(D(row["fill_count_fp"]) - 1)
        return rows

    monkeypatch.setattr(exchange, "all_orders", lagging_orders)
    assert service.run_once()
    assert not exchange.cancelled and exchange.remote[sell]["status"] == "resting"
    assert len(exchange.sent) == (2 if new_entry else 1)
    assert len(refresh_events(events)) == 1
    assert refresh_events(events)[0]["stage"] == "observe_inventory"
    assert exchange.held[WTI] == (12 if new_entry else 10)


def test_partial_adoption_before_refresh_does_not_double_credit_fills(tmp_path, monkeypatch):
    exchange, service, settings, events = setup(tmp_path)
    first = exchange.buy(4, ".40", ticker=WTI)
    second = exchange.buy(6, ".42", ticker=WTI)
    original = exchange.all_orders
    calls = []

    def lag_second_entry(ticker, status=None):
        rows = original(ticker, status)
        calls.append(ticker)
        if len(calls) <= 3:
            for row in rows:
                if row["order_id"] == second:
                    row["fill_count_fp"] = "5"
        return rows

    monkeypatch.setattr(exchange, "all_orders", lag_second_entry)
    assert service.run_once()
    managed = service.worker.ledger(WTI)["managed_fills"]
    assert {m["order_id"] for m in managed.values()} == {first, second}
    assert len(managed) == 2 and len(exchange.sent) == 2 and not exchange.cancelled
    assert sum(D(order["count"]) for order in exchange.sent) == 10
    adopted = [data["fill_id"] for event, data in events if event == "MANUAL_FILL_ADOPTED"]
    assert len(adopted) == len(set(adopted)) == 2
    assert len(refresh_events(events)) == 1
    restarted = ManualExitService(exchange, settings, clock=exchange.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and len(exchange.sent) == 2 and not exchange.cancelled


def test_own_partial_fill_between_order_reads_gets_fresh_replay(tmp_path, monkeypatch):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    original = exchange.all_orders
    calls = []

    def fill_during_observation(ticker, status=None):
        calls.append(ticker)
        if len(calls) == 3:
            exchange.fill(sell, 3, ".47")
        return original(ticker, status)

    monkeypatch.setattr(exchange, "all_orders", fill_during_observation)
    assert service.run_once()
    assert not exchange.cancelled and len(exchange.sent) == 1
    assert exchange.held[WTI] == 7 and exchange.remote[sell]["remaining_count_fp"] == "7"
    assert len(refresh_events(events)) == 1
    assert refresh_events(events)[0]["reason"] == "Resting reservation changed during reconciliation"


def test_persistent_exit_fill_lag_still_cancels_after_one_refresh(tmp_path, monkeypatch):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    hidden = exchange.fill(sell, 3, ".47")
    exchange.hidden_fills.add(hidden)
    original = exchange.all_fills
    reads = []

    def fills(ticker):
        reads.append(ticker)
        return original(ticker)

    monkeypatch.setattr(exchange, "all_fills", fills)
    assert not service.run_once()
    assert reads == [WTI, WTI] and len(refresh_events(events)) == 1
    assert exchange.cancelled == [sell] and len(exchange.sent) == 1
    assert any(event == "MANUAL_EXIT_PAUSED" for event, _ in events)
    assert WTI not in service.worker._plans and WTI not in service.worker._snapshots


def test_persistent_snapshot_staleness_keeps_cancel_fallback_and_never_posts(tmp_path, monkeypatch):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    exchange.buy(2, ".44", ticker=WTI)
    checks = []

    def stale(ticker):
        checks.append(ticker)
        raise ReconciliationPending("Manual/automatic ownership snapshot is stale")

    monkeypatch.setattr(service.worker, "verify_snapshot", stale)
    assert not service.run_once()
    assert checks == [WTI, WTI] and len(refresh_events(events)) == 1
    assert len(exchange.sent) == 1 and exchange.cancelled == [sell]
    assert not any(i["status"] == "pending_ack" for i in service.worker.intents(WTI).values())


def test_refresh_budget_is_shared_across_submissions_in_one_market_pass(tmp_path, monkeypatch):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(4, ".40", ticker=WTI)
    exchange.buy(6, ".42", ticker=WTI)
    original = service.worker.observe
    calls = []

    def changing(*args):
        calls.append(args[0])
        if len(calls) in {1, 3}:
            raise ReconciliationPending("External order/fill counts have not converged")
        return original(*args)

    monkeypatch.setattr(service.worker, "observe", changing)
    assert not service.run_once()
    assert calls == [WTI, WTI, WTI] and len(refresh_events(events)) == 1
    assert len(exchange.sent) == 1 and exchange.cancelled == exit_ids(exchange)


def test_each_market_gets_its_own_single_refresh(tmp_path, monkeypatch):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(1, ".42", ticker=WTI)
    exchange.buy(1, ".42", ticker=ETH)
    original = service.worker.observe
    seen = set()

    def lag_once(*args):
        ticker = args[0]
        if ticker not in seen:
            seen.add(ticker)
            raise ReconciliationPending("External order/fill counts have not converged")
        return original(*args)

    monkeypatch.setattr(service.worker, "observe", lag_once)
    assert service.run_once() and len(exchange.sent) == 2 and not exchange.cancelled
    assert {data["ticker"] for data in refresh_events(events)} == {WTI, ETH}


def test_actual_manual_reduction_on_refresh_cancels_before_sizing_replacement(tmp_path, monkeypatch):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    exchange.buy(2, ".44", ticker=WTI)
    original = service.worker.verify_snapshot
    reduced = []

    def reduce_once(ticker):
        if not reduced:
            manual = exchange.make_order(ticker=WTI, count=5, price=".43", action="sell")
            exchange.fill(manual, 5)
            reduced.append(manual)
        return original(ticker)

    monkeypatch.setattr(service.worker, "verify_snapshot", reduce_once)
    assert service.run_once()
    assert exchange.cancelled == [sell] and reduced[0] not in exchange.cancelled
    assert exchange.held[WTI] == 7
    active = [exchange.remote[oid] for oid in exit_ids(exchange)
              if exchange.remote[oid]["status"] == "resting"]
    assert sum(D(order["remaining_count_fp"]) for order in active) == 7
    assert len(exchange.sent) == 3 and len(refresh_events(events)) == 1


def test_real_app_auto_sell_conflict_still_cancels_only_bot_exit_without_refresh(tmp_path):
    exchange, service, _, events, _, sell = resting_exit(tmp_path)
    app_sell = exchange.make_order(ticker=WTI, count=10, price=".60", action="sell")
    assert not service.run_once()
    assert exchange.cancelled == [sell] and app_sell not in exchange.cancelled
    assert exchange.remote[app_sell]["status"] == "resting" and len(exchange.sent) == 1
    assert not refresh_events(events)


@pytest.mark.parametrize("sign", [1, -1])
def test_sub_cent_identity_mismatch_remains_hard_and_does_not_round_away_difference(tmp_path, sign):
    exchange, service, _, events, _, sell = resting_exit(tmp_path, price=".919", sign=sign)
    assert exchange.sent[0]["price"] == ("0.9690" if sign == 1 else "0.0310")
    exchange.remote[sell]["yes_price_dollars"] = "0.9700" if sign == 1 else "0.0300"
    assert not service.run_once()
    assert exchange.cancelled == [sell] and len(exchange.sent) == 1
    assert not refresh_events(events)
