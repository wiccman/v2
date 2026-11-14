"""Retired entry tiers and old deadlines must not break inventory recovery."""
from decimal import Decimal as D

import pytest

import bot
from test_combined_entry_rules import setup
from test_fill_cost_targets import buy, monitor
from test_price_pairs import PairExchange


@pytest.mark.parametrize("sign", [1, -1])
def test_retired_64_cent_entry_keeps_owned_inventory_exit(tmp_path, sign):
    assert D('.64') not in bot.ENTRY_EXIT_PAIRS
    assert D('.64') not in bot.NEW_ENTRY_EXIT_PAIRS
    assert bot.ALL_ENTRY_EXIT_PAIRS[D('.64')] == D('.69')
    exchange = PairExchange(bid='.63')
    buy(exchange, '.64', '.57', sign=sign)
    worker, _ = monitor(tmp_path, exchange)
    worker.run_once()
    worker.run_once()
    assert worker.healthy and exchange.held == 0
    assert len(exchange.submissions) == 1
    assert exchange.submissions[0]['reduce_only']
    assert exchange.submissions[0]['price'] == ('0.6200' if sign > 0 else '0.3800')


@pytest.mark.parametrize("saved_cancel_offset", [360, 720, 780])
def test_recovery_uses_actual_close_instead_of_old_window_offset(monkeypatch, saved_cancel_offset):
    exchange, record, state, clock, closed = setup(monkeypatch, 720, '.75')
    record.pop('close_timestamp')
    record['entry_cancel_at'] = closed.timestamp() - 900 + saved_cancel_offset
    record['entry_intents'] = [dict(order_id='old', client_id='old', side='YES',
        price='.75', quantity='3', reserved_dollars='2.34', kind='regular',
        cancel_at=closed.timestamp(), entry_execution_version=bot.ENTRY_EXECUTION_VERSION)]
    bot.reconcile_entries(state)
    assert record['close_timestamp'] == closed.timestamp()
    assert exchange.cancelled == ['old']
    assert record['entry_intents'][0]['entry_closed']
    assert record['entry_intents'][0]['reserved_dollars'] == '2.34'
    assert not exchange.entries


def test_unavailable_close_keeps_reservation_until_recovery(monkeypatch):
    exchange, record, state, clock, closed = setup(monkeypatch, 720, '.75')
    record.pop('close_timestamp')
    record['entry_intents'] = [dict(order_id='old', client_id='old', side='YES',
        price='.75', quantity='3', reserved_dollars='2.34', kind='regular',
        cancel_at=closed.timestamp(), entry_execution_version=bot.ENTRY_EXECUTION_VERSION)]
    market = exchange.market
    monkeypatch.setattr(exchange, 'market', lambda ticker: {})
    bot.reconcile_entries(state)
    assert not record['entry_intents'][0].get('entry_closed')
    assert record['entry_intents'][0]['reserved_dollars'] == '2.34'
    assert not exchange.entries and not exchange.cancelled
    monkeypatch.setattr(exchange, 'market', market)
    bot.reconcile_entries(state)
    assert exchange.cancelled == ['old']
