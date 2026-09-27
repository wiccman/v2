"""Offline regression cases for missed entries, sizing, and terminal refunds."""
import copy
import json
from decimal import Decimal as D

import pytest

import bot
import entry_policy as policy
from test_combined_entry_rules import setup


def exchange(monkeypatch, elapsed=660, ask=".85", side="YES"):
    e, record, state, clock, closed = setup(monkeypatch, elapsed, ask, side)
    record["close_timestamp"] = closed.timestamp()
    record["entry_cancel_at"] = closed.timestamp() - 180
    e.remote, e.fill_ioc = {}, True
    original = e._order
    def place(ticker, book_side, quantity, price, **kwargs):
        result = original(ticker, book_side, quantity, price, **kwargs)
        filled = quantity if kwargs.get("ioc") and e.fill_ioc else D(0)
        e.remote[result["order_id"]] = {"order_id": result["order_id"], "ticker": ticker,
            "client_order_id": kwargs["client_order_id"], "fill_count_fp": str(filled),
            "maker_fees_dollars": "0", "taker_fees_dollars": "0",
            "status": ("executed" if filled else "canceled") if kwargs.get("ioc") else "resting"}
        e.held += filled if book_side == "bid" else -filled
        return result
    def cancel(oid, ticker):
        e.cancelled.append(oid)
        e.remote[oid]["status"] = "canceled"
        return {"order_id": oid, "status": "canceled"}
    monkeypatch.setattr(e, "_order", place)
    monkeypatch.setattr(e, "order", lambda oid, ticker=None: copy.deepcopy(e.remote[oid]))
    monkeypatch.setattr(e, "all_orders", lambda ticker: copy.deepcopy(list(e.remote.values())))
    monkeypatch.setattr(e, "cancel", cancel)
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, *a, **data: events.append((event, data)))
    return e, record, state, clock, closed, events


@pytest.mark.parametrize("price", [".45", ".52", ".57", ".59", ".62", ".67", ".70", ".73", ".75", ".85"])
def test_scalps_plus_six_contract_settlement_fit_twenty_five(price):
    record = {}
    # Caller cap cannot expand the configured cap.
    while policy.reserve(record, "YES", D(price), D("100"), D(25), 720, "regular"):
        pass
    scalp_spend = sum(D(i["reserved_dollars"]) for i in record["entry_intents"])
    assert 0 < scalp_spend <= 19
    assert all(1 <= D(i["quantity"]) <= 5 and D(i["reserved_dollars"]) <= D("2.80")
               for i in record["entry_intents"])
    final = policy.reserve(record, "YES", D(".97"), D(100), D(25), 900, policy.SETTLEMENT_KIND)
    assert final["quantity"] == "6"
    assert scalp_spend + D(final["reserved_dollars"]) <= 25


def test_live_loop_limits_pending_inventory_even_with_spare_market_budget(monkeypatch):
    e, record, state, clock, closed, events = exchange(monkeypatch, 301, ".67")
    for _ in range(6):
        bot.cycle(state)
        clock[0] += 8
    assert [order[1] for order in e.entries] == [D(4)]
    bot.reconcile_entries(state)
    assert sum(D(i["reserved_dollars"]) for i in record["entry_intents"]) == D("2.80")
    e.held = D(0)
    bot.cycle(state)
    assert len(e.entries) <= 2
    clock[0] = closed.timestamp() - 120
    e.market("TEST")["yes_ask_dollars"] = ".972"
    bot.settlement_entry(record, state, "TEST", closed)
    assert e.entries[-1][1] <= 6
    assert sum(D(i["reserved_dollars"]) for i in record["entry_intents"]) <= D("21")


