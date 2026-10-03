"""Offline Rule A tests. Fake exchange only; no real orders or network calls."""
import copy
import inspect
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from two_rule_policy import (D, FINAL, RULES, Rule, Receipt, candidate,
                             evaluate_final, target, config)
from two_rule_bot import TwoRuleBot, AtomicStore, Pending, budget_config

TICKER = 'TEST-BTC-15M'
GRID = [{'start': '0', 'end': '1', 'step': '.001'}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError('Network is disabled in tests')
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket, 'create_connection', reject)


class Store:
    def __init__(self):
        self.data = {'schema': 1, 'markets': {}}
        self.saves = 0
    def save(self):
        self.saves += 1


class Exchange:
    def __init__(self, side='YES', fill='9', cash='20'):
        self.now = 1000.0
        self.close = 1060.0
        self.cash = cash
        self.fill_count = D(fill)
        self.quote = {'ticker': TICKER,
                      'yes_ask_dollars': '.96' if side == 'YES' else '.04',
                      'no_ask_dollars': '.96' if side == 'NO' else '.04',
                      'yes_bid_dollars': '.96', 'no_bid_dollars': '.96',
                      'price_ranges': GRID}
        self.book, self.fill_log, self.submissions, self.cancels = [], [], [], []
        self.position = D(0)
        self.quote_reads = 0
        self.quote_hook = None
        self.error = None
    def btc_reference_price(self):
        raise AssertionError('Rule A must never request BTC')
    def market(self, ticker):
        assert ticker == TICKER
        self.quote_reads += 1
        if self.quote_hook:
            self.quote_hook(self)
        if self.error:
            raise self.error
        return dict(self.quote)
    def markets(self, **kwargs):
        return [{**self.quote, 'close_time': datetime.fromtimestamp(self.close, timezone.utc).isoformat()}]
    def market_cash(self, ticker):
        return {'cash_dollars': self.cash}
    def all_orders(self, ticker):
        return copy.deepcopy(self.book)
    def order(self, order_id, ticker):
        return copy.deepcopy(next(o for o in self.book if o['order_id'] == order_id))
    def all_fills(self, ticker):
        return copy.deepcopy(self.fill_log)
    def positions(self, ticker):
        return [{'ticker': ticker, 'position_fp': str(self.position)}] if self.position else []
    def fill(self, oid, side, count, price, role):
        count, price = D(count), D(price)
        self.fill_log.append({'ticker': TICKER, 'order_id': oid,
                              'fill_id': 'fill-' + str(len(self.fill_log)),
                              'count_fp': str(count),
                              'book_side': ('bid' if side == 'YES' else 'ask') if role == 'entry'
                                           else ('ask' if side == 'YES' else 'bid'),
                              'yes_price_dollars': str(price if side == 'YES' else 1 - price),
                              'ts': self.now})
        sign = 1 if side == 'YES' else -1
        self.position += count * sign * (1 if role == 'entry' else -1)
    def place_entry(self, ticker, side, quantity, price, close, **kw):
        self.submissions.append(('entry', side, quantity, price, kw))
        oid = 'order-' + str(len(self.book))
        status = 'executed' if self.fill_count == quantity else 'resting'
        self.book.append({'order_id': oid, 'client_order_id': kw['client_order_id'],
                          'ticker': ticker, 'fill_count_fp': str(self.fill_count),
                          'status': status})
        if self.fill_count:
            self.fill(oid, side, self.fill_count, price, 'entry')
        return dict(self.book[-1])
    def place_take_profit(self, ticker, signed, price, close, **kw):
        side = 'YES' if signed > 0 else 'NO'
        quantity = abs(signed)
        self.submissions.append(('exit', side, quantity, price, kw))
        oid = 'order-' + str(len(self.book))
        self.book.append({'order_id': oid, 'client_order_id': kw['client_order_id'],
                          'ticker': ticker, 'fill_count_fp': str(quantity), 'status': 'executed'})
        self.fill(oid, side, quantity, price, 'exit')
        return dict(self.book[-1])
    def cancel(self, oid, ticker):
        self.cancels.append(oid)
        next(o for o in self.book if o['order_id'] == oid)['status'] = 'canceled'


def setup(side='YES', fill='9', cash='20'):
    x, store, events = Exchange(side, fill, cash), Store(), []
    record = {'close': x.close, 'trades': []}
    store.data['markets'][TICKER] = record
    runner = TwoRuleBot(x, store, clock=lambda: x.now,
                        emit=lambda e, **d: events.append((e, d)))
    return runner, x, record, events


