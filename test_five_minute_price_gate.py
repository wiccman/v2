"""No 60c+ entry before minute five; repeat batches are not single orders."""
from decimal import Decimal as D
import pytest
import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('kind', ['regular', 'dual', 'historical', 'spot', 'opening_bias', 'late_bias'])
@pytest.mark.parametrize('price', ['.60', '.62', '.64', '.67', '.70', '.75', '.85'])
def test_every_route_blocks_high_limits_before_five_minutes(monkeypatch, side, kind, price):
    fake, record, state, clock, closed = setup(monkeypatch, 299.999, '.59', side)
    monkeypatch.setattr(fake, 'market_cash', lambda ticker: pytest.fail('Premature funding read'))
    assert bot.funded_entry(record, state, 'TEST', side, D(price), closed, kind,
                            now_timestamp=clock[0] + 600) == ({}, 0)
    assert not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('elapsed,allowed', [(0, False), (299.999, False), (300, True), (359.999, True), (360, False)])
@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_62_cent_entry_window_is_five_to_six_minutes(monkeypatch, elapsed, allowed, side):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, '.60', side)
    result, quantity = bot.funded_entry(record, state, 'TEST', side, D('.62'), closed, 'regular')
    assert bool(result.get('order_id')) == allowed
    if allowed:
        assert quantity == 4 and D(record['entry_intents'][0]['exit_target']) == D('.67')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_regular_gateway_caps_pending_inventory_and_recovers_zero_fills(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 300, '.60', side)
    first, qty = bot.funded_entry(record, state, 'TEST', side, D('.62'), closed, 'regular')
    assert first['order_id'] and qty == 4
    extra, qty = bot.funded_entry(record, state, 'TEST', side, D('.62'), closed, 'regular')
    assert extra['order_id'] and qty == 1  # Five pending contracts fill the initial slot.
    assert bot.funded_entry(record, state, 'TEST', side, D('.62'), closed, 'regular') == ({}, 0)
    monkeypatch.setattr(fake, 'order', lambda oid, ticker=None: {
        'order_id': oid, 'status': 'canceled', 'fill_count_fp': '0',
        'maker_fees_dollars': '0', 'taker_fees_dollars': '0'})
    bot.reconcile_entries(state)
    assert all(D(i['reserved_dollars']) == 0 for i in record['entry_intents'])
    second, qty = bot.funded_entry(record, state, 'TEST', side, D('.62'), closed, 'regular')
    assert second['order_id'] != first['order_id'] and qty == 4
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) == D('2.60')


def test_higher_early_limits_cannot_consume_allowance_at_contract_start(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 0, '.60')
    bot.cycle(state)
    assert not fake.entries and not record['entry_intents']


pytestmark = pytest.mark.usefixtures("early_entry_scenario")
