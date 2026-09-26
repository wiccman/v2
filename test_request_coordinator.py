"""Offline request scheduling tests: HTTP is always replaced with a fake."""
import json
import threading
from decimal import Decimal as D
from email.utils import formatdate

import pytest
import requests

import bot
import kalshi
from kalshi import KalshiAPIError, KalshiClient
from request_coordinator import RequestCoordinator, RequestDeferred
from test_five_minute_exits import cycle_setup
from test_take_profit_monitor import Exchange, monitor


def response(status=200, payload=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload or {}).encode()
    if retry_after is not None:
        result.headers['Retry-After'] = retry_after
    return result


def workers(monkeypatch, clock):
    gate = RequestCoordinator(clock=lambda: clock[0], wall_clock=lambda: clock[0])
    clients = {role: KalshiClient(coordinator=gate, role=role, clock=lambda: clock[0])
               for role in ('exit', 'entry', 'diagnostics')}
    for client in clients.values():
        monkeypatch.setattr(client, '_headers', lambda *a: {})
    return gate, clients


@pytest.mark.parametrize('source', ['exit', 'entry', 'diagnostics'])
def test_one_429_stops_all_workers_and_exits_resume_first(monkeypatch, source):
    clock, calls = [1000.0], []
    gate, clients = workers(monkeypatch, clock)
    def transport(method, url, **kwargs):
        calls.append((method, url))
        return response(429 if len(calls) == 1 else 200)
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    with pytest.raises(KalshiAPIError) as caught:
        clients[source].request('GET', '/test')
    assert caught.value.retry_after == 2
    for client in clients.values():
        with pytest.raises(RequestDeferred):
            client.request('GET', '/test')
        with pytest.raises(RequestDeferred):
            client.place_take_profit('T', D(2), D('.55'))
    assert len(calls) == 1  # No implicit GET/POST retries or queued writes.
    clock[0] += 2
    clients['exit'].positions('T')
    for role in ('entry', 'diagnostics'):
        with pytest.raises(RequestDeferred):
            clients[role].request('GET', '/test')
    clock[0] += 1
    clients['entry'].request('GET', '/test')
    with pytest.raises(RequestDeferred):
        clients['diagnostics'].request('GET', '/test')
    clock[0] += 1
    clients['diagnostics'].request('GET', '/test')
    assert len(calls) == 4


@pytest.mark.parametrize('header,delay', [
    ('30', 30), (formatdate(1040, usegmt=True), 40),
    (None, 2), ('invalid', 2), ('NaN', 2), ('inf', 2), ('-10', 2),
])
def test_retry_after_seconds_dates_and_bad_headers(monkeypatch, header, delay):
    clock = [1000.0]
    gate, clients = workers(monkeypatch, clock)
    monkeypatch.setattr(kalshi.requests, 'request', lambda *a, **k: response(429, retry_after=header))
    with pytest.raises(KalshiAPIError) as caught:
        clients['entry'].request('GET', '/test')
    assert caught.value.retry_after == delay
    clock[0] += delay - .01
    with pytest.raises(RequestDeferred):
        gate.check('exit')
    clock[0] += .01
    gate.check('exit')


def test_repeated_limits_back_off_without_success_clearing_other_workers(monkeypatch):
    clock = [1000.0]
    gate, clients = workers(monkeypatch, clock)
    monkeypatch.setattr(kalshi.requests, 'request', lambda *a, **k: response())
    for expected in (2, 4, 8, 16, 30, 30):
        assert gate.limited() == expected
        clock[0] += expected
        clients['exit'].request('GET', '/test')
    clock[0] += 60
    assert gate.limited() == 2


def test_slow_diagnostics_does_not_hold_exit_request_lock(monkeypatch):
    gate, clients = workers(monkeypatch, [1000.0])
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def transport(method, url, **kwargs):
        if url.endswith('/diagnostic'):
            entered.set()
            if not release.wait(2):
                failures.append('diagnostics not released')
        else:
            exited.set()
        return response()
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    diagnostic = threading.Thread(target=lambda: clients['diagnostics'].request('GET', '/diagnostic'))
    diagnostic.start()
    try:
        assert entered.wait(1)
        clients['exit'].positions('T')
        assert exited.is_set() and diagnostic.is_alive()
    finally:
        release.set()
        diagnostic.join(timeout=2)
    assert not failures


