"""Offline rule, timestamp, missing-data and upgrade regressions."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
import bot
from boruto import BUILD, build_signal, vote
from test_five_minute_exits import cycle_setup

T = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def fixture(values=(95, 96, 97, 98, 99), strike=101, previous_strike=100):
    target = dict(ticker='TARGET', open_time=T.isoformat(),
                  close_time=(T + timedelta(minutes=15)).isoformat(), floor_strike=strike)
    history = []
    for offset, value in zip((5, 4, 3, 2, 1, 0), list(values) + [999999]):
        end = T - timedelta(minutes=15 * offset)
        history.append(dict(ticker=f'PAST-{offset}', status='finalized', close_time=end.isoformat(),
                            open_time=(end - timedelta(minutes=15)).isoformat(),
                            expiration_value=value, floor_strike=previous_strike if offset == 0 else 100))
    return target, history


@pytest.mark.parametrize('points,strike,side,agreement', [
    ([97,98,99,101],100,'YES','MODERATE'),
    ([101,102,103,104],100,'NO','HIGH'),
    ([97,98,101,102],100,'NO','LOW'),
    ([97,98,99,100],100,'YES','MODERATE'),
    ([101,102,103,100],100,'NO','MODERATE'),
    ([97,98,100,100],100,'YES','LOW'),
    ([101,102,97,98],100,'YES','LOW'),
    ([100,100,100,100],100,'YES','LOW'),
])
def test_four_point_votes(points,strike,side,agreement):
    result=vote(points,strike)
    assert result['bias']==side and result['agreement']==agreement


def test_exact_current_and_previous_boundaries_exclude_t():
    target, history = fixture()
    signal = build_signal(target, history, T)
    assert [p['ticker'] for p in signal['lookbacks']] == ['PAST-4','PAST-3','PAST-2','PAST-1']
    assert [p['ticker'] for p in signal['previous_lookbacks']] == ['PAST-5','PAST-4','PAST-3','PAST-2']
    assert signal['prediction']=='YES' and signal['previous_bias']=='YES'
    assert signal['build']==BUILD and signal['previous_strike']=='100'


def test_opposing_raw_biases_allow_current_prediction():
    target, history = fixture(strike=101, previous_strike=90)
    signal=build_signal(target,history,T)
    assert signal['current_bias']=='YES' and signal['previous_bias']=='NO'
    assert signal['prediction']=='YES' and signal['conflict'] is True


def test_previous_tie_leaves_current_side_alone():
    target,history=fixture(values=(95,96,104,105,106),strike=110,previous_strike=100)
    signal=build_signal(target,history,T)
    assert signal['previous_bias']=='NO' and signal['prediction']=='YES'


def test_current_split_uses_latest_point_despite_previous_direction():
    target,history=fixture(values=(94,95,96,104,105),strike=100,previous_strike=110)
    signal=build_signal(target,history,T)
    assert signal['previous_bias']=='YES' and signal['prediction']=='NO'
    assert signal['reason']=='latest_non_equal_lookback'


@pytest.mark.parametrize('offset',range(1,5))
def test_missing_period_cannot_be_replaced_by_an_older_one(offset):
    target,history=fixture()
    history=[x for x in history if x['ticker']!=f'PAST-{offset}']
    with pytest.raises(RuntimeError,match='DATA UNAVAILABLE'):
        build_signal(target,history,T)


@pytest.mark.parametrize('value',['NaN','Infinity','0','-1',None])
def test_invalid_finalized_price_blocks_signal(value):
    target,history=fixture();history[1]['expiration_value']=value
    with pytest.raises(RuntimeError,match='DATA UNAVAILABLE'):
        build_signal(target,history,T)


def test_unfinalized_and_conflicting_periods_block_signal():
    target,history=fixture();history[1]['status']='closed'
    with pytest.raises(RuntimeError,match='DATA UNAVAILABLE'):build_signal(target,history,T)
    history[1]['status']='finalized';history.append(dict(history[1],expiration_value=1))
    with pytest.raises(RuntimeError,match='conflicting'):build_signal(target,history,T)


def test_closed_window_cannot_get_a_fresh_lock():
    target,history=fixture()
    with pytest.raises(RuntimeError,match='open 15-minute'):build_signal(target,history,T+timedelta(minutes=15))


def test_legacy_saved_signal_blocks_new_entries_without_rewriting_it(monkeypatch):
    fake,record,state,clock,closed=cycle_setup(monkeypatch,60)
    record['signal'].pop('build')
    before=dict(record['signal'])
    bot.cycle(state)
    assert not fake.entries and record['signal']==before
    assert record['boruto_upgrade_wait_logged']


def test_skip_blocks_all_entry_routes(monkeypatch):
    fake,record,state,clock,closed=cycle_setup(monkeypatch,60)
    record['signal'].update(prediction='SKIP',reason='previous_current_bias_conflict')
    bot.cycle(state)
    assert not fake.entries


def test_failed_signal_fetch_leaves_no_partial_lock(monkeypatch):
    fake,record,state,clock,closed=cycle_setup(monkeypatch,60)
    record['signal']=None
    monkeypatch.setattr(fake,'markets',lambda **kw: [],raising=False)
    monkeypatch.setattr(bot,'build_signal',lambda *args: (_ for _ in ()).throw(RuntimeError('DATA UNAVAILABLE')))
    with pytest.raises(RuntimeError,match='DATA UNAVAILABLE'):bot.cycle(state)
    assert record['signal'] is None and not fake.entries


@pytest.mark.parametrize('offset',[0,5])
def test_missing_previous_only_data_does_not_block_current(offset):
    target,history=fixture()
    history=[x for x in history if x['ticker']!=f'PAST-{offset}']
    signal=build_signal(target,history,T)
    assert signal['prediction']=='YES' and signal['previous_bias'] is None


def test_low_confidence_tie_reaches_regular_order_gateway(monkeypatch):
    import bot
    from decimal import Decimal as D
    from test_five_minute_exits import cycle_setup
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record['signal']['base_confidence'] = 'LOW'
    bot.cycle(state)
    assert any(i['kind'] == 'regular' for i in record['entry_intents'])
    assert bot.entry_price_allowed('LOW', D('0.45'))
