"""Offline regressions for the late routes. Never connects to Kalshi."""
import copy
import json
from decimal import Decimal as D

import pytest
import requests

import bot
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network calls are forbidden in these tests")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def setup(monkeypatch, elapsed=720, favorite="YES"):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    monkeypatch.setattr(bot, "LATE_PROBABILITY_ENABLED", True)
    monkeypatch.setattr(bot, "BUDGET", D("0.77"))
    monkeypatch.setattr(bot, "MARKET_BUDGET", D("5"))
    base = fake.market("TEST")
    base.update(yes_ask_dollars="0.85" if favorite == "YES" else "0.15",
                no_ask_dollars="0.15" if favorite == "YES" else "0.85")
    monkeypatch.setattr(fake, "market", lambda ticker: dict(base))
    return fake, record, state, clock, closed


@pytest.mark.parametrize("favorite", ["YES", "NO"])
@pytest.mark.parametrize("elapsed,count", [(719, 0), (720, 1), (899, 1), (900, 0)])
def test_late_window_exact_sides_targets_and_expiry(monkeypatch, favorite, elapsed, count):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, favorite)
    bot.cycle(state)
    assert len(fake.entries) == count
    if not count:
        return
    intents = record["entry_intents"]
    assert [(i["side"], D(i["price"]), D(i["exit_target"])) for i in intents] == [
        (favorite, D("0.85"), D("0.92")),
    ]
    assert all(i["cancel_at"] == closed.timestamp() for i in intents)
    assert all(o[3]["expiration_time"] == closed.timestamp() for o in fake.entries)


