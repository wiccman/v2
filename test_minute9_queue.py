"""Offline queue fallback tests; no credentials, sockets or real orders."""
from decimal import Decimal as D
import pytest
import single_coin_minute9 as mod
from test_single_coin_minute9 import START, market, PreviewCoinClient


class QueueExchange(PreviewCoinClient):
    def __init__(self, outcomes=None, initial=None, fresh=None):
        super().__init__()
        self.outcomes = outcomes or {}
        self.initial, self.fresh = initial or {}, fresh or {}
        self.orders, self.fills_by_ticker, self.held = {}, {}, {}
        self.after_post = None

    def markets(self, *, series_ticker, status, limit):
        return [market(yes=self.initial.get(series_ticker, '.75'), no='.20', series=series_ticker)]

    def market(self, ticker):
        series = ticker.split('-')[0]
        m = market(yes=self.fresh.get(series, self.initial.get(series, '.75')),
                   no='.20', series=series)
        m.update(yes_bid_dollars='.74', no_bid_dollars='.19')
        return m

    def all_fills(self, ticker):
        return list(self.fills_by_ticker.get(ticker, []))

    def positions(self, ticker):
        q = self.held.get(ticker, D(0))
        return [{'ticker': ticker, 'position_fp': str(q), 'subaccount_number': 0}] if q else []

    def all_orders(self, ticker, status=None):
        return [r for r in self.orders.values() if r['ticker'] == ticker
                and (status is None or r['status'] == status)]

    def order(self, order_id, ticker):
        return dict(self.orders[order_id])

    def place_entry(self, ticker, side, quantity, limit, expiry, **kwargs):
        self.posted.append((ticker, side, quantity, limit))
        series = ticker.split('-')[0]
        outcome = self.outcomes.get(series, 'pending')
        if outcome == 'timeout':
            raise TimeoutError('uncertain acceptance')
        if outcome == 'api_error':
            from kalshi import KalshiAPIError
            raise KalshiAPIError(400, 'private raw response not to be logged', code='insufficient_balance')
        oid = 'order-' + str(len(self.posted))
        filled = D(1) if outcome in {'partial', 'fill_lag'} else D(0)
        row = {'order_id': oid, 'client_order_id': kwargs['client_order_id'], 'ticker': ticker,
               'status': 'pending' if outcome == 'pending' else 'canceled',
               'fill_count_fp': str(filled), 'remaining_count_fp': '0', 'subaccount_number': 0}
        if outcome == 'missing_remaining':
            row.pop('remaining_count_fp')
        if outcome == 'remaining_positive':
            row['remaining_count_fp'] = '1'
        if outcome == 'wrong_client':
            row['client_order_id'] = 'foreign-client'
        self.orders[oid] = row
        if outcome in {'partial', 'fill_disagrees'}:
            self.fills_by_ticker[ticker] = [{'order_id': oid, 'ticker': ticker, 'count_fp': '1',
                                           'yes_price_dollars': str(limit), 'no_price_dollars': str(1-limit),
                                           'fee_cost': '.01', 'ts': START+550}]
            self.held[ticker] = D(1)
        if self.after_post:
            self.after_post()
        return {'order_id': oid}

    def place_take_profit(self, *args, **kwargs):
        pytest.fail('This test must not route any real or simulated exits')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    events = []
    now = [START + 550]
    monkeypatch.setattr(mod, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(mod.time, 'time', lambda: now[0])
    j = mod.Journal(tmp_path/'queue.json')
    return j, events, now


def run(client, journal, now, live=True):
    mod.run_pass(client, journal, client.series, D(15), 100, live, now[0])


@pytest.mark.parametrize('first', ['.92', '.99', '.58', '.5899'])
def test_initially_priced_out_first_advances_to_second(setup, first):
    j, events, now = setup
    c = QueueExchange(initial={'KXBTC15M': first})
    run(c, j, now)
    assert len(c.posted) == 1 and c.posted[0][0].startswith('KXETH15M-')
    assert c.posted[0][2] == 6  # $5 starting allocation / (75c + 3c reserve)
    assert any(n == 'M9_QUEUE_SKIP' and d['series'] == 'KXBTC15M' for n,d in events)


@pytest.mark.parametrize('fresh', ['.92', '.99', '.58'])
def test_price_changes_before_submission_advances_to_second(setup, fresh):
    j, events, now = setup
    c = QueueExchange(fresh={'KXBTC15M': fresh})
    run(c, j, now)
    assert len(c.posted) == 1 and c.posted[0][0].startswith('KXETH15M-')
    assert any(n == 'M9_QUEUE_ADVANCE' and d['reason'] == 'fresh_quote_not_eligible' for n,d in events)


def test_terminal_zero_fill_releases_backup_same_window(setup):
    j, events, now = setup
    c = QueueExchange({'KXBTC15M': 'zero'})
    run(c, j, now)
    assert [p[0].split('-')[0] for p in c.posted] == ['KXBTC15M','KXETH15M']
    assert mod.window_entry_count(j, START*1000) == 1
    btc = j.data['windows'][c.posted[0][0]]
    assert mod.entry_slot_released(btc)
    assert btc['outcome']['counted'] is True
    assert j.data['streak'] == mod.starting_streak()
    assert any(n == 'M9_QUEUE_ADVANCE' and d['reason'] == 'confirmed_zero_fill' for n,d in events)
    # Journal survives restart, and ETH's unresolved attempt continues blocking SOL.
    run(c, mod.Journal(j.path), now)
    assert len(c.posted) == 2


def test_all_misses_are_bounded_one_attempt_per_coin(setup):
    j, _, now = setup
    c = QueueExchange({s: 'zero' for s in ('KXBTC15M','KXETH15M','KXSOL15M')})
    run(c, j, now)
    assert len(c.posted) == 3 and mod.window_entry_count(j, START*1000) == 0
    run(c, mod.Journal(j.path), now)
    assert len(c.posted) == 3


@pytest.mark.parametrize('outcome', ['pending','partial','fill_lag','fill_disagrees',
                                    'missing_remaining','remaining_positive','wrong_client'])
def test_unproven_miss_or_partial_fill_never_advances_queue(setup, outcome):
    j, _, now = setup
    c = QueueExchange({'KXBTC15M': outcome})
    run(c, j, now)
    assert len(c.posted) == 1
    assert mod.window_entry_count(mod.Journal(j.path), START*1000) == 1
    assert not mod.entry_slot_released(j.data['windows'][c.posted[0][0]])


@pytest.mark.parametrize('failure', ['timeout','api_error'])
def test_errors_leave_durable_slot_and_do_not_try_backup(setup, failure):
    j, events, now = setup
    c = QueueExchange({'KXBTC15M': failure})
    with pytest.raises(Exception):
        run(c, j, now)
    assert len(c.posted) == 1 and mod.window_entry_count(mod.Journal(j.path), START*1000) == 1
    run(c, mod.Journal(j.path), now)
    assert len(c.posted) == 1
    err = next(d for n,d in events if n == 'M9_BUY_SUBMISSION_ERROR')
    assert 'private raw' not in str(err)
    if failure == 'api_error':
        assert err['http_status'] == 400 and err['api_code'] == 'insufficient_balance'


def test_expired_minute_nine_does_not_buy_backup(setup):
    j, _, now = setup
    c = QueueExchange({'KXBTC15M': 'zero'})
    c.after_post = lambda: now.__setitem__(0, START+600)
    run(c, j, now)
    assert len(c.posted) == 1 and mod.window_entry_count(j, START*1000) == 0


def test_preview_does_not_persist_or_submit_any_order(setup):
    j, events, now = setup
    c = QueueExchange(initial={'KXBTC15M': '.98'})
    before = j.path.read_text()
    run(c, j, now, live=False)
    assert not c.posted and j.path.read_text() == before
    signals = [d for n,d in events if n == 'M9_DRY_RUN_SIGNAL']
    assert len(signals) == 1 and signals[0]['series'] == 'KXETH15M'


def test_unverified_legacy_no_fill_does_not_free_slot(setup):
    j, _, _ = setup
    record = {'buy': {'client_id':'x','order_id':'y'}, 'window_start_ms':START*1000,
              'outcome': {'status':'no_fill'}, 'sells':[]}
    j.data['windows']['legacy'] = record
    assert mod.window_entry_count(j, START*1000) == 1
    record['entry_zero_fill_verified'] = {'client_id':'x','order_id':'y','status':'canceled',
                                          'filled':'0','remaining':'0'}
    assert mod.window_entry_count(j, START*1000) == 0
    record['entry_zero_fill_verified']['order_id'] = 'other-order'
    assert mod.window_entry_count(j, START*1000) == 1


def test_full_budget_after_three_wins_and_fixed_stop_unchanged(setup):
    j, _, now = setup
    c = QueueExchange({'KXBTC15M': 'zero'})
    for _ in range(3):
        j.data['streak'] = mod.next_streak(j.data['streak'], 'win')
    run(c, j, now)
    assert len(c.posted) == 2
    assert all(p[2] == 19 for p in c.posted)  # $15 after three wins; 75c+3c reserve
    assert all(D(r['allocated_dollars']) == 15 for r in j.data['windows'].values())
    assert mod.STOP_BID == D('.45') and mod.PROFIT == D('.05')
