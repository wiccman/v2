"""Actual fill pricing, durable allocation, and upgrade tests; offline only."""
import copy
import json
from decimal import Decimal as D

import pytest

import bot
from price_pairs import fill_cost_inventory
from take_profit import TakeProfitMonitor
from test_price_pairs import PairExchange


def buy(exchange, limit, cost, quantity="5", sign=1, target=None):
    yes_price = D(cost) if sign > 0 else 1 - D(cost)
    oid = exchange.buy("0.39", quantity, sign, actual_price=str(yes_price))
    exchange.intents[-1].update(price=limit,
        exit_target=target or str(bot.ALL_ENTRY_EXIT_PAIRS[D(limit)]))
    return oid


def monitor(tmp_path, exchange, path=None):
    state = {"markets": {"T": {"close_timestamp": 1900, "entry_intents": exchange.intents}}}
    events = []
    svc = TakeProfitMonitor(exchange, lambda: copy.deepcopy(state), path or tmp_path / "cost.json",
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, fill_cost_targets=True, clock=lambda: 1000,
        emit=lambda event, **data: events.append((event, data)))
    return svc, events


@pytest.mark.parametrize("sign,wire", [(1, "0.6300"), (-1, "0.3700")])
def test_better_fill_exits_at_cost_plus_increment_not_original_limit(tmp_path, sign, wire):
    e = PairExchange(bid="0.65")
    buy(e, "0.64", "0.573", sign=sign)
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[-1]["price"] == wire
    assert e.submissions[-1]["count"] == "5"
    armed = next(data for event, data in events if event == "TP_ARMED")
    assert armed["cost_groups"] == [{"average_fill_cost": "0.573", "quantity": "5", "profit_increment": "0.05"}]
    m.run_once()
    assert len(e.submissions) == 1 and m.healthy


@pytest.mark.parametrize("sign", [1, -1])
def test_averaging_down_resizes_and_reprices_all_unsold_shares(tmp_path, sign):
    e = PairExchange()
    buy(e, "0.57", "0.57", sign=sign)
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert m.state["markets"]["T"]["armed"]["target"] == "0.62"
    buy(e, "0.53", "0.47", sign=sign)
    m.run_once()
    armed = m.state["markets"]["T"]["armed"]
    assert armed["target"] == "0.57" and armed["quantity"] == "10"
    assert armed["cost_groups"][0]["average_fill_cost"] == "0.52"
    assert m.healthy and e.held == 10 * sign


@pytest.mark.parametrize("sign", [1, -1])
def test_partial_entry_fills_recalculate_weighted_average(tmp_path, sign):
    e = PairExchange()
    oid = buy(e, "0.57", "0.57", "2", sign)
    m, _ = monitor(tmp_path, e)
    m.run_once()
    e.fill(oid, sign, D(3), "0.47" if sign > 0 else "0.53")
    e.held += 3 * sign
    m.run_once()
    armed = m.state["markets"]["T"]["armed"]
    assert armed["target"] == "0.56" and armed["quantity"] == "5"
    assert armed["cost_groups"][0]["average_fill_cost"] == "0.51"


@pytest.mark.parametrize("sign", [1, -1])
def test_partial_exit_then_restart_and_new_buy_excludes_sold_cost(tmp_path, sign):
    e = PairExchange(bid="0.60", liquidity="2")
    buy(e, "0.57", "0.57", "2", sign)
    buy(e, "0.53", "0.47", "3", sign)
    m, _ = monitor(tmp_path, e)
    m.run_once()  # Five shares at average 51c + 5c; sells the oldest two.
    assert e.held == 3 * sign
    saved = json.loads(m.path.read_text())["markets"]["T"]["pending"]
    assert saved["allocations"] == [{"fill_id": "f0001", "quantity": "2"},
                                    {"fill_id": "f0002", "quantity": "3"}]
    buy(e, "0.48", "0.42", "2", sign)
    e.bid = D("0.20")
    restarted, _ = monitor(tmp_path, e, m.path)
    restarted.run_once()
    armed = restarted.state["markets"]["T"]["armed"]
    assert armed["quantity"] == "5" and armed["target"] == "0.50"
    assert armed["cost_groups"][0]["average_fill_cost"] == "0.45"
    assert restarted.healthy
    e.bid, e.liquidity = D("0.50"), D(100)
    restarted.run_once()
    restarted.run_once()
    assert e.held == 0 and restarted.healthy


@pytest.mark.parametrize("sign", [1, -1])
def test_lost_ack_recovers_allocations_before_new_target(tmp_path, sign):
    e = PairExchange(bid="0.65", liquidity="2")
    buy(e, "0.64", "0.57", sign=sign)
    e.lose_ack = True
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and e.held == 3 * sign
    buy(e, "0.48", "0.47", "2", sign)
    e.lose_ack, e.bid = False, D("0.20")
    restarted, _ = monitor(tmp_path, e, m.path)
    restarted.run_once()
    assert restarted.healthy and len(e.submissions) == 2
    armed = restarted.state["markets"]["T"]["armed"]
    assert armed["quantity"] == "5" and armed["target"] == "0.58"
    exits = restarted.state["markets"]["T"]["exit_orders"]
    assert next(iter(exits.values()))["allocations"] == [{"fill_id": "f0001", "quantity": "5"}]


