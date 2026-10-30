"""Offline regressions for quantity coverage by existing app/manual exits."""
import json
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService
from test_manual_limit_exits import T, auto_buy
from test_resting_manual_exits import setup, exit_ids


def bot_remaining(exchange):
    return sum((D(exchange.remote[oid]["remaining_count_fp"])
                for oid in exit_ids(exchange)
                if exchange.remote[oid]["status"] in {"resting", "pending"}), D(0))


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("external_action", ["sell", "buy"])
def test_existing_exit_for_older_buy_allows_new_buy_own_five_cent_target(
        tmp_path, sign, external_action):
    exchange, service, settings, events = setup(tmp_path)
    exchange.buy(10, ".42", sign)
    external = exchange.make_order(sign=sign if external_action == "sell" else -sign,
        count=10, price=".70", action=external_action)
    exchange.buy(10, ".50", sign)

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("10", "0.5500" if sign == 1 else "0.4500")]
    assert exchange.remote[external]["status"] == "resting"
    assert not exchange.cancelled
    assert service.run_once() and len(exchange.sent) == 1

    restarted = ManualExitService(exchange, settings, clock=exchange.clock,
                                 emit=lambda *args, **kwargs: None)
    assert restarted.run_once() and len(exchange.sent) == 1
    assert bot_remaining(exchange) == 10 and not exchange.cancelled


@pytest.mark.parametrize("sign", [1, -1])
def test_fully_covered_position_does_not_add_duplicate_exit(tmp_path, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42", sign)
    external = exchange.make_order(sign=sign, count=10, price=".70", action="sell")

    assert service.run_once(), events
    assert service.run_once() and not exchange.sent and not exchange.cancelled
    assert exchange.remote[external]["remaining_count_fp"] == "10"


@pytest.mark.parametrize("sign", [1, -1])
def test_external_coverage_on_older_lot_preserves_valid_bot_exit_on_newer_lot(tmp_path, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(5, ".50", sign)
    external = exchange.make_order(sign=sign, count=5, price=".70", action="sell")
    exchange.buy(10, ".42", sign)
    assert service.run_once(), events
    original_exit = exit_ids(exchange)[0]

    assert service.run_once(), events
    assert len(exchange.sent) == 1 and not exchange.cancelled
    assert exchange.remote[original_exit]["remaining_count_fp"] == "10"
    assert exchange.remote[external]["remaining_count_fp"] == "5"

    exchange.buy(4, ".60", sign)
    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("10", "0.4700" if sign == 1 else "0.5300"),
        ("4", "0.6500" if sign == 1 else "0.3500")]
    assert not exchange.cancelled


@pytest.mark.parametrize("sign", [1, -1])
def test_external_partial_fill_reserves_remaining_not_initial_count(tmp_path, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42", sign)
    external = exchange.make_order(sign=sign, count=10, price=".70", action="sell")
    exchange.fill(external, 4)
    exchange.buy(10, ".50", sign)

    assert exchange.held[T] == sign * 16
    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("10", "0.5500" if sign == 1 else "0.4500")]
    assert exchange.remote[external]["initial_count_fp"] == "10"
    assert exchange.remote[external]["remaining_count_fp"] == "6"
    assert not exchange.cancelled


def test_multiple_external_orders_reserve_their_combined_remaining_size(tmp_path):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(20, ".42")
    first = exchange.make_order(count=3, price=".70", action="sell")
    second = exchange.make_order(count=4, price=".75", action="sell")

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [("13", "0.4700")]
    assert bot_remaining(exchange) == 13 and not exchange.cancelled
    assert all(exchange.remote[oid]["status"] == "resting" for oid in (first, second))


@pytest.mark.parametrize("sign", [1, -1])
def test_external_coverage_preserves_fractional_contracts(tmp_path, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy("1.75", ".42", sign)
    exchange.make_order(sign=sign, count="1.75", price=".70", action="sell")
    exchange.buy("2.35", ".50", sign)

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("2.35", "0.5500" if sign == 1 else "0.4500")]
    assert bot_remaining(exchange) == D("2.35") and not exchange.cancelled


@pytest.mark.parametrize("status", ["canceled", "empty_resting"])
def test_terminal_or_empty_external_order_does_not_reserve_inventory(tmp_path, status):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=0 if status == "empty_resting" else 10,
                                   price=".70", action="sell")
    if status == "canceled":
        exchange.cancel(external, T)
        exchange.cancelled.clear()

    assert service.run_once(), events
    assert len(exchange.sent) == 1 and bot_remaining(exchange) == 10
    assert not exchange.cancelled