@pytest.mark.parametrize("filled,retained", [("0", "0"), ("1.25", "0.975"), ("3", "2.34")])
def test_terminal_unfilled_quantity_releases_once_and_preserves_filled_spend(monkeypatch, filled, retained):
    e, record, state, clock, closed, events = exchange(monkeypatch, 365, ".78")
    result, qty = bot.funded_entry(record, state, "TEST", "YES", D(".75"), closed, "regular")
    item = record["entry_intents"][0]
    e.remote[result["order_id"]].update(status="canceled", fill_count_fp=filled)
    bot.reconcile_entries(state)
    assert D(item["reserved_dollars"]) == D(retained)
    assert item["reservation_reconciled"] and item["entry_closed"]
    first = copy.deepcopy(item)
    restored = json.loads(json.dumps(state))
    bot.reconcile_entries(restored)
    assert restored["markets"]["TEST"]["entry_intents"][0] == first


def test_upgrade_reclaims_old_closed_zero_fill_reservation(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 660)
    item = {"order_id": "old", "client_id": "old-client", "kind": "regular", "side": "YES",
            "price": ".59", "quantity": "5", "reserved_dollars": "3.10", "entry_closed": True,
            "cancel_at": closed.timestamp() - 540, "entry_execution_version": 5}
    record["entry_intents"].append(item)
    e.remote["old"] = {"order_id": "old", "status": "canceled", "fill_count_fp": "0"}
    bot.reconcile_entries(state)
    assert D(item["reserved_dollars"]) == 0 and D(item["reservation_released_dollars"]) == D("3.10")


@pytest.mark.parametrize("count", [None, "NaN", "-1", "4"])
def test_unproven_terminal_fill_count_never_refunds(monkeypatch, count):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 365, ".78")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".75"), closed, "regular")
    remote = e.remote[result["order_id"]]
    remote["status"] = "canceled"
    if count is None:
        remote.pop("fill_count_fp")
    else:
        remote["fill_count_fp"] = count
    bot.reconcile_entries(state)
    assert D(record["entry_intents"][0]["reserved_dollars"]) == D("2.34")
    assert policy.attempt_committed(record["entry_intents"][0])


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("price,ask,elapsed,deadline", [(".75", ".78", 365, 720), (".97", ".972", 725, 900)])
def test_resting_limits_survive_next_loop_but_expire_at_correct_deadline(monkeypatch, side, price, ask, elapsed, deadline):
    e, record, state, clock, closed, _ = exchange(monkeypatch, elapsed, ask, side)
    kind = policy.SETTLEMENT_KIND if price == ".97" else "regular"
    result, qty = bot.funded_entry(record, state, "TEST", side, D(price), closed, kind,
        cancel_at=closed.timestamp() if kind == policy.SETTLEMENT_KIND else None)
    assert result.get("order_id") and not e.entries[-1][3].get("ioc", False)
    assert e.entries[-1][2] == (D(price) if side == "YES" else 1 - D(price))
    record["orders"].append(result["order_id"])
    clock[0] += 5
    bot.reconcile_entries(state)
    assert not e.cancelled and not record["entry_intents"][0]["entry_closed"]
    assert not bot.funded_entry(record, state, "TEST", side, D(price), closed, kind)[0]
    clock[0] = closed.timestamp() - 900 + deadline
    bot.reconcile_entries(state)
    assert e.cancelled == [result["order_id"]]
    assert D(record["entry_intents"][0]["reserved_dollars"]) == 0


def test_resting_scalp_is_canceled_on_live_side_change(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 365, ".78")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".75"), closed, "regular")
    monkeypatch.setattr(e, "btc_reference_price", lambda: D("99990"))
    bot.reconcile_entries(state)
    assert e.cancelled == [result["order_id"]]
    assert D(record["entry_intents"][0]["reserved_dollars"]) == 0


def test_late_quote_wait_retries_each_tier_without_duplicating_accepted_order(monkeypatch):
    e, record, state, clock, closed, events = exchange(monkeypatch, 660, ".95")
    monkeypatch.setattr(bot, "MAX_BUYS", 0)
    record["late_entry_attempted"] = True  # Migrate the old premature flag.
    bot.cycle(state)
    assert not e.entries
    e.market("TEST")["yes_ask_dollars"] = ".85"
    clock[0] += 5
    bot.cycle(state)
    assert len(e.entries) == 1 and e.entries[-1][2] == D(".85")
    e.market("TEST")["yes_ask_dollars"] = ".73"
    clock[0] += 5
    bot.cycle(state)
    assert len(e.entries) == 1  # Minute-11 averaging is closed.
    bot.cycle(state)
    assert len(e.entries) == 1


