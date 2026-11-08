"""Offline policy tests: every exchange response is a test double."""
import copy
from decimal import Decimal as D

import pytest
import minute8_top2 as bot
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from test_cash_checked_minute9 import CashExchange
from test_single_coin_minute9 import START


class Feed:
    def __init__(self, clock):
        self.clock = clock
        self.order = ['KXETH15M','KXSOL15M','KXHYPE15M','KXXRP15M','KXBTC15M']
        self.fail = False
    def snapshot(self, names, start):
        if self.fail:
            raise ranked.PerformanceUnavailable('offline_missing_feed')
        return [ranked.Observation(s, start, D(100), D(110-i), self.clock())
                for i, s in enumerate(self.order) if s in names]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now, events = [START+540.0], []
    monkeypatch.setattr(core.time, 'time', lambda: now[0])
    monkeypatch.setattr(core, 'event', lambda n, **d: events.append((n, d)))
    journal = core.Journal(tmp_path/'state.json')
    feed = Feed(lambda: now[0])
    with bot.installed_policy(feed):
        yield journal, now, events, feed


def run(c, j, now, feed, live=True):
    return bot.run_pass(c, j, core.DEFAULT_SERIES, D(10), 100, live, now[0],
                        provider=feed, clock=lambda: now[0])


@pytest.mark.parametrize('elapsed,expected', [
    (479.999, 0), (480, 0), (539.999, 0), (540, 1), (600, 1),
    (660, 1), (720, 1), (780, 1), (840, 1), (899.999, 1), (900, 0)])
def test_entry_boundaries(setup, elapsed, expected):
    j, now, _, feed = setup
    now[0] = START+elapsed
    c = CashExchange(); run(c, j, now, feed)
    assert len(c.posted) == expected


def test_backup_waits_full_minute_and_restart_preserves_it(setup):
    j, now, events, feed = setup
    c = CashExchange({'KXETH15M': 'zero'})
    run(c, j, now, feed)
    assert len(c.posted) == 1
    now[0] = START+599.999
    reopened = core.Journal(j.path)
    run(c, reopened, now, feed)
    assert len(c.posted) == 1
    assert any(n == 'M8_BACKUP_WAIT' for n, _ in events)
    now[0] = START+600
    run(c, reopened, now, feed)
    assert [p[0].split('-')[0] for p in c.posted] == ['KXETH15M', 'KXSOL15M']


def test_delayed_confirmation_starts_later_cooldown(setup):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M': 'pending'})
    run(c, j, now, feed)
    now[0] = START+570
    c.orders['order-1']['status'] = 'canceled'
    run(c, j, now, feed)
    now[0] = START+629.99
    run(c, j, now, feed)
    assert len(c.posted) == 1
    now[0] = START+630
    run(c, j, now, feed)
    assert len(c.posted) == 2


@pytest.mark.parametrize('first', ['partial', 'pending', 'fill_lag'])
def test_fill_or_uncertainty_blocks_second_even_after_wait(setup, first):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M': first})
    run(c, j, now, feed)
    now[0] += 120
    run(c, core.Journal(j.path), now, feed)
    assert len(c.posted) == 1


def test_timeout_never_authorizes_backup(setup):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M': 'timeout'})
    with pytest.raises(TimeoutError):
        run(c, j, now, feed)
    now[0] += 120
    run(c, core.Journal(j.path), now, feed)
    assert len(c.posted) == 1


def test_explicit_rejection_also_waits_one_minute(setup):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M': 'api_error'})
    run(c, j, now, feed)
    assert len(c.posted) == 1
    now[0] += 59
    run(c, j, now, feed)
    assert len(c.posted) == 1
    now[0] += 1
    run(c, j, now, feed)
    assert len(c.posted) == 2


def test_cash_not_sent_also_waits_and_no_money_moved(setup):
    j, now, _, feed = setup
    c = CashExchange(payload={'balance':2500,
        'balance_breakdown':[{'exchange_index':0,'balance':'24'},
                             {'exchange_index':2,'balance':'0.50'}]})
    original = copy.deepcopy(c.payload)
    run(c, j, now, feed)
    assert len(j.data['windows']) == 1 and c.posted == []
    now[0] += 59
    run(c, core.Journal(j.path), now, feed)
    assert len(core.Journal(j.path).data['windows']) == 1
    now[0] += 1
    reopened = core.Journal(j.path)
    run(c, reopened, now, feed)
    assert len(reopened.data['windows']) == 2
    assert c.posted == [] and c.payload == original


