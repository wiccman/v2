"""Stacked fills get their entire uncovered quantity submitted despite read lag."""
from decimal import Decimal as D

import pytest

from kalshi import KalshiAPIError
from manual_exit_runner import ManualExitService
from test_manual_limit_exits import T
from test_resting_manual_exits import setup, exit_ids


def resting_quantity(exchange):
    return sum((D(exchange.remote[oid]["remaining_count_fp"])
                for oid in exit_ids(exchange)), D(0))


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("sizes", [(5, 5), (5, 5, 5), (5, 5, 5, 5), ("2.50", "7.25", "5.25")])
def test_all_stacked_fills_are_covered_before_new_orders_are_visible(tmp_path, sign, sizes):
    e, service, settings, events = setup(tmp_path)
    for size in sizes:
        e.buy(size, ".42", sign)
    e.hide_post = True
    assert not service.run_once()  # Awaiting readback, not missing submission.
    total = sum((D(str(size)) for size in sizes), D(0))
    assert len(e.sent) == len(sizes)
    assert resting_quantity(e) == total == abs(e.held[T])
    assert sum(D(i["quantity"]) for i in service.worker.intents(T).values()) == total
    assert not e.cancelled
    assert any(event == "MANUAL_TP_BATCH_SUBMITTED" and D(data["quantity"]) == total
               for event, data in events)
    # Durable pending reservations survive restart and cannot be posted twice.
    restarted = ManualExitService(e, settings, clock=e.clock, emit=lambda *a, **k: None)
    assert not restarted.run_once() and len(e.sent) == len(sizes)
    e.hidden_orders.clear()
    e.hide_post = False
    assert restarted.run_once() and len(e.sent) == len(sizes)
    assert resting_quantity(e) == total and not e.cancelled