def test_concurrent_limits_cannot_shorten_longer_retry_after(monkeypatch):
    clock = [1000.0]
    gate, clients = workers(monkeypatch, clock)
    dispatched = threading.Barrier(2)
    errors = []
    def transport(method, url, **kwargs):
        dispatched.wait(timeout=2)  # Both requests were already in flight.
        return response(429, retry_after='60' if url.endswith('/long') else '30')
    def read(client, path):
        try:
            client.request('GET', path)
        except Exception as error:
            errors.append(error)
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    threads = [threading.Thread(target=read, args=(clients['entry'], '/long')),
               threading.Thread(target=read, args=(clients['diagnostics'], '/short'))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert len(errors) == 2 and all(isinstance(error, KalshiAPIError) for error in errors)
    clock[0] += 59
    with pytest.raises(RequestDeferred):
        gate.check('exit')
    clock[0] += 1
    gate.check('exit')


@pytest.mark.parametrize('failure', [requests.Timeout('ACK unknown'), 429])
def test_order_post_is_never_automatically_replayed(monkeypatch, failure):
    gate, clients = workers(monkeypatch, [1000.0])
    calls = []
    def transport(method, url, **kwargs):
        calls.append(method)
        if isinstance(failure, Exception):
            raise failure
        return response(failure)
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    with pytest.raises(requests.Timeout if isinstance(failure, Exception) else KalshiAPIError):
        clients['exit'].place_take_profit('T', D(2), D('.55'))
    assert calls == ['POST']


def test_quote_reuse_is_short_lived_ticker_scoped_and_invalidated_by_post(monkeypatch):
    clock, calls = [1000.0], []
    gate, clients = workers(monkeypatch, clock)
    client = clients['entry']
    def transport(method, url, **kwargs):
        calls.append((method, url))
        return response(payload={'market': {'ticker': url.rsplit('/', 1)[-1], 'yes_ask_dollars': '.53'}})
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    client.market('T')['yes_ask_dollars'] = '.99'  # Callers cannot edit cached data.
    clock[0] += .49
    assert client.market('T')['yes_ask_dollars'] == '.53'
    assert len(calls) == 1
    assert client.market('U')['ticker'] == 'U'
    clock[0] += .02
    client.market('T')
    assert len(calls) == 3
    client.request('POST', '/portfolio/events/orders')
    client.market('T')
    assert len(calls) == 5
    gate.limited()
    with pytest.raises(RequestDeferred):
        client.market('T')  # A cached quote cannot bypass another worker's 429.


def test_slow_or_failed_quote_read_cannot_extend_cached_freshness(monkeypatch):
    clock, calls = [1000.0], []
    _, clients = workers(monkeypatch, clock)
    def transport(*args, **kwargs):
        calls.append(clock[0])
        clock[0] += .6
        if len(calls) == 2:
            raise requests.Timeout('quote unavailable')
        return response(payload={'market': {'yes_ask_dollars': '.45'}})
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    clients['entry'].market('T')
    with pytest.raises(requests.Timeout):
        clients['entry'].market('T')
    clients['entry'].market('T')
    assert len(calls) == 3


def test_adjacent_tier_checks_reuse_quote_but_cash_remains_fresh(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    gate, clients = workers(monkeypatch, clock)
    calls = []
    def transport(method, url, **kwargs):
        calls.append(url)
        if '/markets/' in url:
            return response(payload={'market': {'exchange_index': 2, 'yes_ask_dollars': '.72', 'yes_bid_dollars': '.71'}})
        assert url.endswith('/portfolio/balance') and kwargs['params'] == {'exchange_index': 2}
        return response(payload={'balance_dollars': '100'})
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    monkeypatch.setattr(bot, 'client', clients['entry'])
    list(bot.paired_entries(record, state, 'TEST', 'YES', closed, 'regular'))
    assert sum('/markets/' in url for url in calls) == 1
    assert sum('/portfolio/balance' in url for url in calls) == len(bot.ENTRY_EXIT_PAIRS)
    assert not record['entry_intents']


@pytest.mark.parametrize('outcome', ['deferred', 'timeout', '429'])
def test_only_proven_local_no_send_releases_new_entry_reservation(monkeypatch, outcome):
    _, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    accepted = {'order_id': 'prior', 'entry_closed': True, 'price': '.45',
                'reserved_dollars': '2.40', 'side': 'YES'}
    record['entry_intents'].append(dict(accepted))
    gate, clients = workers(monkeypatch, clock)
    posts = []
    def transport(method, url, **kwargs):
        if method == 'POST':
            posts.append(kwargs['json'])
            if outcome == 'timeout':
                raise requests.Timeout('possibly accepted')
            return response(429)
        if '/markets/' in url:
            return response(payload={'market': {'exchange_index': 2, 'yes_ask_dollars': '.45', 'yes_bid_dollars': '.44'}})
        return response(payload={'balance_dollars': '100'})
    def saved(state):
        if outcome == 'deferred' and len(record['entry_intents']) > 1:
            gate.limited()
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    monkeypatch.setattr(bot, 'client', clients['entry'])
    monkeypatch.setattr(bot, 'save_state', saved)
    if outcome == 'deferred':
        assert bot.funded_entry(record, state, 'TEST', 'YES', D('.45'), closed, 'regular') == ({}, 0)
        assert not posts
    else:
        with pytest.raises(requests.Timeout if outcome == 'timeout' else KalshiAPIError):
            bot.funded_entry(record, state, 'TEST', 'YES', D('.45'), closed, 'regular')
        assert len(posts) == 1
    restored = json.loads(json.dumps(state))['markets']['TEST']['entry_intents']
    assert restored[0] == accepted
    intent = restored[-1]
    assert D(intent['reserved_dollars']) == (0 if outcome == 'deferred' else D('2.40'))
    assert (intent.get('release_reason') == 'request_deferred') == (outcome == 'deferred')


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_slow_funding_refreshes_quote_and_blocks_buy_after_drop(monkeypatch, side):
    _, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record['signal']['prediction'] = side
    _, clients = workers(monkeypatch, clock)
    quote_reads = []
    def transport(method, url, **kwargs):
        assert method == 'GET'
        if '/markets/' in url:
            quote_reads.append(clock[0])
            ask = '.45' if len(quote_reads) == 1 else '.44'
            return response(payload={'market': {'exchange_index': 2,
                'yes_ask_dollars': ask, 'yes_bid_dollars': '.43',
                'no_ask_dollars': ask, 'no_bid_dollars': '.43'}})
        clock[0] += .6
        return response(payload={'balance_dollars': '100'})
    monkeypatch.setattr(kalshi.requests, 'request', transport)
    monkeypatch.setattr(bot, 'client', clients['entry'])
    assert bot.funded_entry(record, state, 'TEST', side, D('.45'), closed, 'regular') == ({}, 0)
    assert len(quote_reads) == 2 and not record['entry_intents']


def test_deferred_entry_is_not_queued_past_its_deadline(monkeypatch):
    _, record, state, clock, closed = cycle_setup(monkeypatch, 299)
    gate, clients = workers(monkeypatch, clock)
    monkeypatch.setattr(kalshi.requests, 'request', lambda *a, **k: pytest.fail('Expired entry sent'))
    gate.limited('30')
    with pytest.raises(RequestDeferred):
        clients['entry'].place_entry('T', 'YES', D(5), D('.45'), clock[0] + 1, ioc=True)
    clock[0] += 31
    assert clients['entry'].place_entry('T', 'YES', D(5), D('.45'), clock[0] - 30, ioc=True) == {}


def test_exit_local_deferral_removes_only_unsent_intent_and_retries_later(tmp_path, monkeypatch):
    exchange = Exchange()
    svc, _, clock, _ = monitor(tmp_path, exchange)
    gate, clients = workers(monkeypatch, clock)
    place = exchange.place_take_profit
    monkeypatch.setattr(exchange, 'place_take_profit', clients['exit'].place_take_profit)
    monkeypatch.setattr(kalshi.requests, 'request', lambda *a, **k: pytest.fail('Deferred exit sent'))
    gate.limited('30')
    svc.run_once()
    assert 'pending' not in json.loads(svc.path.read_text())['markets']['T']
    assert not svc.healthy and not exchange.submissions
    monkeypatch.setattr(exchange, 'place_take_profit', place)
    clock[0] += 29
    svc.run_once()
    assert not exchange.submissions
    clock[0] += 1
    svc.run_once()
    assert len(exchange.submissions) == 1
