"""Filled-position and pending-order limits for 15-minute scalp entries."""
from decimal import Decimal as D

import bot
import entry_policy
from test_five_minute_exits import cycle_setup


def test_one_average_down_is_three_contracts_at_most_and_does_not_reset_on_partial_sale(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    fake.held = D(0)
    first, quantity = bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")
    assert first["order_id"] and quantity == 5
    # IOC reconciliation has closed the first order; five shares remain held.
    record["entry_intents"][-1]["entry_closed"] = True
    fake.held = D(5)
    clock[0] += 10
    second, quantity = bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")
    assert second["order_id"] and quantity == 3
    assert record["entry_intents"][-1]["averaging_entry"] is True
    record["entry_intents"][-1]["entry_closed"] = True
    fake.held = D(2)  # Partial take profit cannot refill averaging allowance.
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular") == ({}, D(0))
    fake.held = D(0)
    third, quantity = bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")
    assert third["order_id"] and quantity == 5


def test_pending_orders_count_against_initial_and_open_contract_limit(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    fake.held = D(0)
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")[1] == 5
    # Unknown acknowledgement may still fill all five contracts.
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular") == ({}, D(0))
    record["entry_intents"][-1]["entry_closed"] = True
    fake.held = D(7)
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")[1] == 1


def test_averaging_closes_at_minute_three_and_legacy_position_waits_until_flat(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    fake.held = D(5)
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular") == ({}, D(0))
    fake.held = D(0)
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular")[1] == 5
    record["entry_intents"][-1]["entry_closed"] = True
    fake.held = D(5)
    clock[0] = 1000000180
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.53"), closed, "regular") == ({}, D(0))


def test_settlement_order_respects_explicit_contract_cap():
    record = {"entry_intents": [], "entry_budget_legacy": False}
    assert entry_policy.entry_quantity(D("0.96"), entry_policy.SETTLEMENT_KIND,
                                       record, max_quantity=D(3)) == 3
    intent = entry_policy.reserve(record, "YES", D("0.96"), D(6), D(21), 900,
                                  entry_policy.SETTLEMENT_KIND, max_quantity=D(3))
    assert intent["quantity"] == "3"
