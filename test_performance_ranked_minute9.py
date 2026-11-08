from dataclasses import replace
from decimal import Decimal as D

import pytest
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from test_single_coin_minute9 import START, market, PreviewCoinClient

NAMES = tuple(ranked.PRODUCTS)
NOW = START + 550


def observations():
    returns = ['.1', '.3', '.2', '-.1', '-.2']
    return [ranked.Observation(s, START, D('100'), D('100') + D(v), NOW)
            for s, v in zip(NAMES, returns)]


class Provider:
    calls = 0
    def snapshot(self, names, start):
        self.calls += 1
        return observations()


class Exchange(PreviewCoinClient):
    def __init__(self):
        super().__init__()
        self.prices = {'KXETH15M': ('.96', '.05'), 'KXSOL15M': ('.72', '.29')}
        self.checked = []
        self.fail = False

    def markets(self, *, series_ticker, status, limit):
        self.checked.append(series_ticker)
        yes, no = self.prices.get(series_ticker, ('.80', '.21'))
        return [market(yes=yes, no=no, series=series_ticker)]

    def market(self, ticker):
        s = ticker.split('-')[0]
        yes, no = self.prices.get(s, ('.80', '.21'))
        return market(yes=yes, no=no, series=s)

    def place_entry(self, *args, **kwargs):
        if self.fail:
            from kalshi import KalshiAPIError
            raise KalshiAPIError(400, 'private body not logged', code='insufficient_balance')
        return super().place_entry(*args, **kwargs)


def run(tmp_path, monkeypatch, exchange=None, provider=None, live=True):
    exchange = exchange or Exchange()
    journal = core.Journal(tmp_path / 'journal.json')
    events = []
    monkeypatch.setattr(core.time, 'time', lambda: NOW)
    ranked.run_ranked_pass(exchange, journal, NAMES, D('15'), 100, live, NOW,
        provider=provider or Provider(), clock=lambda: NOW,
        emit=lambda name, **data: events.append((name, data)))
    return exchange, journal, events


def test_ranking_is_signed_percentage_not_contract_price():
    result = ranked.ranked_observations(observations(), NAMES, START, NOW)
    assert [r.series for r in result] == ['KXETH15M','KXSOL15M','KXBTC15M','KXXRP15M','KXHYPE15M']
    rows = [replace(r, price=D('90') + D(i)) for i, r in enumerate(observations())]
    assert ranked.ranked_observations(rows, NAMES, START, NOW)[0].series == 'KXHYPE15M'


@pytest.mark.parametrize('allow_partial', [False, True])
@pytest.mark.parametrize('change', ['duplicate', 'unknown', 'old_window', 'stale', 'future', 'nan', 'zero', 'skew'])
def test_invalid_observations_are_rejected_even_with_partial_ranking(change, allow_partial):
    rows = observations()
    if change == 'duplicate': rows[-1] = rows[0]
    if change == 'unknown': rows[-1] = replace(rows[-1], series='UNSUPPORTED')
    if change == 'old_window': rows[0] = replace(rows[0], window_start=START-900)
    if change == 'stale': rows[0] = replace(rows[0], observed_at=NOW-16)
    if change == 'future': rows[0] = replace(rows[0], observed_at=NOW+2)
    if change == 'nan': rows[0] = replace(rows[0], price=D('NaN'))
    if change == 'zero': rows[0] = replace(rows[0], opening=D('0'))
    if change == 'skew': rows[0] = replace(rows[0], observed_at=NOW-11)
    with pytest.raises(ranked.PerformanceUnavailable):
        ranked.ranked_observations(rows, NAMES, START, NOW, allow_partial=allow_partial)


def test_missing_coin_requires_explicit_partial_ranking():
    rows = observations()[:-1]
    with pytest.raises(ranked.PerformanceUnavailable):
        ranked.ranked_observations(rows, NAMES, START, NOW)
    result = ranked.ranked_observations(rows, NAMES, START, NOW, allow_partial=True)
    assert [r.series for r in result] == ['KXETH15M', 'KXSOL15M', 'KXBTC15M', 'KXXRP15M']


