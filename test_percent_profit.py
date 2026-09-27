"""A five percent target allows fee room per filled buy order."""
from decimal import Decimal as D

from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


def test_five_percent_uses_actual_average_fill_and_rounds_up(tmp_path):
    exchange = PairExchange(bid="0.64")
    buy(exchange, "0.57", "0.55", "4")
    worker, _ = monitor(tmp_path, exchange)
    worker.per_order_percentage = D("0.05")
    worker.per_order_profit = None
    worker.run_once()
    assert exchange.submissions[-1]["price"] == "0.6400"
    assert exchange.held == 0


def test_percentage_target_does_not_grow_for_smaller_lots(tmp_path):
    exchange = PairExchange(bid="0.64")
    buy(exchange, "0.57", "0.55", "1")
    worker, _ = monitor(tmp_path, exchange)
    worker.per_order_percentage = D("0.05")
    worker.per_order_profit = None
    worker.run_once()
    assert exchange.submissions[-1]["price"] == "0.6400"


def test_average_down_keeps_both_lots_covered_by_their_own_targets(tmp_path):
    exchange = PairExchange(bid="0.20")
    exchange.market = lambda ticker: {
        "yes_bid_dollars": str(exchange.bid), "no_bid_dollars": str(exchange.bid)}
    buy(exchange, "0.56", "0.55", "5")
    worker, _ = monitor(tmp_path, exchange)
    worker.per_order_percentage = D("0.05")
    worker.per_order_profit = None
    worker.quote_gate = True
    worker.run_once()
    assert not exchange.submissions

    buy(exchange, "0.45", "0.45", "3")
    exchange.bid = D("0.54")
    worker.run_once()
    assert exchange.submissions[-1]["count"] == "3"
    assert exchange.submissions[-1]["price"] == "0.5400"
    assert exchange.held == 5

    worker.run_once()  # Confirm the lower-price lot's sale.
    exchange.bid = D("0.64")
    worker.run_once()
    assert exchange.submissions[-1]["count"] == "5"
    assert exchange.submissions[-1]["price"] == "0.6400"
    worker.run_once()
    assert exchange.held == 0 and worker.healthy
