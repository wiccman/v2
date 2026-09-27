"""A five percent gross price increase is per filled buy order, not per lot count."""
from decimal import Decimal as D

from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


def test_five_percent_uses_actual_average_fill_and_rounds_up(tmp_path):
    exchange = PairExchange(bid="0.58")
    buy(exchange, "0.57", "0.55", "4")
    worker, _ = monitor(tmp_path, exchange)
    worker.per_order_percentage = D("0.05")
    worker.per_order_profit = None
    worker.run_once()
    assert exchange.submissions[-1]["price"] == "0.5800"
    assert exchange.held == 0


def test_percentage_target_does_not_grow_for_smaller_lots(tmp_path):
    exchange = PairExchange(bid="0.58")
    buy(exchange, "0.57", "0.55", "1")
    worker, _ = monitor(tmp_path, exchange)
    worker.per_order_percentage = D("0.05")
    worker.per_order_profit = None
    worker.run_once()
    assert exchange.submissions[-1]["price"] == "0.5800"
