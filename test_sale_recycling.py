"""Confirmed exits refill only verified cost, and a restart cannot double count."""
import copy
import json
from decimal import Decimal as D

import bot
from entry_policy import remaining_allowance, reserve
from sale_recycling import confirmed_credits


def fixture(count="5", exit_price="0.65"):
    record = {"entry_intents": [{"order_id": "buy", "side": "YES", "kind": "regular",
        "reserved_dollars": "2.65", "confirmed_entry_filled_quantity": "5",
        "reservation_reconciled": True}]}
    fills = [dict(fill_id="entry-fill", order_id="buy", ticker="T", book_side="bid",
                  count_fp="5", yes_price_dollars="0.50", subaccount_number=0),
             dict(fill_id="exit-fill", order_id="sell", ticker="T", book_side="ask",
                  count_fp=count, yes_price_dollars=exit_price, subaccount_number=0)]
    exits = {"sell": {"side": "YES", "purpose": "take_profit", "filled": count,
                      "allocations": [{"fill_id": "entry-fill", "quantity": "5"}]}}
    return record, fills, exits


def test_full_profitable_sale_replenishes_cost_but_not_profit():
    record, fills, exits = fixture()
    record["recycled_exit_orders"] = confirmed_credits(record, exits, fills, "T")
    assert record["recycled_exit_orders"] == {"sell": "2.65"}
    assert remaining_allowance(record, D(21), "regular") == D(15)
    assert reserve(record, "YES", D("0.50"), D("2.80"), D(21), 900, "regular")


def test_losing_partial_sale_restores_only_net_proceeds():
    record, fills, exits = fixture(count="2", exit_price="0.40")
    record["recycled_exit_orders"] = confirmed_credits(record, exits, fills, "T")
    assert record["recycled_exit_orders"] == {"sell": "0.74"}
    assert remaining_allowance(record, D(21), "regular") == D("13.09")


def test_pending_sale_and_missing_fill_do_not_restore_budget():
    record, fills, exits = fixture()
    assert confirmed_credits(record, {}, fills, "T") == {}
    from price_pairs import InventorySyncError
    import pytest
    with pytest.raises(InventorySyncError):
        confirmed_credits(record, exits, fills[:1], "T")


def test_two_partial_exits_never_credit_one_entry_twice():
    record, fills, exits = fixture(count="2")
    fills.append(dict(fill_id="second-exit", order_id="sell-2", ticker="T",
                      book_side="ask", count_fp="3", yes_price_dollars="0.65"))
    exits["sell-2"] = {"side": "YES", "purpose": "take_profit", "filled": "3",
                       "allocations": [{"fill_id": "entry-fill", "quantity": "3"}]}
    assert confirmed_credits(record, exits, fills, "T") == {"sell": "1.06", "sell-2": "1.59"}
    exits["sell-2"]["filled"] = "4"
    from price_pairs import InventorySyncError
    import pytest
    with pytest.raises(InventorySyncError):
        confirmed_credits(record, exits, fills, "T")


def test_no_sale_uses_no_outcome_price():
    record, fills, exits = fixture()
    record["entry_intents"][0]["side"] = "NO"
    fills[0].update(book_side="ask", yes_price_dollars="0.50")
    fills[1].update(book_side="bid", yes_price_dollars="0.35")
    exits["sell"]["side"] = "NO"
    assert confirmed_credits(record, exits, fills, "T") == {"sell": "2.65"}


def test_reconcile_is_durable_and_idempotent(monkeypatch, tmp_path):
    record, fills, exits = fixture()
    state = {"markets": {"T": record}}
    path = tmp_path / "take_profit.json"
    path.write_text(json.dumps({"markets": {"T": {"exit_orders": exits}}}))
    class Monitor:
        pass
    monitor = Monitor(); monitor.path = path
    monkeypatch.setattr(bot, "EXIT_MONITOR", monitor)
    monkeypatch.setattr(bot.client, "all_fills", lambda ticker: copy.deepcopy(fills))
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, *args, **kwargs: events.append(event))
    bot.reconcile_sale_allowance(state, "T", record)
    assert record["recycled_exit_orders"] == {"sell": "2.65"}
    bot.reconcile_sale_allowance(state, "T", record)
    assert events.count("ENTRY_SALE_ALLOWANCE_RESTORED") == 1
    assert remaining_allowance(json.loads(json.dumps(record)), D(21), "regular") == D(15)
