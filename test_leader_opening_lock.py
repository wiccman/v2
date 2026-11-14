"""Offline regressions: fixed minute-four selection, never diagnostic trades."""
from decimal import Decimal as D
import pytest
import leader_mean15 as leader
import performance_ranked_minute9 as ranked
from test_leader_mean15 import START, Feed, Exchange, observations


def test_eth_remains_selected_after_quote_skip_and_restart(tmp_path):
    at = [START + 240]
    path = tmp_path / 'leader.json'
    venue = Exchange()
    venue.book['no_ask_dollars'] = '1.00'
    runner = leader.LeaderRunner(path, provider=Feed(lambda: at[0]), clock=lambda: at[0])
    runner.run(venue, live=True)
    assert not venue.submissions
    assert runner.journal.data['leader_minute_four_signals'][str(START)]['coin'] == 'ETH'
    class MustNotReselect:
        def snapshot(self, *args):
            raise AssertionError('A saved signal must not be recomputed')
    at[0] += 30
    venue.book['no_ask_dollars'] = '.75'
    runner = leader.LeaderRunner(path, provider=MustNotReselect(), clock=lambda: at[0])
    runner.run(venue, live=True)
    assert len(venue.submissions) == 1
    assert venue.submissions[0][1].endswith('-ETH')
    record = runner.journal.data['windows'][str(START)]
    assert record['entry_signal']['sample_ms'] == (START + 240) * 1000
    assert record['entry_signal_source'] == leader.SIGNAL_SOURCE


def test_lowest_return_selects_hype():
    # 2026-11-09 00:30 CST first closed Coinbase candle, before the HYPE loss.
    values = {'BTC': '.022870656933671185', 'ETH': '-.0184205554505934',
              'SOL': '.06959749449020336', 'XRP': '.04577497024627064',
              'HYPE': '-.13468013468013185'}
    rows = [ranked.Observation(leader.COINS[k], START, D(100), D(100)+D(v), START+240)
            for k, v in values.items()]
    assert leader.lowest_return_choice(rows, START, START+240)[0] == 'HYPE'


@pytest.mark.parametrize('kind', ['live_tick', 'missing', 'wrong_window', 'tie'])
def test_unproven_minute_four_never_buys(tmp_path, kind):
    class BadFeed:
        def snapshot(self, names, start):
            rows = observations(START+241 if kind == 'live_tick' else START+240)
            if kind == 'missing':
                return rows[:-1]
            if kind == 'wrong_window':
                return [ranked.Observation(r.series, START-900, r.opening, r.price, START+240)
                        for r in rows]
            if kind == 'tie':
                return [ranked.Observation(r.series, START, D(100), D(100), START+240)
                        for r in rows]
            return rows
    venue = Exchange()
    runner = leader.LeaderRunner(tmp_path/'leader.json', provider=BadFeed(), clock=lambda:START+240)
    runner.run(venue, live=True)
    assert not venue.submissions
    assert not runner.journal.data['windows']


class Response:
    def __init__(self, rows): self.rows = rows
    def raise_for_status(self): pass
    def json(self): return self.rows


class Session:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **kwargs):
        assert url.endswith('/candles') and kwargs['params']['granularity'] == 60
        return Response(self.rows)


def test_provider_uses_exact_closed_candle_not_later_price():
    rows = [[START+240, 90, 200, 101, 180, 1], [START+180, 90, 110, 101, 102, 1], [START, 90, 110, 100, 101, 1]]
    provider = leader.MinuteFourPerformance(clock=lambda: START+250,
                                               session_factory=lambda:Session(rows))
    result = provider.snapshot(leader.SOURCE_SERIES, START)
    assert len(result) == 5
    assert all(r.return_pct == D(2) and r.observed_at == START+240 for r in result)


@pytest.mark.parametrize('rows', [[], [[START+240, 90, 110, 100, 102, 1]],
                                [[START, 90, 110, 100, 102, 1]]*2])
def test_provider_rejects_missing_or_duplicate_opening_candle(rows):
    provider = leader.MinuteFourPerformance(clock=lambda:START+240,
                                               session_factory=lambda:Session(rows))
    with pytest.raises(ranked.PerformanceUnavailable):
        provider.snapshot(leader.SOURCE_SERIES, START)


def test_provider_never_reads_unclosed_candle():
    provider = leader.MinuteFourPerformance(clock=lambda:START+239,
        session_factory=lambda:pytest.fail('must not request a forming candle'))
    with pytest.raises(ranked.PerformanceUnavailable):
        provider.snapshot(leader.SOURCE_SERIES, START)


def test_quote_remains_ineligible_no_substitute_or_late_entry(tmp_path):
    at = [START+240]
    venue = Exchange()
    venue.book['no_ask_dollars'] = '1.00'
    runner = leader.LeaderRunner(tmp_path/'leader.json', provider=Feed(lambda:at[0]), clock=lambda:at[0])
    for elapsed in [240, 255, 285, 299]:
        at[0] = START+elapsed
        runner.run(venue, live=True)
    venue.book['no_ask_dollars'] = '.75'
    at[0] = START+300
    runner.run(venue, live=True)
    assert not venue.submissions


def test_lowest_return_keeps_five_coin_mean():
    # SOL is on the mean and BTC is last.
    vals = {'BTC': -4, 'ETH': .5, 'SOL': 0, 'XRP': 1, 'HYPE': 2.5}
    rows = [ranked.Observation(leader.COINS[k], START, D(100), D(100)+D(str(v)), START+240)
            for k,v in vals.items()]
    coin, mean, _ = leader.lowest_return_choice(rows, START, START+240)
    assert mean == 0 and coin == 'BTC'
