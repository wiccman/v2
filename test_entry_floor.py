"""Offline tests for the submission-time floor; exchange improvement is possible."""
from decimal import Decimal as D
import copy
import pytest
import bot
from test_five_minute_exits import cycle_setup

@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('kind', ['regular', 'opening_bias', 'dual', 'historical', 'spot', 'late_bias', bot.SETTLEMENT_KIND])
@pytest.mark.parametrize('ask', ['0.44', '0.21', 'NaN', '0', '1'])
def test_all_routes_block_below_floor_and_invalid_quotes(monkeypatch, side, kind, ask):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 780 if kind == bot.SETTLEMENT_KIND else 60)
    record['signal']['prediction'] = side
    market = fake.market('TEST')
    market[side.lower() + '_ask_dollars'] = ask
    monkeypatch.setattr(fake, 'market', lambda t: market)
    price = D('.97') if kind == bot.SETTLEMENT_KIND else D('.52')
    result = bot.funded_entry(record, state, 'TEST', side, price, closed, kind, submit_before=closed.timestamp())
    assert result == ({}, 0)
    assert not fake.entries and not record['entry_intents']

@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_boundary_45_uses_ioc_with_existing_target(monkeypatch, side):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    record['signal']['prediction'] = side
    market = fake.market('TEST'); market[side.lower() + '_ask_dollars'] = '.45'
    monkeypatch.setattr(fake, 'market', lambda t: market)
    result, qty = bot.funded_entry(record, state, 'TEST', side, D('.45'), closed, 'regular')
    assert result['order_id'] and qty == 5
    assert fake.entries[0][3]['ioc'] is True
    assert record['entry_intents'][0]['exit_target'] == '0.55'
    assert record['entry_intents'][0]['reserved_dollars'] == '2.40'


def test_read_failure_and_price_above_limit_do_not_spend(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.45'), closed, 'regular') == ({}, 0)
    monkeypatch.setattr(fake, 'market', lambda t: (_ for _ in ()).throw(TimeoutError()))
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.52'), closed, 'regular') == ({}, 0)
    assert not record['entry_intents'] and not fake.entries


def test_old_buy_is_cancelled_and_reservation_preserved(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    old = dict(order_id='old', client_id='old', side='YES', price='.45', quantity='5',
               reserved_dollars='2.40', cancel_at=closed.timestamp(), kind='regular', entry_closed=False)
    record['entry_intents'] = [old]
    record['orders'] = ['old']
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.52'), closed, 'regular') == ({}, 0)
    bot.reconcile_entries(state)
    assert fake.cancelled == ['old']
    assert old['entry_closed'] and old['reserved_dollars'] == '2.40'
    assert fake.held == 16 and not fake.exits


def test_lower_configured_limit_blocked(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.39'), closed, 'regular') == ({}, 0)
    assert not fake.entries