def submit(runner, x, record):
    observed, account, _ = runner.reconcile(TICKER, record)
    return runner.attempt(TICKER, record, FINAL, observed, account)


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('left', [.001, 1, 60, 120])
@pytest.mark.parametrize('spot', [None, 'invalid', '-99999', '999999'])
def test_candidate_ignores_btc_and_missing_strike(side, left, spot):
    x = Exchange(side)
    signal = candidate(FINAL, x.quote, spot, left)
    assert (signal.side, signal.contracts, signal.price, signal.profit) == (side, 9, D('.96'), D('.30'))


@pytest.mark.parametrize('left', [-1, 0, 120.001, 180, 900])
def test_no_entry_outside_two_minutes(left):
    assert candidate(FINAL, Exchange().quote, None, left) is None


@pytest.mark.parametrize('yes,no,reason', [('.95', '.05', 'no_96c_ask'),
                                          ('.964', '.04', 'no_96c_ask'),
                                          ('.96', '.96', 'ambiguous_96c_asks')])
def test_ask_requirements(yes, no, reason):
    assert evaluate_final({'yes_ask_dollars': yes, 'no_ask_dollars': no}, 60) == (None, reason)


def test_only_one_entry_rule():
    old_b = Rule('directional_100', D(100), 9, D(1))
    assert RULES == (FINAL,)
    assert len(config()['rules']) == 1
    assert config()['rules'][0]['strike_distance_dollars'] is None
    assert candidate(old_b, Exchange().quote, '999999', 60) is None
    runner, x, record, _ = setup()
    assert runner.attempt(TICKER, record, old_b, {}, D(0)) is False
    assert not x.submissions and x.quote_reads == 0


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_real_runner_places_nine_without_btc(side):
    runner, x, record, events = setup(side)
    assert submit(runner, x, record)
    assert x.submissions[0][:4] == ('entry', side, D(9), D('.96'))
    assert x.submissions[0][4]['ioc'] is False
    assert record['trades'][0]['profit'] == '.30' or D(record['trades'][0]['profit']) == D('.30')
    assert all('strike' not in key for key in record['trades'][0]['orders'][0]['signal'])
    assert any(e == 'TWO_RULE_CANDIDATE_DECISION' for e, _ in events)


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_runner_refresh_rejects_quote_change(side):
    runner, x, record, _ = setup(side)
    def hook(e):
        if e.quote_reads >= 2:
            e.quote[side.lower() + '_ask_dollars'] = '.97'
    x.quote_hook = hook
    assert not submit(runner, x, record)
    assert not x.submissions


def test_clock_rechecked_after_slow_quote():
    runner, x, record, _ = setup()
    def hook(e):
        if e.quote_reads >= 2:
            e.now = e.close
    x.quote_hook = hook
    assert not submit(runner, x, record)
    assert not x.submissions


@pytest.mark.parametrize('cash,allowed', [('8.90', False), ('8.91', True), ('20', True)])
def test_funding(cash, allowed):
    runner, x, record, _ = setup(cash=cash)
    assert submit(runner, x, record) is allowed
    assert budget_config()['rule_budgets'] == {FINAL.name: '10'}
    assert budget_config()['market_budget'] == '20'
    assert budget_config()['unused_budget_dollars'] == '10'


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_pending_96c_order_never_uses_strike_to_cancel(side):
    runner, x, record, _ = setup(side, fill='0')
    assert submit(runner, x, record)
    x.quote['floor_strike'] = 'invalid'
    assert runner.cancel_ineligible(TICKER, record) is False
    assert not x.cancels


@pytest.mark.parametrize('trigger', ['ask', 'clock', 'no_quote', 'retired_b'])
def test_cancel_only_ineligible_bot_orders(trigger):
    runner, x, record, _ = setup(fill='0')
    assert submit(runner, x, record)
    if trigger == 'ask':
        x.quote['yes_ask_dollars'] = '.97'
    elif trigger == 'clock':
        x.now = x.close
    elif trigger == 'no_quote':
        x.error = ValueError('No quote')
    else:
        record['trades'][0]['rule'] = 'directional_100'
    assert runner.cancel_ineligible(TICKER, record)
    assert x.cancels == ['order-0']
    # Request alone is not confirmation or released budget.
    assert record['trades'][0]['orders'][0]['terminal'] is False


