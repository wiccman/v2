"""Offline safety checks for the per-fill 11-cent manual stop."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from kalshi import KalshiAPIError
from manual_exit_runner import ManualExitService, Settings
from manual_limit_exits import ManualExitBlocked
from resting_manual_exits import RestingExitOnlyClient
from test_manual_limit_exits import Exchange, T
from test_resting_manual_exits import RestingExchange


class StopExchange(RestingExchange):
    place_manual_stop = RestingExitOnlyClient.place_manual_stop

    def request(self, method, path, params=None, body=None, auth=False):
        if method == "POST" and body and body.get("time_in_force") == "immediate_or_cancel":
            return Exchange.request(self, method, path, params=params, body=body, auth=auth)
        return super().request(method, path, params=params, body=body, auth=auth)


def setup(tmp_path, *, dry_run=False):
    e = StopExchange()
    state = tmp_path / "state.json"
    state.write_text('{"markets": {}}')
    state.with_name("state_take_profit.json").write_text('{"markets": {}}')
    config = replace(Settings.read({"STATE_PATH": str(state)}), dry_run=dry_run)
    events = []
    service = ManualExitService(e, config, clock=e.clock,
                                emit=lambda event, **details: events.append((event, details)))
    return e, service, config, events


@pytest.mark.parametrize("sign", [1, -1])
def test_stop_cancels_resting_profit_then_exits_only_owned_contracts(tmp_path, sign):
    e, service, _, events = setup(tmp_path)
    e.buy(10, ".42", sign)
    assert service.run_once()
    take_profit_id = next(oid for oid, row in e.remote.items()
                          if row["client_order_id"].startswith("manual5c-"))
    assert e.remote[take_profit_id]["status"] == "resting"
    if sign == 1:
        e.bid = D(".30")
    else:
        e.no_bid = D(".30")
    assert service.run_once()
    assert e.cancelled == [take_profit_id]
    assert e.remote[take_profit_id]["status"] == "canceled"
    assert e.held[T] == 0
    stop = [row for row in e.sent if row["time_in_force"] == "immediate_or_cancel"]
    assert len(stop) == 1
    assert stop[0]["reduce_only"] is True and stop[0]["count"] == "10"
    assert stop[0]["price"] == ("0.3000" if sign == 1 else "0.7000")
    assert any(event == "MANUAL_STOP_ARMED" for event, _ in events)
    assert service.run_once() and len(e.sent) == 2


def test_fallen_quote_before_first_exit_skips_take_profit(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.bid = D(".30")
    assert service.run_once() and e.held[T] == 0
    assert len(e.sent) == 1
    assert e.sent[0]["time_in_force"] == "immediate_or_cancel"


def test_stop_waits_for_confirmed_cancellation_and_remains_latched(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert service.run_once()
    e.bid = D(".30")
    e.delay_cancel = True
    assert not service.run_once()
    assert len(e.sent) == 1 and e.held[T] == 10
    assert not service.run_once() and len(e.sent) == 1
    # Even if the quote recovers before cancellation is confirmed, the stop
    # remains armed and executes without restoring the +5c order.
    e.bid = D(".40")
    e.delay_cancel = False
    e.clock_value += 6
    assert not service.run_once()
    assert service.run_once()
    assert e.held[T] == 0 and len(e.sent) == 2
    assert e.sent[-1]["price"] == "0.4000"


def test_stop_recovers_lost_ioc_ack_without_duplicate(tmp_path):
    e, service, config, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.bid = D(".30")
    e.lose_ack = True
    assert not service.run_once() and len(e.sent) == 1
    assert service.worker.ledger(T).get("pending")
    e.lose_ack = False
    restarted = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **kw: None)
    assert restarted.run_once()
    assert e.held[T] == 0 and len(e.sent) == 1
    assert not restarted.worker.ledger(T).get("pending")


def test_unfilled_stop_retries_after_quote_refresh_not_immediately(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.bid, e.liquidity = D(".30"), D(0)
    assert service.run_once() and len(e.sent) == 1
    assert e.held[T] == 10
    assert service.run_once() and len(e.sent) == 1
    e.clock_value += 4
    e.bid, e.liquidity = D(".28"), D(10)
    assert service.run_once()
    assert len(e.sent) == 2 and e.sent[-1]["price"] == "0.2800"
    assert e.held[T] == 0


def test_other_fills_are_not_stopped_at_the_wrong_entry_cost(tmp_path):
    e, service, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.buy(5, ".25")
    e.bid = D(".30")
    assert service.run_once()
    assert e.held[T] == 5
    stopped = [x for x in e.sent if x["time_in_force"] == "immediate_or_cancel"]
    assert len(stopped) == 1 and stopped[0]["count"] == "10"
    assert any(x["time_in_force"] == "good_till_canceled" for x in e.sent)


def test_live_stop_policy_cannot_change_under_existing_journal(tmp_path):
    e, service, config, _ = setup(tmp_path)
    with pytest.raises(ManualExitBlocked, match="Stop loss policy"):
        ManualExitService(e, replace(config, stop_loss_cents=D("12")),
                          clock=e.clock, emit=lambda *a, **kw: None)


@pytest.mark.parametrize("value", ["0", "-11", "100", "nan", "x"])
def test_invalid_stop_configuration_rejected(value):
    with pytest.raises((ManualExitBlocked, ValueError)):
        Settings.read({"MANUAL_STOP_LOSS_CENTS": value})
