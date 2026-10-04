"""Offline regressions for final-two-minute intent/exit compatibility."""
from decimal import Decimal as D

import pytest
import bot
from entry_policy import SETTLEMENT_KIND
from test_five_minute_exits import cycle_setup
from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


def current_intent():
    return dict(kind=SETTLEMENT_KIND, price="0.96", exit_target="0.99",
                settlement_profit_dollars="0.30", side="YES", quantity="10",
                entry_execution_version=bot.ENTRY_EXECUTION_VERSION,
                resting_entry=True, order_id="current", client_id="current-client",
                cancel_at=1000000900, reserved_dollars="9.90")


def test_current_gtc_survives_poll_but_is_canceled_at_close(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 795)
    record.update(close_timestamp=closed.timestamp(), entry_intents=[current_intent()])
    bot.reconcile_entries(state)
    assert not fake.cancelled
    clock[0] = closed.timestamp()
    bot.reconcile_entries(state)
    assert fake.cancelled == ["current"]


@pytest.mark.parametrize("target,profit,hold,valid", [
    (".99", ".30", False, True), ("1", None, True, True),
    (".98", ".30", False, False), (".99", None, False, False),
    (".99", ".20", False, False), ("1", None, False, False),
])
def test_saved_target_validation(target, profit, hold, valid):
    item = current_intent()
    item.update(exit_target=target, hold_to_settlement=hold)
    if profit is None:
        item.pop("settlement_profit_dollars")
    else:
        item["settlement_profit_dollars"] = profit
    assert bot.tracked_entry_price_allowed(item) is valid


@pytest.mark.parametrize("sign", [1, -1])
def test_current_profit_target_survives_legacy_override_and_restart(tmp_path, sign):
    e = PairExchange(bid=".98")
    buy(e, ".96", ".96", "10", sign, target=".99")
    e.intents[-1].update(kind=SETTLEMENT_KIND, settlement_profit_dollars=".30")
    e.manual(sign, "2")
    def worker():
        m, _ = monitor(tmp_path, e)
        m.per_order_profit, m.force_exit_price, m.quote_gate = D(".30"), D(".98"), True
        e.market = lambda ticker: {"yes_bid_dollars": str(e.bid), "no_bid_dollars": str(e.bid)}
        return m
    m = worker()
    m.run_once()
    assert m.healthy and not e.submissions
    m = worker()
    e.bid = D(".99")
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 2 * sign
    assert len(e.submissions) == 1
    assert e.submissions[0]["count"] == "10"
    assert D(e.submissions[0]["price"]) == (D(".99") if sign > 0 else D(".01"))


def test_legacy_settlement_lot_keeps_its_98_override(tmp_path):
    e = PairExchange(bid=".98")
    e.market = lambda ticker: {"yes_bid_dollars": str(e.bid), "no_bid_dollars": str(e.bid)}
    buy(e, ".96", ".96", "6", target="1")
    e.intents[-1].update(kind=SETTLEMENT_KIND, hold_to_settlement=True)
    m, _ = monitor(tmp_path, e)
    m.force_exit_price = D(".98")
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[0]["price"] == "0.9800"


@pytest.mark.parametrize("sign", [1, -1])
def test_mixed_legacy_current_and_manual_allocations(tmp_path, sign):
    e = PairExchange(bid=".98")
    e.market = lambda ticker: {"yes_bid_dollars": str(e.bid), "no_bid_dollars": str(e.bid)}
    buy(e, ".96", ".96", "6", sign, target="1")
    e.intents[-1].update(kind=SETTLEMENT_KIND, hold_to_settlement=True)
    buy(e, ".96", ".96", "10", sign, target=".99")
    e.intents[-1].update(kind=SETTLEMENT_KIND, settlement_profit_dollars=".30")
    e.manual(sign, "2")
    m, _ = monitor(tmp_path, e)
    m.per_order_profit, m.force_exit_price, m.quote_gate = D(".30"), D(".98"), True
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 12 * sign
    assert [o["count"] for o in e.submissions] == ["6"]
    e.bid = D(".99")
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 2 * sign
    assert [o["count"] for o in e.submissions] == ["6", "10"]
