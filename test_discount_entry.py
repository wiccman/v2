"""New 35c entries are retired; existing fills still have their 42c exits."""
from decimal import Decimal as D

import pytest

import bot
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('elapsed', [0, 179.999, 180, 359.999, 360, 480, 719])
def test_35_cent_tier_never_opens_new_inventory(monkeypatch, side, elapsed):
    e, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    e.held = D('0')
    monkeypatch.setattr(e, 'btc_reference_price', lambda: D('100010') if side == 'YES' else D('99990'))
    record['signal']['prediction'] = side
    market = e.market('TEST')
    market[side.lower() + '_ask_dollars'] = '.35'
    monkeypatch.setattr(e, 'market', lambda ticker: market)
    bot.funded_entry(record, state, 'TEST', side, D('.35'), closed, 'regular',
                     submit_before=closed.timestamp(), cancel_at=closed.timestamp())
    assert not e.entries and not record['entry_intents']
    assert D('.35') not in bot.NEW_ENTRY_EXIT_PAIRS


@pytest.mark.parametrize('price,ask', [('.35', '.34'), ('.35', '.36'), ('.35', 'NaN'),
                                     ('.45', '.35'), ('.53', '.35'), ('.39', '.39')])
def test_discount_exception_does_not_lower_other_entry_floors(monkeypatch, price, ask):
    e, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    market = e.market('TEST')
    market['yes_ask_dollars'] = ask
    monkeypatch.setattr(e, 'market', lambda ticker: market)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed, 'regular') == ({}, 0)
    assert not e.entries and not record['entry_intents']


@pytest.mark.parametrize('sign', [1, -1])
def test_35_cent_fills_retain_42_cent_target_across_restart(tmp_path, sign):
    e = PairExchange(bid='.41')
    e.buy('.39', '2', sign)
    e.intents[0].update(price='.35', exit_target='.42')
    state = {'markets': {'T': {'close_timestamp': 1900, 'entry_intents': e.intents}}}
    monitor = TakeProfitMonitor(e, lambda: state, tmp_path / 'discount.json',
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: 1300, emit=lambda *a, **k: None)
    monitor.run_once()
    assert abs(e.held) == 2
    e.bid = D('.42')
    restarted = TakeProfitMonitor(e, lambda: state, monitor.path,
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: 1400, emit=lambda *a, **k: None)
    restarted.run_once()
    restarted.run_once()
    assert e.held == 0 and restarted.healthy
    assert all(b['reduce_only'] and b['price'] == ('0.4200' if sign == 1 else '0.5800') for b in e.submissions)