@pytest.mark.parametrize("scope", ["different_ticker", "different_subaccount", "nonreducing"])
def test_only_primary_same_market_reducing_orders_consume_coverage(tmp_path, scope):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    extra = {"ticker": "KXETH15M-OTHER"} if scope == "different_ticker" else {}
    if scope == "different_subaccount":
        extra["subaccount"] = 1
    external = exchange.make_order(count=10, price=".70",
        action="buy" if scope == "nonreducing" else "sell", **extra)

    assert service.run_once(), events
    assert len(exchange.sent) == 1 and bot_remaining(exchange) == 10
    assert exchange.remote[external]["status"] == "resting" and not exchange.cancelled


def test_external_order_growth_before_submission_replans_without_overselling(tmp_path, monkeypatch):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=5, price=".70", action="sell")
    original = service.worker.verify_snapshot
    changed = False

    def grow_once(ticker):
        nonlocal changed
        if not changed:
            changed = True
            exchange.remote[external].update(initial_count_fp="10", remaining_count_fp="10")
        return original(ticker)

    monkeypatch.setattr(service.worker, "verify_snapshot", grow_once)
    assert service.run_once(), events
    assert changed and not exchange.sent and not exchange.cancelled
    assert not service.worker.pending(T)
    assert any(event == "MANUAL_RECONCILIATION_REFRESH" for event, _ in events)


def test_external_growth_during_intent_save_is_rechecked_before_post(tmp_path, monkeypatch):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=5, price=".70", action="sell")
    original = service.worker.save
    changed = False

    def grow_while_saving():
        nonlocal changed
        original()
        if service.worker.pending(T) and not changed:
            changed = True
            exchange.remote[external].update(initial_count_fp="10", remaining_count_fp="10")

    monkeypatch.setattr(service.worker, "save", grow_while_saving)
    assert service.run_once(), events
    assert changed and not exchange.sent and not exchange.cancelled
    assert not service.worker.pending(T)


def test_canceling_external_exit_releases_only_its_remaining_coverage(tmp_path):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=10, price=".70", action="sell")
    assert service.run_once(), events
    assert not exchange.sent
    exchange.cancel(external, T)
    exchange.cancelled.clear()

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [("10", "0.4700")]
    assert service.run_once() and len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("sign", [1, -1])
def test_overlapping_external_exit_resizes_only_bot_owned_reservation(tmp_path, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42", sign)
    assert service.run_once()
    original_exit = exit_ids(exchange)[0]
    external = exchange.make_order(sign=sign, count=4, price=".70", action="sell")

    for _ in range(3):
        service.run_once()
    assert exchange.cancelled == [original_exit], events
    assert exchange.remote[external]["status"] == "resting"
    assert bot_remaining(exchange) == 6
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("10", "0.4700" if sign == 1 else "0.5300"),
        ("6", "0.4700" if sign == 1 else "0.5300")]


def test_external_overcoverage_never_causes_new_sell_or_user_cancel(tmp_path):
    exchange, service, _, _ = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=15, price=".70", action="sell")

    service.run_once()
    service.run_once()
    assert not exchange.sent and not exchange.cancelled
    assert exchange.remote[external]["remaining_count_fp"] == "15"


