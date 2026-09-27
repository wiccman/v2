"""Offline checks for the combined opening, minute-six, and $20 rules."""
import copy
import json
import os
import subprocess
import sys
from decimal import Decimal as D

import pytest

import bot
import entry_policy as policy
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


def setup(monkeypatch, elapsed, price, side='YES'):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    fake.held = D('0')
    monkeypatch.setattr(bot, 'START', 0)
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    monkeypatch.setattr(bot, 'DUAL_LIMIT_BUYS_ENABLED', False)
    monkeypatch.setattr(bot, 'HISTORICAL_STRIKE_ENABLED', False)
    fake.bias_side = side
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D('100010' if side == 'YES' else '99990'))
    market = dict(fake.market('TEST'))
    market[side.lower() + '_ask_dollars'] = price
    market[('no' if side == 'YES' else 'yes') + '_ask_dollars'] = str(1 - D(price))
    monkeypatch.setattr(fake, 'market', lambda ticker: market)
    return fake, record, state, clock, closed


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('elapsed,allowed', [(-1, False), (0, False), (59.999, False), (60, True), (119.999, True), (120, False), (180, False)])
def test_new_opening_tier_uses_its_own_two_minute_deadline(monkeypatch, side, elapsed, allowed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, '.57', side)
    record['opening_bias_attempted'] = True
    result, quantity = bot.funded_entry(record, state, 'TEST', side, D('.57'), closed, 'opening_57',
                                       submit_before=closed.timestamp(), cancel_at=closed.timestamp())
    assert bool(result.get('order_id')) == allowed
    if allowed:
        intent = record['entry_intents'][0]
        assert quantity == 4 and D(intent['exit_target']) == D('.62')
        assert intent['cancel_at'] == closed.timestamp() - 780
        assert fake.entries[0][3]['ioc'] is True


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('elapsed,allowed', [(359.999, False), (360, True), (479.999, True), (480, True), (719.999, True), (720, False)])
def test_75_cent_tier_runs_from_six_until_twelve_minutes(monkeypatch, side, elapsed, allowed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, '.75', side)
    bot.cycle(state)
    assert bool(fake.entries) == allowed
    if allowed:
        matching = [i for i in record['entry_intents'] if D(i['price']) == D('.75')]
        assert len(matching) == 1
        intent = matching[0]
        assert intent['side'] == side and D(intent['price']) == D('.75')
        assert D(intent['exit_target']) == D('.83') and D(intent['quantity']) == 3
        assert D(intent['reserved_dollars']) == D('2.34')
        wire_price = D('.75') if side == 'YES' else D('.25')
        assert all(not order[3].get('ioc', False) for order in fake.entries if order[2] == wire_price)


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_full_cycle_opening_57_is_independent_of_consumed_52_attempt(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 60, '.57', side)
    record['opening_bias_attempted'] = True
    monkeypatch.setattr(bot, 'MAX_BUYS', 0)
    bot.cycle(state)
    assert len(fake.entries) == 1
    intent = record['entry_intents'][0]
    assert intent['kind'] == 'opening_57' and intent['side'] == side
    assert D(intent['exit_target']) == D('.62')
    restored = copy.deepcopy(record)
    assert bot.funded_entry(restored, {'markets': {'TEST': restored}}, 'TEST', side, D('.57'), closed, 'opening_57') == ({}, 0)
    assert len(fake.entries) == 1


@pytest.mark.parametrize('price,elapsed,ask', [('.57', 60, '.52'), ('.57', 60, '.5701'),
                                             ('.75', 360, '.70'), ('.75', 360, '.7499')])
def test_new_rules_wait_for_their_specified_quote_without_spending(monkeypatch, price, elapsed, ask):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ask)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed, 'regular') == ({}, 0)
    assert not fake.entries and not record['entry_intents']


def test_opening_57_retries_quote_wait_but_not_lost_acknowledgement(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 60, '.58')
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.57'), closed, 'opening_57') == ({}, 0)
    fake.market('TEST')['yes_ask_dollars'] = '.57'
    def timeout(*args, **kwargs):
        raise TimeoutError('lost acknowledgement')
    monkeypatch.setattr(fake, 'place_entry', timeout)
    with pytest.raises(TimeoutError):
        bot.funded_entry(record, state, 'TEST', 'YES', D('.57'), closed, 'opening_57')
    restored = json.loads(json.dumps(record))
    assert len(restored['entry_intents']) == 1
    assert bot.funded_entry(restored, {'markets': {'TEST': restored}}, 'TEST', 'YES', D('.57'), closed, 'opening_57') == ({}, 0)


