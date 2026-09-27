"""Replay concurrent trading and delayed REST reads without a live account."""
import json
from decimal import Decimal as D

import pytest

import bot
from kalshi import KalshiAPIError, terminal_ioc_receipt
from test_entry_reliability import exchange as entry_exchange
from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("outside_first", [False, True])
def test_other_trader_inventory_is_excluded_from_cost_and_exit_size(tmp_path, sign, outside_first):
    e = PairExchange(bid=".65")
    if outside_first:
        e.manual(sign, "7")
    buy(e, ".57", ".51", "3", sign=sign)
    if not outside_first:
        e.manual(sign, "7")
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 7 * sign
    assert len(e.submissions) == 1
    assert e.submissions[0]["count"] == "3"
    assert e.submissions[0]["price"] == ("0.5600" if sign > 0 else "0.4400")
    restarted, _ = monitor(tmp_path, e, m.path)
    restarted.run_once()
    assert restarted.healthy and e.held == 7 * sign and len(e.submissions) == 1
    assert len([event for event, _ in events if event == "TP_OUTSIDE_INVENTORY"]) == 1


@pytest.mark.parametrize("sign", [1, -1])
def test_external_sale_reduces_bot_fifo_lots_before_next_exit(tmp_path, sign):
    e = PairExchange(bid=".65")
    buy(e, ".57", ".51", "3", sign=sign)
    e.manual(sign, "7")
    e.manual(-sign, "2")
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 7 * sign
    assert e.submissions[0]["count"] == "1"


def test_unmatched_inventory_does_not_bypass_position_consistency(tmp_path):
    e = PairExchange(bid=".65")
    buy(e, ".57", ".51", "3")
    e.manual(1, "7")
    e.held += 1  # Neither trader's history explains the current net position.
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions
    assert any("Fills and position disagree" in data.get("error", "") for _, data in events)


def test_lost_bot_ack_is_not_mistaken_for_an_outside_trade(tmp_path):
    e = PairExchange(bid=".65")
    buy(e, ".57", ".51", "3")
    del e.intents[0]["order_id"]
    orders = e.all_orders
    e.all_orders = lambda ticker: []
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions
    assert not any(event == "TP_OUTSIDE_INVENTORY" for event, _ in events)
    e.all_orders = orders
    m.run_once()
    assert m.healthy and e.held == 0


def test_unknown_price_on_known_bot_entry_is_not_classified_as_outside(tmp_path):
    e = PairExchange(bid=".65")
    buy(e, ".57", ".51", "3")
    e.intents[0]["price"] = ".54"
    m, _ = monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions


def test_entry_added_during_exchange_read_uses_refreshed_durable_intent(tmp_path):
    e = PairExchange(bid=".65")
    m, events = monitor(tmp_path, e)
    positions = e.positions
    def concurrent_entry(ticker):
        buy(e, ".57", ".51", "3")
        return positions(ticker)
    e.positions = concurrent_entry
    m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[0]["count"] == "3"
    assert not any(event == "TP_OUTSIDE_INVENTORY" for event, _ in events)


