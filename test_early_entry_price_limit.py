"""Offline tests for the eight-minute entry floor on 70c-plus orders."""
from decimal import Decimal as D

import pytest

import bot
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('kind', ['opening_bias', 'regular', 'dual', 'historical', 'spot', 'late_bias'])
@pytest.mark.parametrize('price', ['.70', '.73', '.85'])
def test_every_route_blocks_70_or_higher_before_eight_minutes(monkeypatch, side, kind, price):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    record['signal']['prediction'] = side
    # A cheaper current ask cannot bypass the ceiling on the submitted limit.
    market = dict(fake.market('TEST'))
    market[side.lower() + '_ask_dollars'] = '.45'
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    monkeypatch.setattr(fake, 'market_cash', lambda ticker: pytest.fail('Blocked tier reached funding'))
    result = bot.funded_entry(record, state, 'TEST', side, D(price), closed, kind)
    assert result == ({}, 0) and not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('elapsed,expected', [(0, 0), (180, 0), (479.999, 0), (480, 1), (481, 1), (720, 0)])
def test_70_cent_tier_opens_at_exactly_eight_minutes(monkeypatch, side, elapsed, expected):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    fake.held = D('0')
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D('100010') if side == 'YES' else D('99990'))
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    record['signal']['prediction'] = side
    market = dict(fake.market('TEST'))
    market[side.lower() + '_ask_dollars'] = '.70'
    market[('no' if side == 'YES' else 'yes') + '_ask_dollars'] = '.30'
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    bot.funded_entry(record, state, 'TEST', side, D('.70'), closed, 'regular')
    assert len(fake.entries) == len(record['entry_intents']) == expected
    if expected:
        wire, quantity, price, kwargs = fake.entries[0]
        assert quantity == 5 and kwargs['ioc'] is True
        assert price == (D('.70') if side == 'YES' else D('.30'))
        assert D(record['entry_intents'][0]['exit_target']) == D('.76')
        assert D(record['entry_intents'][0]['reserved_dollars']) == D('3.65')


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('price,kind', [('.52', 'opening_bias'), ('.59', 'regular')])
def test_lower_tiers_remain_available_early(monkeypatch, side, price, kind):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    fake.held = D('0')
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D('100010') if side == 'YES' else D('99990'))
    record['signal']['prediction'] = side
    market = dict(fake.market('TEST'))
    market[side.lower() + '_ask_dollars'] = price
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    result, quantity = bot.funded_entry(record, state, 'TEST', side, D(price), closed, kind)
    assert result['order_id'] and quantity == 5 and len(fake.entries) == 1


def test_future_caller_timestamp_cannot_bypass_early_ceiling(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    result = bot.funded_entry(record, state, 'TEST', 'YES', D('.70'), closed,
                              'regular', now_timestamp=clock[0] + 480)
    assert result == ({}, 0) and not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('elapsed,expected', [(359.999, 1), (360, 0), (479, 0), (480, 0), (719, 0)])
def test_under_70_cent_entries_end_at_six_minutes(monkeypatch, elapsed, expected):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    bot.funded_entry(record, state, 'TEST', 'YES', D('.67'), closed, 'regular',
                     submit_before=closed.timestamp(), cancel_at=closed.timestamp())
    assert len(fake.entries) == expected
    if expected:
        assert fake.entries[0][3]['expiration_time'] == closed.timestamp() - 540


@pytest.mark.parametrize('elapsed,expected', [(479.999, 0), (480, 1), (660, 1)])
@pytest.mark.parametrize('price', ['.70', '.73', '.85'])
def test_high_price_gateway_respects_eight_minute_boundary(monkeypatch, elapsed, expected, price):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    fake.held = D('0')
    market = {**fake.market('TEST'), 'yes_ask_dollars': price, 'no_ask_dollars': '.20'}
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed, 'late_bias',
                     submit_before=closed.timestamp(), cancel_at=closed.timestamp())
    assert len(fake.entries) == expected


def test_slow_funding_cannot_cross_six_minute_cutoff(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 359)
    def slow(ticker):
        clock[0] += 2
        return {'exchange_index': 2, 'cash_dollars': '100'}
    monkeypatch.setattr(fake, 'market_cash', slow)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.67'), closed, 'regular') == ({}, 0)
    assert not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('sign', [1, -1])
def test_early_entry_ceiling_does_not_block_existing_70_cent_exits(tmp_path, sign):
    exchange = PairExchange(bid='.80')
    exchange.buy('.39', '2', sign)
    exchange.intents[0].update(price='.70', exit_target='.80')
    state = {'markets': {'T': {'close_timestamp': 1900, 'entry_intents': exchange.intents}}}
    monitor = TakeProfitMonitor(exchange, lambda: state, tmp_path / 'early-exit.json',
        pairs={D('.70'): D('.80')}, clock=lambda: 1060, emit=lambda *a, **k: None)
    monitor.run_once()
    monitor.run_once()
    assert exchange.held == 0 and monitor.healthy
    assert len(exchange.submissions) == 1
    assert exchange.submissions[0]['reduce_only'] is True
    assert exchange.submissions[0]['price'] == ('0.8000' if sign == 1 else '0.2000')
