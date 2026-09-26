from datetime import datetime, timezone
import pytest
import bot
from test_five_minute_exits import cycle_setup


def setup_current_window(monkeypatch, elapsed):
    start, end, cancel = bot.START, bot.END, bot.CANCEL_AFTER
    assert (start, end, cancel) == (0, 480, 480)
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    monkeypatch.setattr(bot, 'START', start)
    monkeypatch.setattr(bot, 'END', end)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', cancel)
    monkeypatch.setattr(bot, 'OPENING_BIAS_ENABLED', False)
    record['spot_entry_attempted'] = True
    record['entry_cancel_at'] = 1000000480
    return fake, record, state, clock, closed


@pytest.mark.parametrize('elapsed', [0, 300, 360, 420, 479, 480, 481, 600])
def test_regular_entries_through_eight_minute_boundary(monkeypatch, elapsed):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, elapsed)
    bot.cycle(state)
    assert bool(fake.entries) == (elapsed < 480)
    assert all(item[3]['expiration_time'] == 1000000480 for item in fake.entries)
    assert record['close_timestamp'] == 1000000900


@pytest.mark.parametrize('elapsed', [479, 480, 481])
def test_cancel_regular_orders_at_eight_minutes(monkeypatch, elapsed):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, elapsed)
    record.update(orders=['resting'], dual_limit_attempted=True)
    monkeypatch.setattr(bot, 'MAX_BUYS', 0)
    monkeypatch.setattr(bot, 'HISTORICAL_STRIKE_ENABLED', False)
    bot.cycle(state)
    assert fake.cancelled == (['resting'] if elapsed >= 480 else [])


def test_slow_batch_cannot_send_next_order_past_eight_minutes(monkeypatch):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, 479)
    original = fake._order
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 1000000481
        return result
    monkeypatch.setattr(fake, '_order', slow)
    bot.cycle(state)
    assert len(fake.entries) == 1
    assert fake.entries[0][3]['expiration_time'] == 1000000480
