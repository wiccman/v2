"""Offline regressions for the inclusive price block and resting-order cleanup."""
from decimal import Decimal as D
import pytest
import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('price', ['.70', '.73', '.75', '.85'])
@pytest.mark.parametrize('elapsed', [360, 420, 480, 660, 719.999, 720, 779.999])
@pytest.mark.parametrize('kind', ['regular', 'late_bias', 'historical', 'spot', 'opening_bias', 'dual_limit'])
def test_every_route_blocks_without_api_or_reservation(monkeypatch, side, price, elapsed, kind):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, price, side)
    monkeypatch.setattr(fake, 'market', lambda ticker: pytest.fail('blocked buy reached API'))
    assert bot.funded_entry(record, state, 'TEST', side, D(price), closed, kind,
                            now_timestamp=closed.timestamp()-899) == ({}, 0)
    assert not fake.entries and not record['entry_intents']


@pytest.mark.parametrize('price,elapsed,blocked', [('.70',359.999,False),('.70',360,True),
    ('.85',779.999,True),('.85',780,False),('.6999',480,False),('.8501',480,False),('.96',720,False)])
def test_exact_boundaries(price, elapsed, blocked):
    assert bot.blocked_buy_window(price, 1900, 1000+elapsed) is blocked


@pytest.mark.parametrize('include_close', [True, False])
def test_restart_cancels_blocked_pending_buy_but_preserves_settlement(monkeypatch, include_close):
    fake, record, state, clock, closed = setup(monkeypatch, 720, '.96')
    record['entry_cancel_at'] = closed.timestamp()-180
    if include_close:
        record['close_timestamp'] = closed.timestamp()
    record['entry_intents'] = [dict(order_id=oid, price=price, side='YES', kind=kind,
        entry_execution_version=bot.ENTRY_EXECUTION_VERSION, cancel_at=closed.timestamp(),
        quantity='3', reserved_dollars='3', resting_entry=True,
        hold_to_settlement=kind == bot.SETTLEMENT_KIND, exit_target='1')
        for oid, price, kind in [('blocked','.75','regular'),('settlement','.96',bot.SETTLEMENT_KIND)]]
    bot.reconcile_entries(state)
    assert fake.cancelled == ['blocked']
    assert record['entry_intents'][0]['entry_closed']
    assert not record['entry_intents'][1].get('entry_closed')


@pytest.mark.parametrize('elapsed', [360, 480, 660, 719.999])
@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_live_cycle_cannot_submit_former_high_price_tiers(monkeypatch, elapsed, side):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, '.75', side)
    monkeypatch.setattr(bot, 'DIRECTIONAL_ENTRY_POLICY', True)
    monkeypatch.setattr(bot, 'ENTRY_START_DELAY', 60)
    monkeypatch.setattr(fake, 'btc_reference_price', lambda: D('100050' if side == 'YES' else '99950'))
    bot.cycle(state)
    assert not fake.entries


def test_slow_call_cannot_cross_block_start(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 359, '.75')
    # Permit a pre-window route to exercise the final deadline defensively.
    monkeypatch.setattr(bot, 'SIX_MINUTE_ENTRY_START', 300)
    def slow_cash(ticker):
        clock[0] = closed.timestamp()-900+360
        return {'exchange_index': 2, 'cash_dollars': '100'}
    monkeypatch.setattr(fake, 'market_cash', slow_cash)
    assert bot.funded_entry(record, state, 'TEST', 'YES', D('.75'), closed, 'regular') == ({}, 0)
    assert not fake.entries and not record['entry_intents']