def public_feed(monkeypatch, missing=(), *, clock=lambda: NOW):
    """Exercise candle parsing and parallel collection without any network access."""
    events, requested = [], []
    rows = {ranked.PRODUCTS[r.series]: r for r in observations()}

    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            product, resource = url.rsplit('/', 2)[-2:]
            row = rows[product]
            requested.append((row.series, resource))
            if resource == 'candles':
                # Reproduce HYPE: the next minute exists, the opening minute does not.
                ts = START + 60 if row.series in missing else START
                return Response([[ts, 99, 101, 100, 100, 1]])
            return Response({'price': str(row.price),
                'time': ranked.datetime.fromtimestamp(clock(), ranked.timezone.utc).isoformat()})

    provider = ranked.CoinbasePerformance(clock=clock, session_factory=Session,
        emit=lambda name, **data: events.append((name, data)))
    return provider, events, requested


@pytest.mark.parametrize('missing', ['KXHYPE15M', 'KXETH15M'])
def test_missing_opening_candle_skips_only_that_coin_and_buys_backup(tmp_path, monkeypatch, missing):
    provider, feed_events, _ = public_feed(monkeypatch, {missing})
    exchange, journal, events = run(tmp_path, monkeypatch, provider=provider)
    # ETH is either missing or priced out. SOL remains the next eligible coin.
    assert len(exchange.posted) == 1
    assert exchange.posted[0][0].startswith('KXSOL15M-')
    assert missing not in exchange.checked
    assert core.window_entry_count(journal, START * 1000) == 1
    skipped = [data for name, data in feed_events if name == 'M9_PERFORMANCE_COIN_SKIPPED']
    assert skipped == [{'series': missing, 'window_start_ms': START * 1000,
        'error_type': 'PerformanceUnavailable', 'reason': 'opening_candle_missing_or_ambiguous'}]
    queue = next(data for name, data in events if name == 'M9_PERFORMANCE_QUEUE')
    assert queue['excluded_series'] == [missing]
    assert queue['ranking_scope'] == 'available_coin_data'


def test_one_remaining_valid_coin_can_be_selected(tmp_path, monkeypatch):
    provider, _, _ = public_feed(monkeypatch, set(NAMES) - {'KXBTC15M'})
    exchange, _, _ = run(tmp_path, monkeypatch, provider=provider)
    assert exchange.checked == ['KXBTC15M']
    assert len(exchange.posted) == 1
    assert exchange.posted[0][0].startswith('KXBTC15M-')