@pytest.mark.parametrize("first_fill", ["external", "bot"])
@pytest.mark.parametrize("sign", [1, -1])
def test_external_and_bot_fills_before_next_poll_reconcile_in_either_order(tmp_path, first_fill, sign):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(5, ".42", sign)
    external = exchange.make_order(sign=sign, count=5, price=".70", action="sell")
    exchange.buy(10, ".50", sign)
    assert service.run_once(), events
    bot = exit_ids(exchange)[0]

    sequence = [(external, 5, ".70"), (bot, 4, ".55")]
    if first_fill == "bot":
        sequence.reverse()
    for oid, size, price in sequence:
        exchange.fill(oid, size, price)

    assert service.run_once(), events
    assert exchange.held[T] == sign * 6
    assert bot_remaining(exchange) == 6 and len(exchange.sent) == 1
    assert not exchange.cancelled
    exchange.fill(bot, 6, ".55")
    assert service.run_once(), events
    assert exchange.held[T] == 0 and not bot_remaining(exchange)
    assert len(exchange.sent) == 1 and not exchange.cancelled


@pytest.mark.parametrize("older_inventory", ["automatic", "pre_activation"])
@pytest.mark.parametrize("sign", [1, -1])
def test_external_fifo_reservation_includes_older_unmanaged_inventory(
        tmp_path, older_inventory, sign):
    exchange, service, settings, events = setup(tmp_path)
    if older_inventory == "automatic":
        entries = {"markets": {}}
        auto_buy(exchange, entries, count=10, price=".42", sign=sign)
        settings.entry_state.write_text(json.dumps(entries))
    else:
        exchange.clock_value -= 10
        exchange.buy(10, ".42", sign)
        exchange.clock_value += 10
    external = exchange.make_order(sign=sign, count=10, price=".70", action="sell")
    exchange.buy(5, ".50", sign)

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("5", "0.5500" if sign == 1 else "0.4500")]
    assert not exchange.cancelled
    exchange.fill(external, 10, ".70")
    exchange.fill(exit_ids(exchange)[0], 5, ".55")
    assert service.run_once(), events
    assert exchange.held[T] == 0 and len(exchange.sent) == 1


def test_external_overlap_waits_for_cancel_confirmation_before_reusing_size(tmp_path):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    assert service.run_once()
    original_exit = exit_ids(exchange)[0]
    external = exchange.make_order(count=4, price=".70", action="sell")
    exchange.delay_cancel = True

    service.run_once()
    assert exchange.remote[original_exit]["status"] == "resting"
    assert len(exchange.sent) == 1 and bot_remaining(exchange) == 10
    exchange.buy(2, ".50")
    service.run_once()
    assert len(exchange.sent) == 1 and bot_remaining(exchange) == 10
    assert exchange.remote[external]["remaining_count_fp"] == "4"

    exchange.delay_cancel = False
    exchange.clock_value += 6
    for _ in range(3):
        service.run_once()
    assert exchange.remote[original_exit]["status"] == "canceled", events
    assert set(exchange.cancelled) == {original_exit}
    assert [(order["count"], order["price"]) for order in exchange.sent] == [
        ("10", "0.4700"), ("6", "0.4700"), ("2", "0.5500")]
    assert bot_remaining(exchange) == 8
    assert exchange.remote[external]["status"] == "resting"


@pytest.mark.parametrize("first_fill", ["external", "bot"])
def test_partial_external_and_bot_exits_for_same_buy_replay_in_either_order(tmp_path, first_fill):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(20, ".42")
    external = exchange.make_order(count=7, price=".70", action="sell")
    assert service.run_once(), events
    bot = exit_ids(exchange)[0]
    assert bot_remaining(exchange) == 13
    sequence = [(external, 3, ".70"), (bot, 5, ".47")]
    if first_fill == "bot":
        sequence.reverse()
    for oid, size, price in sequence:
        exchange.fill(oid, size, price)

    assert service.run_once(), events
    assert exchange.held[T] == 12
    assert exchange.remote[external]["remaining_count_fp"] == "4"
    assert bot_remaining(exchange) == 8
    assert len(exchange.sent) == 1 and not exchange.cancelled
    exchange.fill(external, 4, ".70")
    exchange.fill(bot, 8, ".47")
    assert service.run_once(), events
    assert exchange.held[T] == 0 and len(exchange.sent) == 1
    assert not exchange.cancelled