def test_existing_five_plus_two_more_fives_covers_fifteen(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(5, ".42")
    assert service.run_once()
    original = exit_ids(e)[0]
    e.buy(5, ".43")
    e.buy(5, ".44")
    e.hide_post = True
    assert not service.run_once()
    assert resting_quantity(e) == 15 and len(e.sent) == 3
    assert e.remote[original]["remaining_count_fp"] == "5"
    assert [o["price"] for o in e.sent] == ["0.4700", "0.4800", "0.4900"]
    assert not e.cancelled


def test_partial_sale_then_two_fives_only_covers_remaining_position(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(5, ".42")
    assert service.run_once()
    e.fill(exit_ids(e)[0], 2, ".47")
    e.buy(5, ".43")
    e.buy(5, ".44")
    e.hide_post = True
    assert not service.run_once()
    assert resting_quantity(e) == 13 == e.held[T]
    assert len(e.sent) == 3 and not e.cancelled


def test_partial_entry_fills_share_full_coverage_without_waiting_for_each_tp(tmp_path):
    e, service, _, _ = setup(tmp_path)
    entry = e.make_order(count=20, price=".42")
    for _ in range(3):
        e.fill(entry, 5, ".42")
    e.hide_post = True
    assert not service.run_once()
    assert resting_quantity(e) == 15 and len(e.sent) == 3
    e.hidden_orders.clear()
    e.hide_post = False
    e.fill(entry, 5, ".42")
    assert service.run_once()
    assert resting_quantity(e) == 20 and len(e.sent) == 4


def test_external_sell_is_subtracted_from_total_batch_quantity(tmp_path):
    e, service, _, _ = setup(tmp_path)
    for _ in range(3):
        e.buy(5, ".42")
    external = e.make_order(count=7, price=".60", action="sell")
    e.hide_post = True
    assert not service.run_once()
    assert resting_quantity(e) == 8
    assert resting_quantity(e) + D(e.remote[external]["remaining_count_fp"]) == 15
    assert not e.cancelled


@pytest.mark.parametrize("failure", ["timeout", "rejection", "bad_identity"])
def test_second_order_failure_does_not_submit_third_or_release_unknown_size(tmp_path, monkeypatch, failure):
    e, service, settings, _ = setup(tmp_path)
    for _ in range(3):
        e.buy(5, ".42")
    original = e.place_manual_limit
    attempts = []

    def place(ticker, intent):
        attempts.append(intent["client_id"])
        if len(attempts) == 2:
            if failure == "rejection":
                raise KalshiAPIError(422, "definite rejection")
            result = original(ticker, intent)
            if failure == "timeout":
                raise TimeoutError("accepted response lost")
            return {**result, "client_order_id": "wrong-client"}
        return original(ticker, intent)

    monkeypatch.setattr(e, "place_manual_limit", place)
    assert not service.run_once()
    assert len(attempts) == 2
    assert len(service.worker.intents(T)) == (1 if failure == "rejection" else 2)
    assert not e.cancelled
    monkeypatch.setattr(e, "place_manual_limit", original)
    e.clock_value += 20
    restarted = ManualExitService(e, settings, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once()
    assert resting_quantity(e) == 15 and len(e.sent) == 3


def test_manual_position_reduction_during_batch_requires_fresh_replay(tmp_path, monkeypatch):
    e, service, _, _ = setup(tmp_path)
    for _ in range(3):
        e.buy(5, ".42")
    original = e.place_manual_limit
    changed = []

    def place(ticker, intent):
        result = original(ticker, intent)
        if not changed:
            manual = e.make_order(count=10, price=".40", action="sell")
            e.fill(manual, 10)
            changed.append(manual)
        return result

    monkeypatch.setattr(e, "place_manual_limit", place)
    assert service.run_once()
    assert e.held[T] == 5 and resting_quantity(e) == 5
    assert changed[0] not in e.cancelled
    assert len(e.sent) == 2  # First reservation retired; only five remain.


def test_batch_stops_when_snapshot_expires(tmp_path, monkeypatch):
    e, service, _, _ = setup(tmp_path)
    for _ in range(3):
        e.buy(5, ".42")
    original = e.place_manual_limit
    e.hide_post = True

    def slow_post(ticker, intent):
        result = original(ticker, intent)
        e.clock_value += 11
        return result

    monkeypatch.setattr(e, "place_manual_limit", slow_post)
    assert not service.run_once()
    assert len(e.sent) == 1 and len(service.worker.intents(T)) == 1
    assert not e.cancelled


def test_bounded_batch_keeps_all_submitted_reservations(tmp_path):
    e, service, _, _ = setup(tmp_path)
    for _ in range(21):
        e.buy(5, ".42")
    e.hide_post = True
    assert not service.run_once()
    assert len(e.sent) == 20 and resting_quantity(e) == 100
    e.hidden_orders.clear()
    e.hide_post = False
    assert service.run_once()
    assert len(e.sent) == 21 and resting_quantity(e) == 105


@pytest.mark.parametrize("sign", [1, -1])
def test_immediate_tp_fills_refresh_between_orders_without_cancel_churn(tmp_path, sign):
    e, service, _, _ = setup(tmp_path)
    for _ in range(4):
        e.buy(5, ".42", sign)
    e.bid = e.no_bid = D(".50")
    assert service.run_once()
    assert e.held[T] == 0 and len(e.sent) == 4
    assert not e.cancelled


@pytest.mark.parametrize("series", ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M", "KXWTI15M", "KXSILVER15M"])
def test_quantity_batches_apply_to_each_configured_series(tmp_path, series):
    from dataclasses import replace
    e, service, settings, _ = setup(tmp_path)
    if series not in settings.series:
        settings = replace(settings, series=(*settings.series, series))
        service = ManualExitService(e, settings, clock=e.clock, emit=lambda *a, **k: None)
    ticker = series + "-TEST"
    for _ in range(3):
        e.buy(5, ".42", ticker=ticker)
    e.hide_post = True
    assert not service.run_once()
    assert resting_quantity(e) == 15 and len(e.sent) == 3
    assert all(o["ticker"] == ticker for o in e.sent)