def test_zero_fill_late_order_retries_after_terminal_reconciliation(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch)
    monkeypatch.setattr(bot, "MAX_BUYS", 0)
    e.fill_ioc = False
    bot.cycle(state)
    assert len(e.entries) == 1
    clock[0] += 5
    e.fill_ioc = True
    bot.cycle(state)
    assert len(e.entries) == 2 and D(record["entry_intents"][0]["reserved_dollars"]) == 0
    bot.cycle(state)
    assert len(e.entries) == 2


def test_opening_quote_wait_retries_even_after_legacy_attempt_flag(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 60, ".53")
    monkeypatch.setattr(bot, "MAX_BUYS", 0)
    record["opening_bias_attempted"] = True
    bot.cycle(state)
    assert not e.entries
    e.market("TEST")["yes_ask_dollars"] = ".52"
    bot.cycle(state)
    assert len(e.entries) == 1
    bot.cycle(state)
    assert len(e.entries) == 1


def test_lost_late_ack_is_recovered_without_second_submission(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch)
    monkeypatch.setattr(bot, "MAX_BUYS", 0)
    place = e._order
    def lost(*args, **kwargs):
        place(*args, **kwargs)
        raise TimeoutError("ack lost")
    monkeypatch.setattr(e, "_order", lost)
    with pytest.raises(TimeoutError):
        bot.cycle(state)
    restored = json.loads(json.dumps(state))
    monkeypatch.setattr(e, "_order", place)
    bot.cycle(restored)
    assert len(e.entries) == 1
    assert restored["markets"]["TEST"]["entry_intents"][0]["reservation_reconciled"]


def test_skip_log_explains_quote_and_budget_blockers(monkeypatch):
    e, record, state, clock, closed, events = exchange(monkeypatch, 480, ".78")
    bot.funded_entry(record, state, "TEST", "YES", D(".70"), closed, "regular")
    skips = [json.loads(d["details"]) for event, d in events if event == "ENTRY_SKIP"]
    assert skips[-1]["reason"] == "ask_above_limit" and D(skips[-1]["ask"]) == D(".78")
    record["entry_intents"] = [{"reserved_dollars": "19", "entry_closed": True}]
    bot.funded_entry(record, state, "TEST", "YES", D(".70"), closed, "regular")
    skips = [json.loads(d["details"]) for event, d in events if event == "ENTRY_SKIP"]
    assert skips[-1]["reason"] == "market_allowance_unavailable"
    assert D(skips[-1]["remaining_dollars"]) == 0


def test_zero_fill_attempt_does_not_consume_regular_order_limit(monkeypatch):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 301, ".67")
    monkeypatch.setattr(bot, "MAX_BUYS", 1)
    record["buys"] = 100  # Old attempts are not proof of filled orders.
    e.fill_ioc = False
    bot.cycle(state)
    assert len(e.entries) == 1
    clock[0] += 8
    e.fill_ioc = True
    bot.cycle(state)
    assert len(e.entries) == 2 and bot.committed_regular_orders(record) == 1
    clock[0] += 8
    bot.cycle(state)
    assert len(e.entries) == 2


@pytest.mark.parametrize("fees,remaining", [(None, "2.34"), ("0.02", "0.0275"), ("2.40", "2.4075")])
def test_fractional_fill_refund_keeps_verified_rounded_fees(monkeypatch, fees, remaining):
    e, record, state, clock, closed, _ = exchange(monkeypatch, 365, ".78")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".75"), closed, "regular")
    remote = e.remote[result["order_id"]]
    remote.update(status="canceled", fill_count_fp="0.01")
    if fees is None:
        remote.pop("taker_fees_dollars")
    else:
        remote["taker_fees_dollars"] = fees
    bot.reconcile_entries(state)
    assert D(record["entry_intents"][0]["reserved_dollars"]) == D(remaining)
