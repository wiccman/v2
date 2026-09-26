"""Offline funding and cancellation regressions; no live account writes."""
import copy
from decimal import Decimal as D

import pytest

import bot
from balance_diagnostics import balance_report
from kalshi import KalshiAPIError, KalshiClient
from test_five_minute_exits import cycle_setup


@pytest.mark.parametrize('index', [0, 2, 3])
def test_market_cash_uses_metadata_shard_and_read_only_scoped_balance(index):
    client = KalshiClient()
    calls = []
    def read(method, path, params=None, **kwargs):
        calls.append((method, path, params))
        if path == '/markets/T':
            return {'market': {'exchange_index': index}}
        assert path == '/portfolio/balance' and params == {'exchange_index': index}
        return {'balance_dollars': '1.8615', 'balance': 186}
    client.request = read
    report = client.market_cash('T')
    assert report['exchange_index'] == index and D(report['cash_dollars']) == D('1.8615')
    assert [c[0] for c in calls] == ['GET', 'GET']


@pytest.mark.parametrize('index', [None, -1, '2', True])
def test_unknown_market_shard_never_defaults_to_zero(index):
    client = KalshiClient()
    client.market = lambda ticker: {'exchange_index': index}
    client.balance = lambda **kw: pytest.fail('No balance read for unknown shard')
    with pytest.raises(ValueError, match='exchange_index'):
        client.market_cash('T')


def test_aggregate_cash_is_not_crypto_collateral():
    payload = {'balance_dollars': '50.0061', 'balance': 5000, 'balance_breakdown': [
        {'exchange_index': 0, 'balance': '48.1446'},
        {'exchange_index': 2, 'balance': '1.8615'}]}
    report = balance_report(payload, exchange_index=2)
    assert D(report['cash_dollars']) == D('1.8615')
    assert report['balance_fields_agree_to_cent'] is False
    assert D(balance_report(payload)['cash_dollars']) == D('50.0061')


@pytest.mark.parametrize('rows', [[], [{'exchange_index': 0, 'balance': '50'}],
    [{'exchange_index': 2, 'balance': '1'}, {'exchange_index': 2, 'balance': '2'}]])
def test_missing_or_ambiguous_shard_never_uses_total(rows):
    with pytest.raises(ValueError):
        balance_report({'balance_dollars': '50', 'balance_breakdown': rows}, exchange_index=2)


@pytest.mark.parametrize('cash,expected', [('1.8615', 0), ('2.0499', 0), ('2.05', 1)])
def test_five_contract_order_requires_shard_cash_including_fee_reserve(monkeypatch, cash, expected):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    monkeypatch.setattr(fake, 'market_cash', lambda ticker: {'exchange_index': 2, 'cash_dollars': cash})
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append((event, k)))
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    assert len(fake.entries) == expected
    assert len(record['entry_intents']) == expected
    if expected:
        assert fake.entries[0][1] == 5
    else:
        assert record['cash_retry_at'] == clock[0] + 30
        assert events[-1][0] == 'ENTRY_WAIT_MARKET_CASH'


def test_low_cash_wait_survives_restart_then_recovers_without_losing_budget(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    cash = ['1']
    reads = []
    def funding(ticker):
        reads.append(ticker)
        return {'exchange_index': 2, 'cash_dollars': cash[0]}
    monkeypatch.setattr(fake, 'market_cash', funding)
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    state = copy.deepcopy(state)
    record = state['markets']['TEST']
    cash[0] = '25'
    clock[0] += 29
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    assert reads == ['TEST'] and not fake.entries and not record['entry_intents']
    clock[0] += 1
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    assert len(fake.entries) == 1 and fake.entries[0][1] == 5


@pytest.mark.parametrize('failure', [TimeoutError('offline'), ValueError('bad cash')])
def test_failed_cash_read_blocks_new_orders_without_touching_existing_reservations(monkeypatch, failure):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    saved = copy.deepcopy(record['entry_intents'])
    def fail(ticker):
        raise failure
    monkeypatch.setattr(fake, 'market_cash', fail)
    bot.funded_entry(record, state, 'TEST', 'YES', D('.39'), closed, 'regular')
    assert record['entry_intents'] == saved and len(fake.entries) == 1
    assert bot.EXIT_MONITOR.healthy  # Funding waits do not suspend sell monitoring.


def test_slow_cash_lookup_cannot_buy_after_cutoff(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 299)
    def slow(ticker):
        clock[0] = 1000000300
        return {'exchange_index': 2, 'cash_dollars': '25'}
    monkeypatch.setattr(fake, 'market_cash', slow)
    bot.funded_entry(record, state, 'TEST', 'YES', D('.38'), closed, 'regular')
    assert not fake.entries


def test_cash_wait_does_not_consume_settlement_one_attempt(monkeypatch):
    from test_settlement_entry import setup
    fake, record, state, clock, closed = setup(monkeypatch)
    cash = ['9.99']
    monkeypatch.setattr(fake, 'market_cash', lambda ticker: {'exchange_index': 2, 'cash_dollars': cash[0]})
    bot.settlement_entry(record, state, 'TEST', closed)
    assert not fake.entries and not record['entry_intents']
    cash[0] = '10'
    clock[0] += 30
    bot.settlement_entry(record, state, 'TEST', closed)
    assert len(fake.entries) == 1 and fake.entries[0][1] == 10


@pytest.mark.parametrize('status', ['executed', 'canceled', 'expired'])
def test_already_terminal_order_does_not_send_cancel(monkeypatch, status):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    monkeypatch.setattr(fake, 'order', lambda oid: {'order_id': oid, 'status': status})
    assert bot.cancel_confirmed('done', 'TEST')
    assert not fake.cancelled


def test_cancel_404_racing_with_fill_is_reconciled_without_false_error(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    statuses = iter(['resting', 'executed'])
    monkeypatch.setattr(fake, 'order', lambda oid: {'order_id': oid, 'status': next(statuses)})
    def not_found(*args):
        raise KalshiAPIError(404, 'not found')
    monkeypatch.setattr(fake, 'cancel', not_found)
    events = []
    monkeypatch.setattr(bot, 'write_log', lambda event, *a, **k: events.append(event))
    assert bot.cancel_confirmed('race', 'TEST')
    assert events == ['ENTRY_ALREADY_TERMINAL']


def test_missing_order_and_failed_cancel_are_not_assumed_terminal(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    def not_found(*args):
        raise KalshiAPIError(404, 'not found')
    monkeypatch.setattr(fake, 'order', not_found)
    monkeypatch.setattr(fake, 'cancel', not_found)
    assert not bot.cancel_confirmed('unknown', 'TEST')


def test_failed_status_read_does_not_prevent_cancel(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    def offline(*args):
        raise TimeoutError('offline')
    monkeypatch.setattr(fake, 'order', offline)
    assert bot.cancel_confirmed('resting', 'TEST')
    assert fake.cancelled == ['resting']
