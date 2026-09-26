"""Offline end-to-end settlement switches; never contact a live account."""
import copy
import json
from decimal import Decimal as D

import pytest

import bot
from kalshi import KalshiAPIError
from take_profit import TakeProfitMonitor
from test_five_minute_exits import cycle_setup
from test_price_pairs import PairExchange


class SwitchExchange(PairExchange):
    def __init__(self, desired, liquidity='100'):
        super().__init__(bid='.02', liquidity=liquidity)
        self.desired = desired
        self.buys = []
        self.quotes = {'yes_ask_dollars': '.97' if desired == 'YES' else '.03',
                       'no_ask_dollars': '.97' if desired == 'NO' else '.03',
                       'yes_bid_dollars': '.96' if desired == 'YES' else '.02',
                       'no_bid_dollars': '.96' if desired == 'NO' else '.02'}
        self.entry_failure = None

    def market(self, ticker):
        return dict(self.quotes, ticker=ticker)

    def market_cash(self, ticker):
        return {'exchange_index': 2, 'cash_dollars': '100'}

    def request(self, method, path, params=None, body=None, auth=False):
        if body['reduce_only']:
            return super().request(method, path, params, body, auth)
        assert method == 'POST' and body['time_in_force'] == 'immediate_or_cancel'
        assert self.held == 0, 'A settlement buy must not merely net out old inventory'
        self.buys.append(dict(body))
        sign = 1 if body['side'] == 'bid' else -1
        quantity = D(body['count'])
        oid = f'settlement-{len(self.buys)}'
        self.held += sign * quantity
        self.remote[oid] = {'order_id': oid, 'client_order_id': body['client_order_id'],
                            'status': 'executed', 'fill_count_fp': str(quantity)}
        self.fill(oid, sign, quantity, body['price'])
        if self.entry_failure:
            raise self.entry_failure
        return {'order_id': oid}


def setup_switch(tmp_path, monkeypatch, desired='YES', liquidity='100', manual=False):
    _, record, state, clock, closed = cycle_setup(monkeypatch, 720)
    monkeypatch.setattr(bot, 'END', 720)
    monkeypatch.setattr(bot, 'CANCEL_AFTER', 720)
    exchange = SwitchExchange(desired, liquidity)
    old_sign = -1 if desired == 'YES' else 1
    if manual:
        exchange.manual(old_sign, D(2))
    else:
        exchange.buy('.39', '2', old_sign)
        exchange.intents[0].update(kind='regular', quantity='2', reserved_dollars='.84',
            cancel_at=closed.timestamp() - 540, entry_execution_version=3, entry_closed=False)
    record.update(entry_intents=exchange.intents, orders=[], historical_strike_orders=[],
                  trade_side='NO' if desired == 'YES' else 'YES', close_timestamp=closed.timestamp(),
                  signal={'prediction': 'NO' if desired == 'YES' else 'YES', 'build': bot.SIGNAL_BUILD},
                  entry_cancel_at=closed.timestamp() - 180)
    state['markets'] = {'T': record}
    events = []
    monitor = TakeProfitMonitor(exchange, lambda: copy.deepcopy(state), tmp_path / 'switch.json',
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: clock[0],
        emit=lambda event, **data: events.append((event, data)))
    monkeypatch.setattr(bot, 'client', exchange)
    monkeypatch.setattr(bot, 'EXIT_MONITOR', monitor)
    return exchange, record, state, clock, closed, monitor, events


@pytest.mark.parametrize('desired', ['YES', 'NO'])
@pytest.mark.parametrize('manual', [False, True])
def test_loss_close_is_confirmed_before_fixed_97_cent_buy(tmp_path, monkeypatch, desired, manual):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch, desired, manual=manual)
    reserved = sum(D(i['reserved_dollars']) for i in record['entry_intents'])
    bot.settlement_entry(record, state, 'T', closed)
    assert record['settlement_switch']['phase'] == 'requested'
    assert not e.buys and not e.submissions
    monitor.run_once()
    assert len(e.submissions) == 1 and e.held == 0
    assert e.submissions[0]['reduce_only'] is True
    assert e.submissions[0]['price'] == ('0.9800' if desired == 'YES' else '0.0200')
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys  # Flat alone is not proof the close is reconciled.
    monitor.run_once()
    assert monitor.settlement_ready('T', desired)
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.buys) == 1 and D(e.buys[0]['count']) == 6
    assert e.buys[0]['price'] == ('0.9700' if desired == 'YES' else '0.0300')
    assert record['trade_side'] == desired
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) == reserved + 6
    assert any(event == 'SETTLEMENT_CLOSE_FILL' for event, _ in events)
    monitor.run_once()
    assert monitor.healthy and abs(e.held) == 6
    assert len(e.submissions) == 1  # Settlement inventory is never scalped.
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.buys) == 1


