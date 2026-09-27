"""The live strike, rather than a saved research prediction, owns scalp entries."""
from decimal import Decimal as D

import pytest

import bot
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


@pytest.mark.parametrize('spot,side', [('100010', 'YES'), ('99990', 'NO')])
def test_live_strike_overrides_stale_signal_on_both_sides(monkeypatch, spot, side):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 190)
    fake.held = D('0')
    record['signal'] = {'build': 'old', 'prediction': 'NO' if side == 'YES' else 'YES'}
    record['trade_side'] = 'NO' if side == 'YES' else 'YES'
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D(spot))
    market = dict(fake.market('TEST'))
    market[side.lower() + '_ask_dollars'] = '.45'
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    result, qty = bot.funded_entry(record, state, 'TEST', side, D('.45'), closed, 'regular')
    assert result['order_id'] and qty == 5
    assert record['entry_intents'][-1]['side'] == side
    opposite = 'NO' if side == 'YES' else 'YES'
    assert bot.funded_entry(record, state, 'TEST', opposite, D('.45'), closed, 'regular') == ({}, D(0))


@pytest.mark.parametrize('yes,no,spot,allowed', [
    ('.70', '.30', '100010', True),
    ('.30', '.70', '99990', True),
    ('.45', '.56', '100010', False),
    ('.69', '.31', '100010', False),
    ('.70', '.70', '100010', False),
    ('.70', '.30', '100000', False),
])
def test_late_trade_requires_strike_side_higher_quoted_at_least_70(monkeypatch, yes, no, spot, allowed):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 480)
    fake.held = D('0')
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D(spot))
    market = {**fake.market('TEST'), 'yes_ask_dollars': yes, 'no_ask_dollars': no}
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    side = 'YES' if D(spot) >= 100000 else 'NO'
    result, qty = bot.funded_entry(record, state, 'TEST', side, D('.70'), closed, 'regular')
    assert bool(result.get('order_id')) == allowed
    if allowed:
        assert qty == 3 and D(record['entry_intents'][-1]['exit_target']) == D('.76')


@pytest.mark.parametrize('price,target', [('.70', '.76'), ('.73', '.79'), ('.85', '.91')])
def test_all_late_targets_are_six_cents_above_limit(monkeypatch, price, target):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 660)
    fake.held = D('0')
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    market = {**fake.market('TEST'), 'yes_ask_dollars': price, 'no_ask_dollars': '.20'}
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed, 'late_bias')[0]['order_id']
    assert D(record['entry_intents'][-1]['exit_target']) == D(target)


def test_opposite_inventory_waits_for_natural_exit_before_scalping(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 480)
    fake.held = D('-5')
    monkeypatch.setattr(bot, 'END', 720)
    market = {**fake.market('TEST'), 'yes_ask_dollars': '.70', 'no_ask_dollars': '.30'}
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.70'), closed, 'regular') == ({}, D(0))
    assert fake.entries == []


@pytest.mark.parametrize('spot,side', [('100010', 'YES'), ('99990', 'NO')])
def test_full_cycle_uses_live_strike_after_eight_minutes(monkeypatch, spot, side):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 480)
    fake.held = D('0')
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D(spot))
    record['signal'] = {'build': 'old', 'prediction': 'NO' if side == 'YES' else 'YES'}
    market = dict(fake.market('TEST'))
    market.update(yes_ask_dollars='.70' if side == 'YES' else '.30',
                  no_ask_dollars='.70' if side == 'NO' else '.30')
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    bot.cycle(state)
    assert fake.entries
    assert {intent['side'] for intent in record['entry_intents']} == {side}
    assert {D(intent['exit_target']) for intent in record['entry_intents']} == {D('.76')}


def test_existing_70_cent_lot_keeps_80_cent_exit_after_upgrade(tmp_path):
    exchange = PairExchange(bid='.80')
    exchange.buy('.39', '2', 1)
    exchange.intents[0].update(price='.70', exit_target='.80', entry_execution_version=3)
    state = {'markets': {'T': {'close_timestamp': 1900, 'entry_intents': exchange.intents}}}
    monitor = TakeProfitMonitor(exchange, lambda: state, tmp_path / 'old-lot.json',
        pairs={D('.70'): D('.76')}, clock=lambda: 1060, emit=lambda *a, **k: None)
    monitor.run_once()
    monitor.run_once()
    assert exchange.held == 0 and monitor.healthy
    assert exchange.submissions[0]['price'] == '0.8000'
