from datetime import datetime, timezone
from decimal import Decimal as D
import pytest
import bot
from test_five_minute_exits import cycle_setup


def setup_current_window(monkeypatch, elapsed):
    start, end, cancel = bot.START, bot.END, bot.CANCEL_AFTER
    assert (start, end, cancel) == (0, 780, 780)
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    monkeypatch.setattr(bot, 'START', start)
    monkeypatch.setattr(bot, 'END', end)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', cancel)
    monkeypatch.setattr(bot, 'OPENING_BIAS_ENABLED', False)
    record['spot_entry_attempted'] = True
    record['entry_cancel_at'] = 1000000720
    return fake, record, state, clock, closed


@pytest.mark.parametrize('elapsed', [0, 59.999, 60, 180, 300, 359, 360, 420, 479, 480, 481, 600, 719, 720])
def test_current_dispatcher_does_not_run_retired_regular_entries(monkeypatch, elapsed):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, elapsed)
    fake.held = D('0')
    if 480 <= elapsed < 720:
        market = {**fake.market('TEST'), 'yes_ask_dollars': '.70', 'no_ask_dollars': '.30'}
        monkeypatch.setattr(fake, 'market', lambda ticker: market)
    bot.cycle(state)
    assert not fake.entries
    assert not record['entry_intents']
    assert record['close_timestamp'] == 1000000900


@pytest.mark.parametrize('elapsed', [359, 360, 361])
def test_cancel_lower_price_orders_at_six_minutes(monkeypatch, elapsed):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, elapsed)
    record.update(orders=['resting'], dual_limit_attempted=True)
    record['entry_intents'] = [dict(order_id='resting', client_id='resting', side='YES',
        price='.53', quantity='5', reserved_dollars='2.80', entry_execution_version=bot.ENTRY_EXECUTION_VERSION,
        kind='regular', entry_closed=False, cancel_at=1000000360)]
    monkeypatch.setattr(bot, 'HISTORICAL_STRIKE_ENABLED', False)
    bot.cycle(state)
    assert fake.cancelled == (['resting'] if elapsed >= 360 else [])


def test_slow_batch_cannot_send_lower_price_order_past_six_minutes(monkeypatch):
    fake, record, state, clock, closed = setup_current_window(monkeypatch, 359)
    original = fake._order
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 1000000361
        return result
    monkeypatch.setattr(fake, '_order', slow)
    list(bot.paired_entries(record, state, 'TEST', 'YES', closed, 'regular'))
    assert len(fake.entries) == 1
    assert fake.entries[0][3]['expiration_time'] == 1000000360



pytestmark = pytest.mark.usefixtures("early_entry_scenario")