@pytest.mark.parametrize("converges", [True, False])
def test_external_fill_count_must_match_fill_and_position_snapshot(tmp_path, monkeypatch, converges):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(20, ".42")
    external = exchange.make_order(count=10, price=".70", action="sell")
    fill_id = exchange.fill(external, 4)
    original_fills, original_positions = exchange.all_fills, exchange.positions
    fill_reads, position_reads = 0, 0

    def lag_fills(ticker):
        nonlocal fill_reads
        fill_reads += 1
        rows = original_fills(ticker)
        if not converges or fill_reads == 1:
            return [row for row in rows if row["fill_id"] != fill_id]
        return rows

    def lag_position(ticker):
        nonlocal position_reads
        position_reads += 1
        if not converges or position_reads == 1:
            return [{"ticker": ticker, "position_fp": "20", "subaccount_number": 0}]
        return original_positions(ticker)

    monkeypatch.setattr(exchange, "all_fills", lag_fills)
    monkeypatch.setattr(exchange, "positions", lag_position)
    assert service.run_once() == converges, events
    assert [(order["count"], order["price"]) for order in exchange.sent] == (
        [("10", "0.4700")] if converges else [])
    assert not exchange.cancelled
    refreshes = [data for event, data in events if event == "MANUAL_RECONCILIATION_REFRESH"]
    assert len(refreshes) == 1
    assert refreshes[0]["reason"] == "External order/fill counts have not converged"


def test_decreased_external_order_reserves_only_current_remaining_count(tmp_path):
    exchange, service, _, events = setup(tmp_path)
    exchange.buy(10, ".42")
    external = exchange.make_order(count=10, price=".70", action="sell")
    exchange.fill(external, 2)
    # A decrease removes five unfilled contracts while retaining original size.
    exchange.remote[external]["remaining_count_fp"] = "3"

    assert service.run_once(), events
    assert [(order["count"], order["price"]) for order in exchange.sent] == [("5", "0.4700")]
    assert exchange.held[T] == 8 and bot_remaining(exchange) == 5
    assert exchange.remote[external]["initial_count_fp"] == "10"
    assert exchange.remote[external]["fill_count_fp"] == "2"
    assert exchange.remote[external]["remaining_count_fp"] == "3"
    assert not exchange.cancelled


@pytest.mark.parametrize("paired", [False, True])
def test_known_automatic_exit_does_not_get_treated_as_external_fifo(tmp_path, paired):
    exchange, service, settings, events = setup(tmp_path)
    entries = {"markets": {}}
    auto_buy(exchange, entries, count=10, price=".42")
    settings.entry_state.write_text(json.dumps(entries))
    automatic_fill = exchange.history[-1]["fill_id"]
    automatic_exit = exchange.make_order(count=10, price=".55", action="sell",
        client_id="automatic-exit-client-sentinel")
    ownership = {"side": "YES", "target": ".55", "filled": "0", "paired": paired}
    if not paired:
        ownership["allocations"] = [{"fill_id": automatic_fill, "quantity": "10"}]
    settings.automatic_exit_state.write_text(json.dumps({
        "markets": {T: {"exit_orders": {automatic_exit: ownership}}}}))
    exchange.buy(5, ".50")

    assert not service.run_once()
    assert not exchange.sent and not exchange.cancelled
    assert exchange.remote[automatic_exit]["status"] == "resting"
    pause = [data for event, data in events if event == "MANUAL_EXIT_PAUSED"][-1]
    assert pause["stage"] == "observe_inventory"
    assert pause["reason"] == "A managed automatic exit can reduce the shared position"
    assert pause["check_source"] == "manual_limit_exits.py" and pause["check_line"] > 0
    assert "automatic-exit-client-sentinel" not in json.dumps(pause)
    assert "offline-test-key" not in json.dumps(pause)