def test_transient_position_snapshot_is_retried_before_sending_exit(tmp_path):
    e = PairExchange(bid=".65")
    buy(e, ".57", ".51", "3")
    positions = e.positions
    calls = []
    def stale_once(ticker):
        calls.append(ticker)
        return [{"ticker": ticker, "position_fp": "0"}] if len(calls) == 1 else positions(ticker)
    e.positions = stale_once
    m, events = monitor(tmp_path, e)
    m.run_once()
    assert len(calls) == 2 and len(e.submissions) == 1 and m.healthy
    assert not any(event == "TP_ERROR" for event, _ in events)


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("liquidity", ["0", "1", "3"])
def test_terminal_exit_receipt_survives_restart_and_order_404(tmp_path, sign, liquidity):
    e = PairExchange(bid=".65", liquidity=liquidity)
    buy(e, ".57", ".51", "3", sign=sign)
    post = e.request
    def receipt(*args, **kwargs):
        return dict(post(*args, **kwargs), remaining_count="0.00")
    e.request = receipt
    m, _ = monitor(tmp_path, e)
    m.run_once()
    pending = json.loads(m.path.read_text())["markets"]["T"]["pending"]
    assert pending["placement_receipt"]["remaining_count"] == "0.00"
    def invisible(*args):
        raise AssertionError("A proven terminal IOC does not need an order lookup")
    e.order = e.all_orders = invisible
    e.liquidity = D(3)
    restarted, _ = monitor(tmp_path, e, m.path)
    restarted.run_once()
    restarted.run_once()
    assert restarted.healthy and e.held == 0
    assert [x["count"] for x in e.submissions] == (["3"] if liquidity == "3" else ["3", str(3-D(liquidity))])


def test_receipt_does_not_bypass_missing_exit_fill_history(tmp_path):
    e = PairExchange(bid=".65", liquidity="1")
    buy(e, ".57", ".51", "3")
    post = e.request
    e.request = lambda *a, **kw: dict(post(*a, **kw), remaining_count="0")
    m, _ = monitor(tmp_path, e)
    m.run_once()
    missing = e.history.pop()
    m.run_once()
    assert not m.healthy and len(e.submissions) == 1
    e.history.append(missing)
    m.run_once()
    assert m.healthy and e.submissions[-1]["count"] == "2"


@pytest.mark.parametrize("changes", [
    {"order_id": "wrong"}, {"client_order_id": "wrong"},
    {"fill_count": "NaN"}, {"fill_count": "-1"}, {"fill_count": "4"},
    {"remaining_count": "1"}, {"remaining_count": None}, {"fill_count": "invalid"},
])
def test_invalid_or_nonterminal_receipt_never_proves_completion(changes):
    intent = {"order_id": "O", "client_id": "C", "quantity": "3",
              "placement_receipt": {"order_id": "O", "client_order_id": "C",
                                    "fill_count": "2", "remaining_count": "0"}}
    intent["placement_receipt"].update(changes)
    assert terminal_ioc_receipt(intent) is None


@pytest.mark.parametrize("filled", ["0", "1", "3"])
def test_entry_receipt_avoids_cancel_404_and_keeps_partial_fee_reservation(monkeypatch, filled):
    e, record, state, _, closed, _ = entry_exchange(monkeypatch, 660, ".85")
    place = e.place_entry
    def receipt(*args, **kwargs):
        result = place(*args, **kwargs)
        return dict(result, client_order_id=kwargs["client_order_id"],
                    fill_count=filled, remaining_count="0")
    e.place_entry = receipt
    bot.funded_entry(record, state, "TEST", "YES", D(".85"), closed, "regular")
    restored = json.loads(json.dumps(state))
    item = restored["markets"]["TEST"]["entry_intents"][0]
    def invisible(*args):
        if filled != "1":
            raise AssertionError("Full/zero IOC needs no GET")
        raise KalshiAPIError(404, "Read model pending")
    e.order = invisible
    bot.reconcile_entries(restored)
    assert item["entry_closed"] and not e.cancelled
    assert D(item["reserved_dollars"]) == (D(0) if filled == "0" else D("2.64"))
    if filled == "1":
        assert not item.get("reservation_reconciled")
        e.order = lambda *a: {"order_id": item["order_id"], "status": "canceled",
            "fill_count_fp": "0", "maker_fees_dollars": "0", "taker_fees_dollars": "0"}
        bot.reconcile_entries(restored)
        assert D(item["reserved_dollars"]) == D("2.64")
        e.order = lambda *a: {"order_id": item["order_id"], "status": "canceled",
            "fill_count_fp": "1", "maker_fees_dollars": "0", "taker_fees_dollars": ".01"}
        bot.reconcile_entries(restored)
        assert D(item["reserved_dollars"]) == D(".88")
