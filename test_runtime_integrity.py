"""Offline regressions for state, exact lookbacks, timing, and entry priority."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import bot
from test_five_minute_exits import cycle_setup


def test_missing_ledger_is_never_automatically_reset(monkeypatch, tmp_path):
    path = tmp_path / 'state.json'
    monkeypatch.setattr(bot, 'STATE', path)
    with pytest.raises(RuntimeError, match='STATE_MISSING'):
        bot.load_state()
    assert not path.exists()


@pytest.mark.parametrize('payload', ['[]', '{}', '{"markets": []}', 'broken json'])
def test_invalid_ledger_fails_closed(monkeypatch, tmp_path, payload):
    path = tmp_path / 'state.json'
    path.write_text(payload)
    monkeypatch.setattr(bot, 'STATE', path)
    with pytest.raises(ValueError):
        bot.load_state()
    assert path.read_text() == payload


def test_storage_preserves_reservations_and_requires_mounted_volume(monkeypatch, tmp_path):
    path = tmp_path / 'state.json'
    state = {'markets': {'T': {'entry_intents': [{'reserved_dollars': '4.99'}]}}}
    path.write_text(json.dumps(state))
    monkeypatch.setattr(bot, 'STATE', path)
    monkeypatch.setattr(bot, 'LOG', tmp_path / 'trades.csv')
    monkeypatch.setenv('RAILWAY_ENVIRONMENT_ID', 'test')
    monkeypatch.setenv('RAILWAY_VOLUME_MOUNT_PATH', str(tmp_path))
    monkeypatch.setattr(bot.os.path, 'ismount', lambda p: False)
    with pytest.raises(RuntimeError, match='STATE_VOLUME_REQUIRED'):
        bot.validate_storage()
    monkeypatch.setattr(bot.os.path, 'ismount', lambda p: True)
    bot.validate_storage()
    assert bot.load_state() == state
    monkeypatch.setattr(bot, 'LOG', tmp_path.parent / 'outside.csv')
    with pytest.raises(RuntimeError, match='STATE_VOLUME_REQUIRED'):
        bot.validate_storage()


def lookbacks(monkeypatch):
    started = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    rows = [dict(close_time=(started - timedelta(minutes=15 * i)).isoformat(),
                 expiration_value=str(100000 + i), floor_strike=str(99000 + i))
            for i in range(5)]
    monkeypatch.setattr(bot, 'client', SimpleNamespace(markets=lambda **kw: rows))
    return started, rows


def test_exact_consecutive_lookbacks_in_chronological_order(monkeypatch):
    started, rows = lookbacks(monkeypatch)
    assert bot.prior_three(started) == [D('100003'), D('100002'), D('100001')]
    assert bot.prior_strikes(started) == [D('99002'), D('99001'), D('99000')]


def test_older_settlements_cannot_replace_missing_immediate_period(monkeypatch):
    started, rows = lookbacks(monkeypatch)
    latest = rows.pop(1)
    with pytest.raises(RuntimeError, match='exact prior periods'):
        bot.prior_three(started)
    rows.append(latest)  # A later cycle can use the newly finalized settlement.
    assert bot.prior_three(started)[-1] == D('100001')


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '0', '-1'])
def test_invalid_settlement_is_rejected(monkeypatch, value):
    started, rows = lookbacks(monkeypatch)
    rows[1]['expiration_value'] = value
    with pytest.raises(RuntimeError, match='invalid finalized'):
        bot.prior_three(started)


def test_conflicting_same_period_is_rejected(monkeypatch):
    started, rows = lookbacks(monkeypatch)
    rows.append(dict(rows[1], expiration_value='1'))
    with pytest.raises(RuntimeError, match='conflicting'):
        bot.prior_three(started)


def test_previous_bias_is_rebuilt_from_official_kalshi_values(monkeypatch):
    started, _ = lookbacks(monkeypatch)
    # Previous target opened T-15. Its exact T-45/T-30/T-15 prices are the
    # current target's T-60/T-45/T-30 values: all above its official strike.
    # A contradictory saved signal must not override those official values.
    stale = {'markets': {'old': {'signal': {'prediction': 'YES'}}}}
    assert bot.previous_market_bias(stale, started) == 'NO'


def prediction_setup(monkeypatch):
    monkeypatch.setattr(bot, 'write_log', lambda *a, **k: None)
    return {'predictions': [], 'signal': {'prediction': 'YES'}}, {
        'yes_ask_dollars': '0.60', 'yes_bid_dollars': '0.59'}


def test_late_start_marks_all_snapshots_missed(monkeypatch):
    record, market = prediction_setup(monkeypatch)
    assert bot.update_prediction(record, 'T', market, 420)
    assert [p['scheduled_minute'] for p in record['predictions']] == [2, 4, 6]
    assert all(p['status'] == 'missed' and p['ask'] is None for p in record['predictions'])
    assert record['final_confidence'] is None
    assert not bot.update_prediction(record, 'T', market, 421)


def test_delayed_cycle_only_captures_current_slot(monkeypatch):
    record, market = prediction_setup(monkeypatch)
    bot.update_prediction(record, 'T', market, 241)
    assert [p['status'] for p in record['predictions']] == ['missed', 'captured']
    assert record['predictions'][1]['observed_elapsed_seconds'] == 241
    assert record['predictions'][1]['recorded_at']
    bot.update_prediction(record, 'T', market, 360)
    assert record['final_confidence'] is None


def test_complete_snapshots_have_final_average(monkeypatch):
    record, market = prediction_setup(monkeypatch)
    for elapsed, ask in [(120, '0.60'), (240, '0.70'), (360, '0.80')]:
        market['yes_ask_dollars'] = ask
        bot.update_prediction(record, 'T', market, elapsed)
    assert record['final_confidence'] == '70.0%'


@pytest.mark.parametrize('elapsed,status', [(135, 'captured'), (136, 'missed')])
def test_capture_grace_boundary(monkeypatch, elapsed, status):
    record, market = prediction_setup(monkeypatch)
    bot.update_prediction(record, 'T', market, elapsed)
    assert record['predictions'][0]['status'] == status


def test_skip_never_reads_no_quote_as_a_prediction(monkeypatch):
    record, market = prediction_setup(monkeypatch)
    record['signal']['prediction'] = 'SKIP'
    bot.update_prediction(record, 'T', {}, 120)
    assert record['predictions'][0]['ask'] is None


def test_logs_visible_even_if_csv_write_fails(monkeypatch, tmp_path, capsys):
    parent = tmp_path / 'file'
    parent.write_text('not a directory')
    monkeypatch.setattr(bot, 'LOG', parent / 'trades.csv')
    with pytest.raises(OSError):
        bot.write_log('LOOP_ERROR', 'T', details='offline test')
    row = json.loads(capsys.readouterr().out)
    assert row['event'] == 'LOOP_ERROR' and row['ticker'] == 'T'


def test_signal_entries_have_priority_and_dual_batch_shares_one_budget(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    monkeypatch.setattr(bot, 'BUDGET', D('2'))
    bot.cycle(state)
    intents = record['entry_intents']
    assert [i['kind'] for i in intents[:1]] == ['regular']
    dual = [i for i in intents if i['kind'] == 'dual']
    assert len(dual) == 1
    assert sum(D(i['quantity']) * D(i['price']) for i in dual) <= D('2')
    assert sum(D(i['reserved_dollars']) for i in intents) <= D('5')


def test_slow_quote_is_not_backdated_to_scheduled_snapshot(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record['predictions'] = []
    original = fake.market
    def slow_market(ticker):
        clock[0] = 1000000140
        return original(ticker)
    monkeypatch.setattr(fake, 'market', slow_market)
    bot.cycle(state)
    assert record['predictions'][0]['status'] == 'missed'
    assert not any(i['kind'] == 'regular' for i in record['entry_intents'])


def test_archived_strategy_ownership_is_preserved(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    state['mm'] = {'markets': {'TEST': {}}}
    bot.cycle(state)
    assert not fake.entries


@pytest.mark.parametrize('record', [None, {'entry_intents': {}},
    {'entry_intents': [{}]}, {'entry_intents': [{'reserved_dollars': '-2'}]},
    {'entry_intents': [{'reserved_dollars': 'NaN'}]}])
def test_damaged_market_reservations_cannot_restore_allowance(monkeypatch, tmp_path, record):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'markets': {'T': record}}))
    monkeypatch.setattr(bot, 'STATE', path)
    with pytest.raises(ValueError, match='STATE_INVALID'):
        bot.load_state()


def test_opening_bias_posts_one_side_at_52_and_cancels_at_two_minutes(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    monkeypatch.setattr(bot, "DUAL_LIMIT_BUYS_ENABLED", False)
    monkeypatch.setattr(bot, "HISTORICAL_STRIKE_ENABLED", False)
    monkeypatch.setattr(bot, "SPOT_ENTRY_WINDOW", 0)
    monkeypatch.setattr(bot, "START", 120)
    monkeypatch.setattr(bot, "OPENING_BIAS_ENABLED", True)
    bot.cycle(state)
    assert len(fake.entries) == 1
    side, quantity, price, options = fake.entries[0]
    assert side == "bid" and price == D("0.52")
    assert options["expiration_time"] == 1000000120
    intent = record["entry_intents"][0]
    assert intent["kind"] == "opening_bias" and intent["exit_target"] == "0.6"
    order_id = record["orders"][0]
    bot.reconcile_entries(state, now_timestamp=1000000120)
    assert fake.cancelled == [order_id]
    assert intent["entry_closed"] is True


def test_new_prediction_does_not_fetch_previous_bias(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record['signal'] = None
    from test_boruto import fixture, T
    from boruto import build_signal
    target, history = fixture()
    history = [x for x in history if x['ticker'] not in ('PAST-0', 'PAST-5')]
    monkeypatch.setattr(fake, 'markets', lambda **kw: history, raising=False)
    monkeypatch.setattr(bot, 'build_signal', lambda *args: build_signal(target, history, T))
    def unavailable(*args):
        raise AssertionError('Previous bias must not be required')
    monkeypatch.setattr(bot, 'previous_market_bias', unavailable)
    bot.cycle(state)
    assert record['signal']['prediction'] == 'YES'
    assert fake.entries
    assert all(i['price'] == '0.39' for i in record['entry_intents'])