@pytest.mark.parametrize("sign", [1, -1])
def test_distinct_profit_increments_and_settlement_hold_stay_separate(tmp_path, sign):
    e = PairExchange(bid="0.98")
    buy(e, "0.57", "0.57", sign=sign)
    buy(e, "0.70", "0.68", sign=sign)
    buy(e, "0.75", "0.72", sign=sign)
    buy(e, "0.97", "0.97", "6", sign, target="1")
    e.intents[-1]["hold_to_settlement"] = True
    m, events = monitor(tmp_path, e)
    for _ in range(5):
        m.run_once()
    assert m.healthy and e.held == 6 * sign
    targets = [data["target"] for event, data in events if event == "TP_SUBMITTED"]
    assert targets == ["0.62", "0.74", "0.80"]
    assert all(order["count"] == "5" for order in e.submissions)


@pytest.mark.parametrize("pending", [False, True])
def test_upgrade_replays_original_fixed_exit_before_repricing_remaining(tmp_path, pending):
    e = PairExchange(bid="0.61")
    buy(e, "0.56", "0.55")
    buy(e, "0.64", "0.573")
    old, _ = monitor(tmp_path, e)
    old.fill_cost_targets = False
    old.run_once()  # Legacy fixed 61c exit sells only the first order.
    if not pending:
        old.run_once()  # Persist its receipt, leave an unfilled fixed 69c pending.
    assert e.held == 5
    e.bid = D("0.65")
    new, _ = monitor(tmp_path, e, old.path)
    new.run_once()
    assert new.healthy and e.held == 0
    assert e.submissions[-1]["price"] == "0.6300"
    new.run_once()
    assert new.healthy


def test_missing_fill_history_blocks_new_target_until_consistent(tmp_path):
    e = PairExchange(bid="0.65", liquidity="2")
    buy(e, "0.64", "0.573")
    m, _ = monitor(tmp_path, e)
    m.run_once()
    missing = e.history.pop()
    m.run_once()
    assert not m.healthy and len(e.submissions) == 1
    e.history.append(missing)
    m.run_once()
    assert m.healthy and len(e.submissions) == 2
    assert e.submissions[-1]["count"] == "3"


@pytest.mark.parametrize("change", [
    {"yes_price_dollars": None}, {"yes_price_dollars": "NaN"},
    {"yes_price_dollars": "0"}, {"yes_price_dollars": "1"},
    {"yes_price_dollars": "0.65"}, {"no_price_dollars": "0.50"},
])
def test_unverifiable_or_impossible_cost_pauses_without_fallback(tmp_path, change):
    e = PairExchange(bid="0.99")
    buy(e, "0.64", "0.57")
    e.history[0].update(change)
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions
    assert any(event == "TP_ERROR" for event, _ in events)


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("prices", [
    {"yes_price_dollars": "0.573", "no_price_dollars": "0.427"},
    {"no_price_dollars": "0.427"}, {"yes_price": 57, "no_price": 43},
    {"yes_price_dollars": "0.573", "yes_price": 57},
    {"yes_price_dollars": "0.573", "yes_price": 57, "no_price": 43},
])
def test_price_fields_use_outcome_and_prefer_decimal_dollars(tmp_path, sign, prices):
    e = PairExchange()
    buy(e, "0.64", "0.57", sign=sign)
    e.history[0].pop("yes_price_dollars")
    e.history[0].update(prices)
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert m.healthy
    exact = "0.62" if prices == {"yes_price": 57, "no_price": 43} else "0.63"
    assert m.state["markets"]["T"]["armed"]["target"] == (exact if sign > 0 else "0.48")


def test_manual_sale_uses_fifo_before_average_cost(tmp_path):
    e = PairExchange()
    buy(e, "0.57", "0.57", "2")
    buy(e, "0.53", "0.47", "3")
    e.manual(-1, "2")
    m, _ = monitor(tmp_path, e)
    m.run_once()
    armed = m.state["markets"]["T"]["armed"]
    assert armed["quantity"] == "3" and armed["target"] == "0.52"


def test_partial_exit_cannot_consume_new_shares_at_a_changed_target():
    e = PairExchange()
    buy(e, "0.57", "0.57", "2")
    buy(e, "0.53", "0.47", "3")
    e.manual(-1, "1")
    e.history[-1]["order_id"] = "exit"
    e.manual(-1, "1")
    e.history[-1]["order_id"] = "exit"
    entries = {item["order_id"]: {"side": item["side"], "price": item["price"],
               "target": item["exit_target"]} for item in e.intents}
    exits = {"exit": {"side": "YES", "paired": True, "target": "0.62",
                      "allocations": [{"fill_id": "f0001", "quantity": "2"}]}}
    buckets, _ = fill_cost_inventory(e.history, entries, exits, e.held, "T")
    assert buckets == {D("0.52"): D(3)}
    e.manual(-1, "1")
    e.history[-1]["order_id"] = "exit"
    with pytest.raises(ValueError, match="exceeds its attributable"):
        fill_cost_inventory(e.history, entries, exits, e.held, "T")


def test_groups_with_same_rounded_target_keep_all_allocations(tmp_path):
    e = PairExchange(bid="0.62", liquidity="6")
    buy(e, "0.57", "0.57")  # +5c
    buy(e, "0.70", "0.56")  # +6c
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert e.submissions[0]["count"] == "10" and e.held == 4
    e.bid = D("0.20")
    m.run_once()
    assert m.healthy and e.submissions[-1]["count"] == "4"
    assert e.submissions[-1]["price"] == "0.6200"


def test_failed_receipt_save_cannot_submit_cost_exit(tmp_path, monkeypatch):
    e = PairExchange(bid="0.99")
    buy(e, "0.57", "0.57")
    m, _ = monitor(tmp_path, e)
    monkeypatch.setattr(m, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    m.run_once()
    assert not m.healthy and not e.submissions