def test_no_valid_coins_skips_entries_and_still_runs_exit_pass(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(core, 'apply_new_outcomes', lambda journal: order.append('exits'))
    provider, feed_events, _ = public_feed(monkeypatch, set(NAMES))
    exchange, journal, events = run(tmp_path, monkeypatch, provider=provider)
    assert order == ['exits']
    assert not exchange.posted and not exchange.checked and not journal.data['windows']
    assert len(feed_events) == len(NAMES)
    assert events[-1] == ('M9_PERFORMANCE_UNAVAILABLE', {
        'error_type': 'PerformanceUnavailable', 'reason': 'no_valid_coin_observations'})


@pytest.mark.parametrize('problem', ['timeout', 'http_error', 'stale', 'old_window', 'nan', 'zero'])
def test_single_coin_error_is_isolated(monkeypatch, problem):
    provider, events, _ = public_feed(monkeypatch)
    original = provider._one
    def one(series, start):
        if series != 'KXETH15M': return original(series, start)
        if problem == 'timeout': raise ranked.requests.Timeout('private response text')
        if problem == 'http_error': raise ranked.requests.HTTPError('private response text')
        row = observations()[1]
        if problem == 'stale': return replace(row, observed_at=NOW - 16)
        if problem == 'old_window': return replace(row, window_start=START - 900)
        if problem == 'nan': return replace(row, price=D('NaN'))
        return replace(row, opening=D('0'))
    monkeypatch.setattr(provider, '_one', one)
    rows = provider.snapshot(NAMES, START)
    assert len(rows) == 4 and rows[0].series == 'KXSOL15M'
    assert all(row.series != 'KXETH15M' for row in rows)
    assert events[0][1]['series'] == 'KXETH15M'
    assert 'private response text' not in str(events)


def test_partial_cache_expires_and_recovered_coin_rejoins(monkeypatch):
    missing, now = {'KXETH15M'}, [NOW]
    provider, _, requested = public_feed(monkeypatch, missing, clock=lambda: now[0])
    assert provider.snapshot(NAMES, START)[0].series == 'KXSOL15M'
    calls = len(requested)
    missing.clear()
    now[0] += 2
    assert provider.snapshot(NAMES, START)[0].series == 'KXSOL15M'
    assert len(requested) == calls
    now[0] += 2
    assert provider.snapshot(NAMES, START)[0].series == 'KXETH15M'
    assert len(requested) > calls


def test_priced_out_leader_falls_to_second_without_consuming_slot(tmp_path, monkeypatch):
    exchange, journal, events = run(tmp_path, monkeypatch)
    assert exchange.checked[:2] == ['KXETH15M', 'KXSOL15M']
    assert len(exchange.posted) == 1
    assert exchange.posted[0][0].startswith('KXSOL15M-')
    assert exchange.posted[0][2] == 20
    assert core.window_entry_count(journal, START*1000) == 1
    skipped = [data for name, data in events if name == 'M9_QUEUE_SKIPPED']
    assert skipped[0]['rank'] == 1 and skipped[0]['yes_ask'] == '.96'


def test_two_priced_out_coins_fall_to_third(tmp_path, monkeypatch):
    exchange = Exchange()
    exchange.prices['KXSOL15M'] = ('.51', '.50')
    exchange, _, _ = run(tmp_path, monkeypatch, exchange=exchange)
    assert exchange.posted[0][0].startswith('KXBTC15M-')


def test_eligible_first_place_wins_even_if_second_has_higher_odds(tmp_path, monkeypatch):
    exchange = Exchange()
    exchange.prices['KXETH15M'] = ('.69', '.32')
    exchange.prices['KXSOL15M'] = ('.90', '.11')
    exchange, _, _ = run(tmp_path, monkeypatch, exchange=exchange)
    assert exchange.posted[0][0].startswith('KXETH15M-')


def test_no_side_stays_supported(tmp_path, monkeypatch):
    exchange = Exchange()
    exchange.prices['KXSOL15M'] = ('.25', '.76')
    exchange, _, _ = run(tmp_path, monkeypatch, exchange=exchange)
    assert exchange.posted[0][1] == 'NO'


def test_preview_never_submits(tmp_path, monkeypatch):
    exchange, journal, _ = run(tmp_path, monkeypatch, live=False)
    assert not exchange.posted and not journal.data['windows']


def test_missing_data_blocks_new_entries_but_exit_pass_runs_first(tmp_path, monkeypatch):
    events = []
    class Broken:
        def snapshot(self, *args):
            assert events == ['exits']
            raise ranked.PerformanceUnavailable('incomplete_coin_snapshot')
    monkeypatch.setattr(core, 'apply_new_outcomes', lambda journal: events.append('exits'))
    exchange, _, logged = run(tmp_path, monkeypatch, provider=Broken())
    assert not exchange.posted and not exchange.checked
    assert logged[-1][0] == 'M9_PERFORMANCE_UNAVAILABLE'


def test_error_codes_are_recorded_without_raw_response_bodies(tmp_path, monkeypatch):
    exchange = Exchange()
    exchange.fail = True
    journal = core.Journal(tmp_path/'failure.json')
    events=[]
    monkeypatch.setattr(core.time, 'time', lambda: NOW)
    with pytest.raises(Exception):
        ranked.run_ranked_pass(exchange, journal, NAMES, D('15'),100,True,NOW,
            provider=Provider(),clock=lambda: NOW,
            emit=lambda name,**data: events.append((name,data)))
    failure = events[-1][1]
    assert failure['http_status'] == 400
    assert failure['exchange_error_code'] == 'insufficient_balance'
    assert 'private body' not in str(events)
    assert len(journal.data['windows']) == 1


def test_no_network_outside_minute_nine_or_when_slot_already_used(tmp_path):
    p = Provider()
    journal = core.Journal(tmp_path/'skip.json')
    exchange = Exchange()
    for now in [START+539, START+600]:
        ranked.run_ranked_pass(exchange,journal,NAMES,D('15'),100,False,now,
                              provider=p,clock=lambda: now)
    assert p.calls == 0 and not exchange.checked
    journal.data['windows']['pending'] = {'window_start_ms': START*1000, 'buy': {'client_id': 'x'}}
    ranked.run_ranked_pass(exchange,journal,NAMES,D('15'),100,False,NOW,
                          provider=p,clock=lambda: NOW)
    assert p.calls == 0
