"""Fixed price movement targets, verified with offline YES and NO fills."""
from decimal import Decimal as D

import pytest

from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


def worker(tmp_path, exchange, path=None):
    service, events = monitor(tmp_path, exchange, path)
    service.per_order_increment = D(".09")
    service.quote_gate = True
    exchange.market = lambda ticker: {"yes_bid_dollars": str(exchange.bid),
                                      "no_bid_dollars": str(exchange.bid)}
    return service


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("quantity", ["1", "5"])
@pytest.mark.parametrize("limit,cost,target", [(".57", ".50", ".59"), (".75", ".75", ".84"), (".85", ".85", ".94")])
def test_nine_cent_move_uses_actual_fill_and_does_not_depend_on_quantity(tmp_path, sign, quantity, limit, cost, target):
    e = PairExchange(bid=str(D(target) - D(".01")))
    buy(e, limit, cost, quantity, sign)
    m = worker(tmp_path, e)
    m.run_once()
    assert m.healthy and not e.submissions
    e.bid = D(target)
    m.run_once()
    assert e.held == 0 and e.submissions[-1]["count"] == quantity
    assert D(e.submissions[-1]["price"]) == (D(target) if sign > 0 else 1 - D(target))
    assert e.submissions[-1]["reduce_only"]


@pytest.mark.parametrize("sign", [1, -1])
def test_average_down_has_separate_target_and_partial_exit_survives_restart(tmp_path, sign):
    e = PairExchange(bid=".20", liquidity="2")
    buy(e, ".57", ".50", "5", sign)
    m = worker(tmp_path, e)
    m.run_once()
    buy(e, ".45", ".40", "3", sign)
    e.bid = D(".49")
    # The rotation can inspect the higher target first.
    m.run_once(); m.run_once()
    assert e.held == 6 * sign
    e.bid, e.liquidity = D(".20"), D(100)
    restarted = worker(tmp_path, e, m.path)
    restarted.run_once()
    assert restarted.healthy and len(e.submissions) == 1
    e.bid = D(".49")
    restarted.run_once(); restarted.run_once()
    assert e.held == 5 * sign and e.submissions[-1]["count"] == "1"
    e.bid = D(".59")
    restarted.run_once(); restarted.run_once()
    assert e.held == 0 and e.submissions[-1]["count"] == "5"


@pytest.mark.parametrize("sign", [1, -1])
def test_same_order_partial_fills_use_weighted_cost_and_round_up(tmp_path, sign):
    e = PairExchange(bid=".20")
    oid = buy(e, ".57", ".57", "2", sign)
    m = worker(tmp_path, e)
    m.run_once()
    e.fill(oid, sign, D(3), ".473" if sign > 0 else ".527")
    e.held += 3 * sign
    e.bid = D(".60")
    m.run_once()
    assert m.healthy and not e.submissions
    e.bid = D(".61")
    m.run_once()
    assert e.held == 0 and e.submissions[-1]["count"] == "5"
    assert e.submissions[-1]["price"] == ("0.6100" if sign > 0 else "0.3900")


@pytest.mark.parametrize("sign", [1, -1])
def test_manual_and_settlement_inventory_are_not_sold(tmp_path, sign):
    e = PairExchange(bid=".99")
    e.manual(sign, "2")
    buy(e, ".57", ".50", "3", sign)
    buy(e, ".96", ".96", "6", sign, target="1")
    e.intents[-1]["hold_to_settlement"] = True
    m = worker(tmp_path, e)
    for _ in range(3):
        m.run_once()
    assert m.healthy and e.held == 8 * sign
    assert len(e.submissions) == 1 and e.submissions[0]["count"] == "3"


def test_upgrade_reconciles_old_dollar_target_before_new_exit(tmp_path):
    e = PairExchange(bid=".65", liquidity="2")
    buy(e, ".57", ".50", "5")
    old, _ = monitor(tmp_path, e)
    old.per_order_profit = D(".75")
    old.run_once()
    assert e.held == 3
    e.bid, e.liquidity = D(".59"), D(100)
    new = worker(tmp_path, e, old.path)
    new.run_once(); new.run_once()
    assert new.healthy and e.held == 0
    assert e.submissions[-1]["count"] == "3" and e.submissions[-1]["price"] == "0.5900"
