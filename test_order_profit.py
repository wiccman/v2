"""Verify dollar targets across one buy order without mixing separate trades."""
from decimal import Decimal as D

from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


def test_five_contract_buy_closes_for_75_cents_gross(tmp_path):
    e = PairExchange(bid="0.65")
    buy(e, "0.57", "0.50", "5")
    m, _ = monitor(tmp_path, e)
    m.per_order_profit = D("0.75")
    m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[-1]["price"] == "0.6500"
    assert e.submissions[-1]["count"] == "5"


def test_two_buy_orders_keep_separate_profit_goals(tmp_path):
    e = PairExchange(bid="0.65")
    buy(e, "0.57", "0.50", "5")
    buy(e, "0.53", "0.40", "5")
    m, _ = monitor(tmp_path, e)
    m.per_order_profit = D("0.75")
    m.run_once()
    assert e.submissions[-1]["price"] == "0.5500"
    assert e.submissions[-1]["count"] == "5"
    m.run_once()
    assert e.submissions[-1]["price"] == "0.6500"
    assert e.submissions[-1]["count"] == "5"
    assert e.held == 0 and m.healthy


def test_unattainable_one_contract_uses_saved_exit(tmp_path):
    e = PairExchange(bid="0.62")
    buy(e, "0.57", "0.57", "1")
    m, _ = monitor(tmp_path, e)
    m.per_order_profit = D("0.75")
    m.run_once()
    assert e.submissions[-1]["price"] == "0.6200"
    assert e.held == 0 and m.healthy
