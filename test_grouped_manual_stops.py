"""Multi-fill regressions for the live +3c TP and -3.1c stop worker."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService, Settings
from test_manual_limit_exits import T
from test_manual_stop_loss import StopExchange
from test_resting_manual_exits import exit_ids


def setup(tmp_path):
    path = tmp_path / "state.json"
    for journal in (path, path.with_name("state_take_profit.json")):
        journal.write_text('{"markets": {}}')
    config = replace(Settings.read({"STATE_PATH": str(path), "MANUAL_EXIT_INCREMENT_CENTS": "3",
                                    "MANUAL_STOP_LOSS_CENTS": "3.1"}), dry_run=False)
    exchange = StopExchange()
    service = ManualExitService(exchange, config, clock=exchange.clock, emit=lambda *a, **k: None)
    return exchange, service, config


def stops(e):
    return [order for order in e.sent if order["time_in_force"] == "immediate_or_cancel"]


@pytest.mark.parametrize("sign", [1, -1])
def test_eight_crossed_fills_close_together_after_confirmed_cancels(tmp_path, sign):
    e, service, _ = setup(tmp_path)
    e.bid = e.no_bid = D(".90")
    amounts = [".07", "1.10", "1.10", "1.09", "1.09", "1.08", "1.09", "1.09"]
    prices = [".916", ".900", ".902", ".905", ".909", ".913", ".910", ".911"]
    for amount, price in zip(amounts, prices):
        e.buy(amount, price, sign)
    e.buy(2, ".88", sign)
    assert service.run_once() and len(exit_ids(e)) == 9
    e.delay_cancel = True
    e.bid = e.no_bid = D(".86")
    assert not service.run_once() and not stops(e)
    assert len(set(e.cancelled)) == 8
    assert sum(D(e.remote[oid]["remaining_count_fp"]) for oid in e.cancelled) == D("7.71")
    e.delay_cancel = False
    e.clock_value += 6
    for _ in range(3):
        service.run_once()
    orders = stops(e)
    assert len(orders) == 1 and orders[0]["reduce_only"] is True
    assert D(orders[0]["count"]) == D("7.71") and e.held[T] == sign * 2
    resting = [oid for oid in exit_ids(e) if e.remote[oid]["status"] == "resting"]
    assert len(resting) == 1 and D(e.remote[resting[0]]["remaining_count_fp"]) == 2


def test_partial_grouped_stop_recovers_after_lost_ack_without_overselling(tmp_path):
    e, service, config = setup(tmp_path)
    for count in (2, 3, 4):
        e.buy(count, ".62")
    e.bid = D(".55")
    e.lose_ack, e.liquidity = True, D("3.5")
    assert not service.run_once()
    assert e.held[T] == D("5.5") and len(stops(e)) == 1
    pending = service.worker.ledger(T)["pending"]
    assert len(pending["allocations"]) == 3 and D(pending["quantity"]) == 9
    e.lose_ack, e.liquidity = False, D(100)
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert restarted.run_once() and e.held[T] == 0
    assert [D(order["count"]) for order in stops(e)] == [D(9), D("5.5")]
    assert restarted.run_once() and len(stops(e)) == 2


def test_manual_sell_reservation_is_excluded_from_group(tmp_path):
    e, service, _ = setup(tmp_path)
    e.buy(5, ".62")
    e.buy(5, ".61")
    manual = e.make_order(count=3, price=".70", action="sell")
    e.bid = D(".55")
    assert service.run_once()
    assert len(stops(e)) == 1 and D(stops(e)[0]["count"]) == 7
    assert e.held[T] == 3 and manual not in e.cancelled
    assert e.remote[manual]["status"] == "resting"


def test_confirmed_unfilled_group_reprices_after_one_second_but_respects_api_backoff(tmp_path):
    e, service, _ = setup(tmp_path)
    e.buy(2, ".62")
    e.buy(3, ".63")
    e.bid, e.liquidity = D(".55"), D(0)
    assert service.run_once() and len(stops(e)) == 1 and e.held[T] == 5
    ledger = service.worker.ledger(T)
    assert ledger["next_attempt_at"] == e.clock() + 1 and not ledger.get("pending")
    e.bid, e.liquidity = D(".54"), D(5)
    e.clock_value += .9
    assert service.run_once() and len(stops(e)) == 1
    ledger["retry_after"] = e.clock() + .5
    e.clock_value = ledger["next_attempt_at"]
    service.run_once()
    assert len(stops(e)) == 1
    e.clock_value = ledger["retry_after"] + .01
    assert service.run_once() and e.held[T] == 0 and len(stops(e)) == 2
    assert D(stops(e)[1]["price"]) == D(".54")