def test_cancel_ownership_mismatch_cannot_cancel_manual_order():
    runner, x, record, _ = setup(fill='0')
    assert submit(runner, x, record)
    x.book[0]['client_order_id'] = 'external'
    x.quote['yes_ask_dollars'] = '.97'
    with pytest.raises(Pending, match='ownership'):
        runner.cancel_ineligible(TICKER, record)
    assert not x.cancels


@pytest.mark.parametrize('saved_name', [FINAL.name, 'final_2m_50'])
def test_one_filled_entry_survives_alias_and_restart(saved_name, tmp_path):
    runner, x, record, _ = setup()
    assert submit(runner, x, record)
    record['trades'][0]['rule'] = saved_name
    store = AtomicStore(tmp_path/'ledger.json')
    store.data['markets'][TICKER] = record
    store.save()
    restored = AtomicStore(tmp_path/'ledger.json')
    again = TwoRuleBot(x, restored, clock=lambda: x.now, emit=lambda *a, **k: None)
    assert not submit(again, x, restored.data['markets'][TICKER])
    assert len(x.submissions) == 1


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_exit_based_on_actual_fills(side):
    runner, x, record, events = setup(side)
    assert submit(runner, x, record)
    observed, account, _ = runner.reconcile(TICKER, record)
    x.quote[side.lower() + '_bid_dollars'] = '.994'
    assert runner.exits_for_market(TICKER, record, observed)
    assert x.submissions[-1][:4] == ('exit', side, D(9), D('.994'))
    runner.reconcile(TICKER, record)
    verified = [d for e, d in events if e == 'TWO_RULE_FILL_VERIFIED'][-1]
    assert D(verified['gross_profit_when_flat']) == D('.306')
    assert verified['fees_included'] is False


@pytest.mark.parametrize('q,price', [(1, None), (7, None), (8, '.998'), (9, '.994')])
def test_partial_fill_targets(q, price):
    value = target({'side': 'YES', 'profit': '.30'}, Receipt(D(q), D(0), D(q)*D('.96'), D(0)), GRID)
    assert value == (D(price) if price else None)


def test_saved_b_receipt_not_discarded_or_given_new_profit_goal():
    value = target({'side': 'NO', 'profit': '1.00'}, Receipt(D(9), D(0), D('5.40'), D(0)), GRID)
    assert value == D('.712')


def test_manual_pause_preserved():
    runner, x, record, _ = setup()
    runner.reconcile(TICKER, record)
    x.fill('manual', 'YES', '1', '.50', 'entry')
    with pytest.raises(Pending, match='Manual-control pause'):
        runner.reconcile(TICKER, record)
    with pytest.raises(Pending):
        runner.attempt(TICKER, record, FINAL, {}, D(1))
    assert record['manual_control_pause']['reason'] == 'new_external_fill'
    assert not x.submissions


def test_unresolved_ack_does_not_duplicate():
    runner, x, record, _ = setup(fill='0')
    assert submit(runner, x, record)
    record['trades'][0]['orders'][0].pop('order_id')
    assert not submit(runner, x, record)
    assert len(x.submissions) == 1


def test_old_loss_stays_charged_to_a_and_b_never_borrows():
    runner, x, record, _ = setup()
    record['trades'] = [{'id': 'old', 'rule': 'final_2m_50', 'orders': []}]
    observed = {'old': Receipt(D(9), D(9), D(5), D(3))}
    selected = candidate(FINAL, x.quote, None, 60)
    assert not runner.funding_allowed(TICKER, record, observed, selected, '100')


def test_cycle_has_no_b_and_no_btc_read():
    runner, x, record, _ = setup()
    runner.cycle()
    assert len(x.submissions) == 1
    assert record['trades'][0]['rule'] == FINAL.name
    assert 'btc_reference_price' not in inspect.getsource(TwoRuleBot)
    assert 'floor_strike' not in inspect.getsource(TwoRuleBot)


def test_shared_cap_still_applies_to_historical_exposure():
    runner, x, record, _ = setup()
    record['trades'] = [{'id': 'old', 'rule': 'directional_100', 'orders': []}]
    selected = candidate(FINAL, x.quote, None, 60)
    observed = {'old': Receipt(D(20), D(0), D(15), D(0))}
    assert not runner.funding_allowed(TICKER, record, observed, selected, '100')


def test_missing_ledger_not_recreated(tmp_path):
    path = tmp_path/'ledger.json'
    path.with_suffix('.initialized').touch()
    with pytest.raises(RuntimeError, match='ledger missing'):
        AtomicStore(path)
    assert not path.exists()