@pytest.mark.parametrize('desired', ['YES', 'NO'])
def test_partial_loss_close_and_restart_never_duplicate_inventory(tmp_path, monkeypatch, desired):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch, desired, liquidity='.75')
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    assert abs(e.held) == D('1.25') and not e.buys
    restarted = TakeProfitMonitor(e, lambda: copy.deepcopy(state), monitor.path,
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: clock[0], emit=lambda *a, **k: None)
    monkeypatch.setattr(bot, 'EXIT_MONITOR', restarted)
    assert not restarted.settlement_ready('T', desired)
    e.liquidity = D(100)
    restarted.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys
    restarted.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert [b['count'] for b in e.submissions] == ['2', '1.25']
    assert len(e.buys) == 1


def test_lost_close_ack_recovered_before_buy(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    e.lose_ack = True
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    assert e.held == 0 and not monitor.healthy
    saved = json.loads(monitor.path.read_text())['markets']['T']['pending']
    assert saved['purpose'] == 'settlement_switch' and 'order_id' not in saved
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys
    e.lose_ack = False
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.submissions) == len(e.buys) == 1


def test_close_404_and_delayed_fill_history_both_block_new_buy(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    order = e.order
    def missing(*args):
        raise KalshiAPIError(404, 'not visible')
    monkeypatch.setattr(e, 'order', missing)
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys and len(e.submissions) == 1
    monkeypatch.setattr(e, 'order', order)
    fill = e.history.pop()
    clock[0] += 2
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys and not monitor.healthy
    e.history.append(fill)
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.buys) == 1 and len(e.submissions) == 1


def test_unknown_prior_buy_ack_blocks_loss_close(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    record['entry_intents'][0].pop('order_id')
    e.remote.clear()
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    assert not e.buys and not e.submissions and not monitor.healthy


def test_unknown_existing_take_profit_must_reconcile_before_loss_close(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    monitor.run_once()  # Ordinary .46 take-profit IOC; no fill at the .02 bid.
    assert len(e.submissions) == 1
    bot.settlement_entry(record, state, 'T', closed)
    order = e.order
    def missing(*args):
        raise KalshiAPIError(404, 'prior exit not visible')
    monkeypatch.setattr(e, 'order', missing)
    monitor.run_once()
    assert len(e.submissions) == 1 and not e.buys
    monkeypatch.setattr(e, 'order', order)
    clock[0] += 2
    monitor.run_once()
    assert len(e.submissions) == 2 and not e.buys


@pytest.mark.parametrize('failure', [TimeoutError('outcome unknown'), KalshiAPIError(429, 'limit', retry_after='30')])
def test_failed_close_never_allows_netting_buy(tmp_path, monkeypatch, failure):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    e.failure = failure
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys and abs(e.held) == 2 and not monitor.healthy
    monitor.run_once()
    assert len(e.submissions) == 1


def test_missing_liquidity_or_changed_97_quote_does_not_force_a_sale(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    e.quotes['yes_ask_dollars'] = '.98'
    monitor.run_once()
    assert not e.submissions
    e.quotes['yes_ask_dollars'] = '.97'
    e.quotes['no_bid_dollars'] = '0'
    monitor.run_once()
    assert not e.submissions and not e.buys


def test_market_close_prevents_both_transition_orders(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    clock[0] = closed.timestamp()
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.submissions and not e.buys


def test_exhausted_allowance_does_not_liquidate_for_unfundable_entry(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    record['entry_intents'][0]['reserved_dollars'] = '10'
    bot.settlement_entry(record, state, 'T', closed)
    assert 'settlement_switch' not in record and not e.buys and not e.submissions


def test_new_opposite_inventory_invalidates_ready_receipt(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    monitor.run_once()
    e.manual(-1, D(1))
    bot.settlement_entry(record, state, 'T', closed)
    assert not e.buys


def test_ambiguous_settlement_buy_never_retries_or_recycles_budget(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    monitor.run_once()
    e.entry_failure = TimeoutError('buy ACK lost')
    with pytest.raises(TimeoutError):
        bot.settlement_entry(record, state, 'T', closed)
    spent = sum(D(i['reserved_dollars']) for i in record['entry_intents'])
    bot.settlement_entry(record, state, 'T', closed)
    assert len(e.buys) == 1 and sum(D(i['reserved_dollars']) for i in record['entry_intents']) == spent


def test_completed_switch_does_not_authorize_new_loss_closes_after_manual_trade(tmp_path, monkeypatch):
    e, record, state, clock, closed, monitor, events = setup_switch(tmp_path, monkeypatch)
    bot.settlement_entry(record, state, 'T', closed)
    monitor.run_once()
    monitor.run_once()
    bot.settlement_entry(record, state, 'T', closed)
    e.manual(-1, D(8))  # Later manual trade reverses the new six-contract holding.
    monitor.run_once()
    assert len(e.submissions) == 1 and len(e.buys) == 1
    assert not monitor.settlement_ready('T', 'YES')
