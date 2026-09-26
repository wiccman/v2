"""Read-only status recovery; never place live orders."""
import json
from decimal import Decimal
import pytest
from kalshi import KalshiClient, KalshiAPIError
from test_take_profit_monitor import Exchange, monitor


def test_routed_lookup_sends_ticker(monkeypatch):
    client = KalshiClient()
    calls = []
    def request(method, path, **kw):
        calls.append((method, path, kw))
        return {'order': {'order_id': 'O', 'ticker': 'T', 'status': 'executed'}}
    monkeypatch.setattr(client, 'request', request)
    assert client.order('O', 'T')['status'] == 'executed'
    assert calls == [('GET', '/portfolio/orders/O', {'params': {'market_ticker': 'T', 'exchange_index': -1}, 'auth': True})]


def test_404_recovers_exact_order_on_later_page(monkeypatch):
    client = KalshiClient()
    def request(method, path, params=None, **kw):
        assert method == 'GET'
        if path.endswith('/O'):
            assert params == {'market_ticker': 'T', 'exchange_index': -1}
            raise KalshiAPIError(404, 'not found')
        assert params['ticker'] == 'T'
        assert 'exchange_index' not in params and 'market_ticker' not in params
        if not params.get('cursor'):
            return {'orders': [{'order_id': 'other', 'ticker': 'T'}], 'cursor': 'next'}
        return {'orders': [{'order_id': 'O', 'ticker': 'T', 'status': 'canceled', 'fill_count_fp': '2'}]}
    monkeypatch.setattr(client, 'request', request)
    assert client.order('O', 'T')['fill_count_fp'] == '2'

@pytest.mark.parametrize('rows', [[], [{'order_id':'O','ticker':'WRONG'}], [{'order_id':'OTHER','ticker':'T'}]])
def test_missing_or_wrong_identity_does_not_clear_404(monkeypatch, rows):
    client = KalshiClient()
    def request(*a, **k):
        raise KalshiAPIError(404, 'not found')
    monkeypatch.setattr(client, 'request', request)
    monkeypatch.setattr(client, 'all_orders', lambda ticker: rows)
    with pytest.raises(KalshiAPIError):
        client.order('O', 'T')

@pytest.mark.parametrize('code', [401,403,429,500])
def test_other_errors_are_not_hidden(monkeypatch, code):
    client = KalshiClient()
    monkeypatch.setattr(client, 'request', lambda *a,**k: (_ for _ in ()).throw(KalshiAPIError(code, 'failure')))
    monkeypatch.setattr(client, 'all_orders', lambda t: pytest.fail('Unexpected fallback'))
    with pytest.raises(KalshiAPIError) as caught:
        client.order('O', 'T')
    assert caught.value.status_code == code


def test_pending_exit_survives_404_and_restart_without_duplicate_sell(tmp_path, monkeypatch):
    exchange = Exchange(quantity='2', bid='.45')
    svc, entries, clock, events = monitor(tmp_path, exchange)
    svc.run_once()
    assert len(exchange.submissions) == 1
    lookup = exchange.order
    seen = []
    def missing(order_id, ticker=None):
        seen.append(ticker)
        raise KalshiAPIError(404, 'not found')
    monkeypatch.setattr(exchange, 'order', missing)
    svc.run_once()
    saved = json.loads(svc.path.read_text())['markets']['T']['pending']
    assert saved['order_id'] and len(exchange.submissions) == 1 and not svc.healthy
    restarted, _, restart_clock, _ = monitor(tmp_path, exchange)
    restarted.run_once()
    assert len(exchange.submissions) == 1
    monkeypatch.setattr(exchange, 'order', lookup)
    restart_clock[0] += 60
    restarted.run_once()
    assert 'pending' not in json.loads(svc.path.read_text())['markets']['T']
    assert len(exchange.submissions) == 1 and restarted.healthy
    assert all(t == 'T' for t in seen)


@pytest.mark.parametrize('visibility', ['404', 'missing_ack', 'nonterminal'])
@pytest.mark.parametrize('sign', [1, -1])
def test_unresolved_exit_retries_are_persisted_and_never_duplicate(tmp_path, monkeypatch, visibility, sign):
    exchange = Exchange(quantity=str(sign * 2), bid='.45', liquidity='.75')
    exchange.lose_ack = visibility == 'missing_ack'
    svc, entries, clock, events = monitor(tmp_path, exchange)
    svc.run_once()
    exchange.lose_ack = False
    order, history = exchange.order, exchange.all_orders
    reads = []
    def unresolved(order_id, ticker=None):
        reads.append(clock[0])
        if visibility == '404':
            raise KalshiAPIError(404, 'not found')
        return dict(order(order_id, ticker), status='resting')
    def absent(ticker):
        reads.append(clock[0])
        return []
    monkeypatch.setattr(exchange, 'order', unresolved)
    if visibility == 'missing_ack':
        monkeypatch.setattr(exchange, 'all_orders', absent)
    svc.run_once()
    saved = json.loads(svc.path.read_text())['markets']['T']['pending']
    assert saved['next_status_at'] == clock[0] + 2
    assert saved['client_id'] == exchange.submissions[0]['client_order_id']
    assert len(reads) == 1 and not svc.healthy
    restarted, _, restart_clock, restart_events = monitor(tmp_path, exchange)
    restarted.run_once()
    restart_clock[0] += 1
    restarted.run_once()
    assert len(reads) == 1 and len(exchange.submissions) == 1
    restart_clock[0] += 1
    clock[0] = restart_clock[0]
    restarted.run_once()
    pending = restarted.state['markets']['T']['pending']
    assert pending['next_status_at'] == restart_clock[0] + 4
    assert len(reads) == 2 and len(exchange.submissions) == 1
    assert any(event == 'TP_STATUS_PENDING' for event, _ in restart_events)
    assert not any(event == 'TP_ERROR' for event, _ in restart_events)
    monkeypatch.setattr(exchange, 'order', order)
    monkeypatch.setattr(exchange, 'all_orders', history)
    exchange.liquidity = Decimal(100)
    restart_clock[0] += 4
    restarted.run_once()
    restarted.run_once()
    assert exchange.held == 0 and restarted.healthy
    assert [body['count'] for body in exchange.submissions] == ['2', '1.25']
    assert all(body['reduce_only'] for body in exchange.submissions)