def test_late_orders_survive_old_six_minute_sweep_and_restart(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    bot.cycle(state)
    ids = set(record["orders"])
    restored = json.loads(json.dumps(state))
    clock[0] += 1
    bot.cycle(restored)
    assert not fake.cancelled and len(fake.entries) == 1
    clock[0] = closed.timestamp() - 1
    bot.reconcile_entries(restored)
    assert not fake.cancelled
    clock[0] = closed.timestamp()
    bot.reconcile_entries(restored)
    assert set(fake.cancelled) == ids
    assert all(i["entry_closed"] for i in restored["markets"]["TEST"]["entry_intents"])
    assert restored["markets"]["TEST"]["entry_intents"][-1]["reserved_dollars"] == record["entry_intents"][-1]["reserved_dollars"]


def test_late_entry_does_not_need_strike_ruler_lookbacks(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    record["signal"] = None
    def unavailable(*args):
        raise AssertionError("Late route must not request signal history")
    monkeypatch.setattr(bot, "prior_three", unavailable)
    bot.cycle(state)
    assert len(fake.entries) == 1 and record["signal"] is None


def test_wait_for_each_price_and_preserve_triggers_across_restart(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    quote = fake.market("TEST")
    monkeypatch.setattr(fake, "market", lambda ticker: dict(quote))
    for yes, no, count in [("0.60", "0.40", 0), ("0.73", "0.27", 1),
                           ("0.74", "0.26", 1), ("0.85", "0.15", 2),
                           ("0.27", "0.73", 3), ("0.15", "0.85", 3)]:
        quote.update(yes_ask_dollars=yes, no_ask_dollars=no)
        bot.cycle(state)
        assert len(fake.entries) == count
        state = json.loads(json.dumps(state))
    intents = state["markets"]["TEST"]["entry_intents"]
    assert [(i["side"], i["price"], i["exit_target"]) for i in intents] == [
        ("YES", "0.73", "0.81"), ("YES", "0.85", "0.92"),
        ("NO", "0.73", "0.81")]


def test_jumping_over_prices_does_not_queue_retracement_orders(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    quote = dict(fake.market("TEST"), yes_ask_dollars="0.86", no_ask_dollars="0.14")
    monkeypatch.setattr(fake, "market", lambda ticker: quote)
    bot.cycle(state)
    assert not fake.entries and not record["late_price_attempts"]


def test_slow_quote_cannot_buy_after_contract_close(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 899)
    original = fake.market
    def slow_market(ticker):
        clock[0] = closed.timestamp()
        return original(ticker)
    monkeypatch.setattr(fake, "market", slow_market)
    bot.cycle(state)
    assert not fake.entries


@pytest.mark.parametrize("kind", ["late_probability", "late_dual"])
@pytest.mark.parametrize("elapsed", [0, 300, 719, 900, 901])
def test_funded_late_route_cannot_bypass_its_window(monkeypatch, kind, elapsed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed)
    price = D("0.85") if kind == "late_probability" else D("0.73")
    assert bot.funded_entry(record, state, "TEST", "YES", price, closed, kind,
                            submit_before=closed.timestamp() + 100) == ({}, 0)
    assert not fake.entries


def test_retired_historical_cannot_submit_even_through_shared_helper(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 120)
    assert bot.funded_entry(record, state, "TEST", "YES", D("0.32"), closed,
                            "historical") == ({}, 0)
    assert not fake.entries


def test_remove_pending_historical_without_cancelling_late_orders(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    bot.cycle(state)
    record["entry_intents"].append(dict(order_id="old-historical", client_id="old",
        kind="historical", price="0.32", side="YES", reserved_dollars="0.35",
        quantity="1", exit_target="0.39", cancel_at=closed.timestamp(), entry_closed=False))
    record["orders"].append("old-historical")
    bot.reconcile_entries(state)
    assert fake.cancelled == ["old-historical"]
    assert len(record["orders"]) == 1
    assert record["entry_intents"][-1]["reserved_dollars"] == "0.35"


def test_late_market_budget_is_not_reset_or_refunded(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    monkeypatch.setattr(bot, "BUDGET", D("2"))
    record["entry_intents"].append(dict(kind="regular", side="YES", price="0.32",
        reserved_dollars="4.5", entry_closed=True, order_id="prior"))
    bot.cycle(state)
    spent = sum(D(i["reserved_dollars"]) for i in record["entry_intents"])
    assert D("4.99") < spent <= 5
    bot.cycle(json.loads(json.dumps(state)))
    assert len(fake.entries) == 1


def test_unhealthy_exit_monitor_does_not_consume_late_attempt(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    bot.EXIT_MONITOR.healthy = False
    bot.cycle(state)
    assert not fake.entries and not record.get("late_probability_attempted")
    bot.EXIT_MONITOR.healthy = True
    bot.cycle(state)
    assert len(fake.entries) == 1


def test_ambiguous_submission_is_not_retried_after_restart(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch)
    saved = []
    monkeypatch.setattr(bot, "save_state", lambda s: saved.append(copy.deepcopy(s)))
    calls = []
    def lost_ack(*args, **kwargs):
        assert "late_probability" in saved[-1]["markets"]["TEST"]["late_price_attempts"]
        assert saved[-1]["markets"]["TEST"]["entry_intents"][-1]["client_id"] == kwargs["client_order_id"]
        calls.append(kwargs)
        raise TimeoutError("unknown response")
    monkeypatch.setattr(fake, "place_entry", lost_ack)
    monkeypatch.setattr(fake, "all_orders", lambda ticker: [])
    with pytest.raises(TimeoutError):
        bot.cycle(state)
    bot.cycle(json.loads(json.dumps(state)))
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "1", "-0.1"])
def test_invalid_late_quotes_block_submission(monkeypatch, value):
    fake, record, state, clock, closed = setup(monkeypatch)
    with pytest.raises(ValueError):
        bot.place_late_probability_entries(record, state, "TEST",
            {"yes_ask_dollars": value, "no_ask_dollars": "0.15"}, closed, 720)
    assert not fake.entries and not record.get("late_probability_attempted")


@pytest.mark.parametrize("price,target", [("0.85", "0.92"), ("0.73", "0.81")])
@pytest.mark.parametrize("sign", [1, -1])
def test_late_targets_reach_existing_ioc_monitor(tmp_path, price, target, sign):
    exchange = PairExchange(bid=target)
    exchange.buy("0.32", "1", sign)
    exchange.intents[0].update(price=price, exit_target=target)
    entries = {"markets": {"T": {"close_timestamp": 1900, "entry_intents": exchange.intents}}}
    monitor = TakeProfitMonitor(exchange, lambda: entries, tmp_path / "tp.json",
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: 1800, emit=lambda *a, **k: None)
    monitor.run_once()
    monitor.run_once()
    assert monitor.healthy and exchange.held == 0
    order = exchange.submissions[0]
    assert D(order["price"]) == (D(target) if sign == 1 else 1 - D(target))
    assert order["reduce_only"] is True
    assert order["time_in_force"] == "immediate_or_cancel"