def test_never_promotes_third_when_top_two_priced_out(setup):
    j, now, events, feed = setup
    c = CashExchange()
    c.initial = {'KXETH15M': '.99', 'KXSOL15M': '.98'}
    run(c, j, now, feed)
    assert c.posted == [] and not j.data['windows']
    q = next(d for n,d in events if n == 'M8_TOP2_QUEUE')
    assert [r['series'] for r in q['queue']] == ['KXETH15M','KXSOL15M']
    assert 'KXHYPE15M' in q['skipped_lower_ranks']


def test_ranks_by_performance_not_fixed_series_order(setup):
    j, now, _, feed = setup
    feed.order = ['KXHYPE15M','KXXRP15M','KXSOL15M','KXETH15M','KXBTC15M']
    c = CashExchange()
    run(c, j, now, feed)
    assert len(c.posted) == 1 and c.posted[0][0].startswith('KXHYPE15M-')


def test_at_most_two_attempts_despite_ranking_changes(setup):
    j, now, _, feed = setup
    c = CashExchange({s:'zero' for s in core.DEFAULT_SERIES})
    run(c, j, now, feed)
    now[0] += 60; run(c, j, now, feed)
    assert len(c.posted) == 2
    feed.order = ['KXSOL15M','KXXRP15M','KXHYPE15M','KXBTC15M','KXETH15M']
    now[0] += 60; run(c, core.Journal(j.path), now, feed)
    assert len(c.posted) == 2


def test_backup_must_still_be_current_top_two(setup):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M':'zero'})
    run(c, j, now, feed)
    feed.order = ['KXSOL15M','KXXRP15M','KXHYPE15M','KXBTC15M','KXETH15M']
    now[0] += 60; run(c, j, now, feed)
    assert [p[0].split('-')[0] for p in c.posted] == ['KXETH15M','KXSOL15M']


def test_cooldown_does_not_block_exit_reconciliation(setup, monkeypatch):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M':'pending'})
    run(c, j, now, feed)
    calls = []
    monkeypatch.setattr(core, 'reconcile_exit', lambda *a: calls.append(a[2]['ticker']))
    now[0] += 10; run(c, j, now, feed)
    assert calls and len(c.posted) == 1


def test_feed_failure_does_not_block_exit_reconciliation(setup, monkeypatch):
    j, now, _, feed = setup
    c = CashExchange({'KXETH15M':'pending'})
    run(c, j, now, feed)
    calls = []
    monkeypatch.setattr(core, 'reconcile_exit', lambda *a: calls.append(a[2]['ticker']))
    feed.fail = True
    now[0] += 70; run(c, j, now, feed)
    assert calls and len(c.posted) == 1


def test_preview_does_not_write_or_submit(setup):
    j, now, events, feed = setup
    before = j.path.read_bytes()
    c = CashExchange(); run(c, j, now, feed, live=False)
    assert c.posted == [] and j.path.read_bytes() == before
    assert len([n for n,d in events if n == 'M9_DRY_RUN_SIGNAL']) == 1


def test_stop_tp_budget_and_streak_unchanged(setup):
    j, now, _, feed = setup
    for _ in range(3):
        j.data['streak'] = core.next_streak(j.data['streak'], 'win')
    j.save()
    c = CashExchange(); run(c,j,now,feed)
    assert c.posted[0][2] == 4
    r = next(iter(j.data['windows'].values()))
    assert D(r['allocated_dollars']) == D(10)/3
    assert core.STOP_DROP == D('.21') and core.PROFIT == D('.05')


@pytest.mark.parametrize('value', [None, True, 'bad', float('nan'), float('inf'), -1])
def test_invalid_cooldown_time_fails_closed(value):
    with pytest.raises(ValueError):
        bot.backup_not_before([{'buy':{'created_at':value}}])


def test_old_window_attempts_do_not_delay_new_window(setup):
    j, now, _, feed = setup
    j.data['windows']['old'] = {'ticker':'old','window_start_ms':(START-900)*1000,
        'buy':{'created_at':START-1},'outcome':{'status':'no_fill','counted':True}}
    c = CashExchange(); run(c,j,now,feed)
    assert len(c.posted) == 1


def test_no_backup_after_close(setup):
    j, now, _, feed = setup
    now[0] = START+850
    c = CashExchange({'KXETH15M':'zero'})
    run(c,j,now,feed)
    now[0] = START+910
    run(c,j,now,feed)
    assert len(c.posted) == 1