def test_market_close_does_not_erase_unresolved_exit_or_retry_sell(tmp_path, monkeypatch):
    exchange = Exchange()
    svc, _, clock, _ = monitor(tmp_path, exchange)
    svc.run_once()
    def missing(*args):
        raise KalshiAPIError(404, 'not found')
    monkeypatch.setattr(exchange, 'order', missing)
    svc.run_once()
    pending = dict(svc.state['markets']['T']['pending'])
    clock[0] = 1900
    svc.run_once()
    assert svc.state['markets']['T']['pending'] == pending
    assert len(exchange.submissions) == 1


@pytest.mark.parametrize('status', [None, 'resting'])
def test_order_history_uses_ticker_filter_without_negative_shard(monkeypatch, status):
    """Replay GetOrders validation observed in production on September 26."""
    client = KalshiClient()
    calls = []
    def request(method, path, params=None, **kwargs):
        assert (method, path) == ('GET', '/portfolio/orders')
        calls.append(dict(params))
        if params.get('exchange_index', 0) < 0:
            raise KalshiAPIError(400, 'GetOrdersParams.ExchangeIndex failed on gte')
        return {'orders': [{'order_id': 'O', 'ticker': 'T', 'exchange_index': 2}]}
    monkeypatch.setattr(client, 'request', request)
    assert client.all_orders('T', status)[0]['order_id'] == 'O'
    expected = {'limit': 100, 'ticker': 'T'}
    if status is not None:
        expected['status'] = status
    assert calls == [expected]


def test_unfiltered_order_history_keeps_all_shards(monkeypatch):
    client = KalshiClient()
    def request(method, path, params=None, **kwargs):
        assert (method, path, params) == ('GET', '/portfolio/orders', {'limit': 100})
        return {'orders': []}
    monkeypatch.setattr(client, 'request', request)
    assert client.all_orders(None) == []


@pytest.mark.parametrize('sign', [1, -1])
@pytest.mark.parametrize('lost_ack', [False, True])
def test_exit_recovery_uses_valid_history_query_after_restart(tmp_path, monkeypatch, sign, lost_ack):
    exchange = Exchange(quantity=str(sign * 2), bid='.45', liquidity='.75')
    exchange.lose_ack = lost_ack
    svc, _, _, _ = monitor(tmp_path, exchange)
    svc.run_once()
    assert len(exchange.submissions) == 1
    assert exchange.held == sign * Decimal('1.25')
    exchange.lose_ack = False

    # Exercise the real client lookup/fallback, including an order-status 404
    # and the production list endpoint's nonnegative exchange-index constraint.
    post = exchange.request
    reads = []
    def request(method, path, params=None, body=None, auth=False):
        if method == 'POST':
            return post(method, path, params, body, auth)
        assert method == 'GET'
        reads.append((path, dict(params)))
        if path.startswith('/portfolio/orders/'):
            raise KalshiAPIError(404, 'Read model has not exposed single order yet')
        assert path == '/portfolio/orders'
        if params.get('exchange_index', 0) < 0:
            raise KalshiAPIError(400, 'GetOrdersParams.ExchangeIndex failed on gte')
        assert params['ticker'] == 'T'
        return {'orders': [dict(order, ticker='T', exchange_index=2)
                           for order in exchange.remote.values()]}
    monkeypatch.setattr(exchange, 'request', request)
    monkeypatch.setattr(exchange, 'order', KalshiClient.order.__get__(exchange))
    monkeypatch.setattr(exchange, 'all_orders', KalshiClient.all_orders.__get__(exchange))
    exchange.liquidity = 100
    restarted, _, clock, _ = monitor(tmp_path, exchange)
    clock[0] += 60
    restarted.run_once()
    restarted.run_once()
    assert restarted.healthy
    assert exchange.held == 0
    assert [body['count'] for body in exchange.submissions] == ['2', '1.25']
    assert all(body['reduce_only'] and body['time_in_force'] == 'immediate_or_cancel'
               for body in exchange.submissions)
    assert any(path == '/portfolio/orders' for path, _ in reads)
    assert 'pending' not in json.loads(svc.path.read_text())['markets']['T']