@pytest.mark.parametrize('price,elapsed,cutoff', [('.57', 119, 120), ('.75', 719, 720)])
def test_slow_funding_cannot_extend_new_entry_windows(monkeypatch, price, elapsed, cutoff):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, price)
    def slow_cash(ticker):
        clock[0] = closed.timestamp() - 900 + cutoff
        return {'exchange_index': 2, 'cash_dollars': '100'}
    monkeypatch.setattr(fake, 'market_cash', slow_cash)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D(price), closed, 'regular') == ({}, 0)
    assert not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_minute_six_keeps_live_strike_and_higher_ask_requirements(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 360, '.75', side)
    opposite = 'NO' if side == 'YES' else 'YES'
    assert bot.funded_entry(record, state, 'TEST', opposite, D('.75'), closed, 'regular') == ({}, 0)
    fake.market('TEST')[opposite.lower() + '_ask_dollars'] = '.75'
    assert bot.funded_entry(record, state, 'TEST', side, D('.75'), closed, 'regular') == ({}, 0)
    assert not fake.entries and not record['entry_intents']


def test_20_cap_preserves_saved_spending_and_six_dollar_settlement_reserve():
    record = {'entry_intents': [{'kind': 'regular', 'reserved_dollars': '9', 'entry_closed': True}]}
    before = copy.deepcopy(record['entry_intents'][0])
    assert policy.market_budget() == 20
    assert policy.reserve(record, 'YES', D('.75'), D('.77'), policy.market_budget(), 720, 'regular')
    assert policy.reserve(record, 'YES', D('.75'), D('.77'), policy.market_budget(), 720, 'regular')
    assert policy.reserve(record, 'YES', D('.75'), D('.77'), policy.market_budget(), 720, 'regular') is None
    restored = json.loads(json.dumps(record))
    settlement = policy.reserve(restored, 'YES', D('.97'), D('.77'), policy.market_budget(), 900, policy.SETTLEMENT_KIND)
    assert D(settlement['quantity']) == 6
    assert restored['entry_intents'][0] == before
    assert sum(D(i['reserved_dollars']) for i in restored['entry_intents']) == D('19.68')


def test_stale_configuration_cannot_restore_35_or_regular_57():
    env = {**os.environ, 'ENTRY_EXIT_PAIRS_CENTS': '35:42,57:90,70:80,75:90', 'MARKET_BUDGET_DOLLARS': '15'}
    result = subprocess.run([sys.executable, '-c',
        "import bot; from decimal import Decimal as D; "
        "assert D('.35') not in bot.NEW_ENTRY_EXIT_PAIRS; "
        "assert D('.57') not in bot.ENTRY_EXIT_PAIRS; "
        "assert bot.NEW_ENTRY_EXIT_PAIRS[D('.57')] == D('.62'); "
        "assert bot.NEW_ENTRY_EXIT_PAIRS[D('.75')] == D('.83'); "
        "assert bot.NEW_ENTRY_EXIT_PAIRS[D('.70')] == D('.76'); "
        "assert bot.ALL_ENTRY_EXIT_PAIRS[D('.35')] == D('.42'); "
        "assert bot.MARKET_BUDGET == 20"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('price,target', [('.57', '.62'), ('.75', '.83'), ('.70', '.76')])
@pytest.mark.parametrize('sign', [1, -1])
def test_new_and_minute_eight_targets_sell_only_when_reached(tmp_path, price, target, sign):
    fake = PairExchange(bid=str(D(target) - D('.01')))
    fake.buy('.39', '5', sign)
    fake.intents[0].update(price=price, exit_target=target)
    state = {'markets': {'T': {'close_timestamp': 1900, 'entry_intents': fake.intents}}}
    monitor = TakeProfitMonitor(fake, lambda: state, tmp_path / 'receipt.json',
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: 1500, emit=lambda *a, **k: None)
    monitor.run_once()
    assert abs(fake.held) == 5  # IOC target orders may be sent, but cannot fill below target.
    fake.bid = D(target)
    monitor.run_once()
    monitor.run_once()
    assert fake.held == 0 and monitor.healthy
    wire_target = D(target) if sign == 1 else 1 - D(target)
    assert all(order['reduce_only'] and D(order['price']) == wire_target for order in fake.submissions)
