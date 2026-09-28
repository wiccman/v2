"""98c overrides all bot targets while preserving manual inventory."""
from decimal import Decimal as D

import pytest

from test_fill_cost_targets import buy
from test_nine_cent_profit import worker
from test_price_pairs import PairExchange


def setup(tmp_path, bid, sign=1, liquidity="100"):
    e = PairExchange(bid=bid, liquidity=liquidity)
    buy(e, ".96", ".96", "6", sign, target="1")
    e.intents[-1]["hold_to_settlement"] = True
    m = worker(tmp_path, e)
    m.force_exit_price = D(".98")
    return e, m


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("bid,sold", [(".9799", False), (".98", True), (".99", True)])
def test_settlement_exits_at_98_bid_or_above(tmp_path, sign, bid, sold):
    e, m = setup(tmp_path, bid, sign)
    m.run_once(); m.run_once()
    assert m.healthy and bool(e.submissions) == sold
    assert e.held == (0 if sold else 6 * sign)
    if sold:
        assert e.submissions[0]["price"] == ("0.9800" if sign > 0 else "0.0200")
        assert e.submissions[0]["reduce_only"] and e.submissions[0]["count"] == "6"


@pytest.mark.parametrize("sign", [1, -1])
def test_all_bot_lots_exit_together_without_manual_lots(tmp_path, sign):
    e, m = setup(tmp_path, ".98", sign)
    e.manual(sign, "2")
    buy(e, ".57", ".50", "3", sign)
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 2 * sign
    assert len(e.submissions) == 1 and e.submissions[0]["count"] == "9"


@pytest.mark.parametrize("lost_ack", [False, True])
def test_partial_fill_restart_reconciles_before_selling_remainder(tmp_path, lost_ack):
    e, m = setup(tmp_path, ".98", liquidity="2")
    e.lose_ack = lost_ack
    m.run_once()
    assert e.held == 4
    e.lose_ack, e.liquidity = False, D(100)
    resumed = worker(tmp_path, e, m.path)
    resumed.force_exit_price = D(".98")
    resumed.run_once(); resumed.run_once()
    assert resumed.healthy and e.held == 0
    assert [o["count"] for o in e.submissions] == ["6", "4"]


def test_98_ask_is_not_a_sellable_98_bid(tmp_path):
    e, m = setup(tmp_path, ".97")
    e.market = lambda ticker: {"yes_bid_dollars": ".97", "no_bid_dollars": ".02",
                                "yes_ask_dollars": ".98", "last_price_dollars": ".98"}
    m.run_once()
    assert m.healthy and not e.submissions and e.held == 6


def test_nine_cent_normal_exit_still_runs_below_98(tmp_path):
    e = PairExchange(bid=".59")
    buy(e, ".57", ".50", "3")
    m = worker(tmp_path, e)
    m.force_exit_price = D(".98")
    m.run_once(); m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[0]["price"] == "0.5900"
