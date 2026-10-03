"""Offline $10/$10 budget tests; fake exchange only, all sockets blocked."""
import copy
import json
import socket

import pytest
import two_rule_bot as module
from two_rule_bot import AtomicStore, Pending, TwoRuleBot, budget_config
from two_rule_policy import D, FINAL, DIRECTIONAL, Receipt, Signal, candidate, target


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network forbidden in budget tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


class Store:
    def __init__(self):
        self.data = {'schema': 1, 'markets': {}}
    def save(self):
        self.persisted = copy.deepcopy(self.data)


class Fake:
    def __init__(self, side='YES', ask='.60', left=900):
        self.now, self.close, self.side = 1000, 1000 + left, side
        self.ask, self.bid, self.held, self.liquidity = D(ask), D('.59'), D(0), D(15)
        self.cash, self.lose_ack = D('100'), False
        self.remote, self.history, self.buys, self.sells = {}, [], [], []
    def market(self, ticker):
        return {'ticker': ticker, 'floor_strike': '100000',
                'yes_ask_dollars': str(self.ask), 'no_ask_dollars': str(self.ask),
                'yes_bid_dollars': str(self.bid), 'no_bid_dollars': str(self.bid),
                'price_ranges': [{'start': '0', 'end': '1', 'step': '.001'}]}
    def btc_reference_price(self):
        return D('100100' if self.side == 'YES' else '99900')
    def market_cash(self, ticker):
        return {'cash_dollars': str(self.cash)}
    def all_orders(self, ticker):
        return copy.deepcopy(list(self.remote.values()))
    def order(self, oid, ticker):
        return copy.deepcopy(self.remote[oid])
    def all_fills(self, ticker):
        return copy.deepcopy(self.history)
    def positions(self, ticker):
        return [{'ticker': ticker, 'position_fp': str(self.held)}]
    def _record(self, side, q, price, client_id, role):
        oid = 'o' + str(len(self.remote))
        sign = (1 if side == 'YES' else -1) * (1 if role == 'entry' else -1)
        self.held += sign * q
        self.remote[oid] = {'order_id': oid, 'ticker': 'T', 'client_order_id': client_id,
                            'status': 'canceled', 'fill_count_fp': str(q)}
        if q:
            self.history.append({'fill_id': 'f'+oid, 'ticker': 'T', 'order_id': oid,
                'count_fp': str(q), 'book_side': 'bid' if sign > 0 else 'ask',
                'yes_price_dollars': str(price if side == 'YES' else 1-price)})
        return {'order_id': oid}
    def place_entry(self, ticker, side, quantity, price, close, **kwargs):
        self.buys.append((side, quantity, price, kwargs))
        response = self._record(side, min(quantity, self.liquidity), price, kwargs['client_order_id'], 'entry')
        if self.lose_ack:
            raise TimeoutError('Lost acknowledgement')
        return response
    def place_take_profit(self, ticker, signed, price, close, **kwargs):
        self.sells.append((signed, price))
        return self._record('YES' if signed > 0 else 'NO', abs(signed), price,
                            kwargs['client_order_id'], 'exit')


def setup(side='YES', ask='.60', left=900, budget='20', store=None):
    client, store, events = Fake(side, ask, left), store or Store(), []
    bot = TwoRuleBot(client, store, budget=budget, clock=lambda: client.now,
                     emit=lambda event, **kw: events.append((event, kw)))
    record = store.data['markets'].setdefault('T', {'close': client.close, 'trades': []})
    return bot, client, record, events


def selected(rule=DIRECTIONAL, ask='.60', quantity=None):
    return Signal(rule.name, 'YES', D(ask), quantity or rule.contracts, rule.profit)


def accounting(rule, cost, proceeds='0', entered='0', sold='0', orders=None):
    trade = {'id': 'prior', 'rule': rule.name, 'side': 'YES', 'profit': str(rule.profit), 'orders': orders or []}
    return {'trades': [trade]}, {'prior': Receipt(D(entered), D(sold), D(cost), D(proceeds))}


def test_config_reports_no_borrowing():
    assert budget_config()['rule_budgets'] == {FINAL.name: '10', DIRECTIONAL.name: '10'}
    assert budget_config()['market_budget'] == '20'
    assert budget_config()['cross_rule_borrowing'] is False


@pytest.mark.parametrize('side', ['YES', 'NO'])
@pytest.mark.parametrize('left', [900, 500, 120, 1])
def test_b_can_buy_any_open_time_within_own_budget(side, left):
    bot, client, record, events = setup(side=side, left=left)
    assert bot.attempt('T', record, DIRECTIONAL, {}, D(0))
    assert client.buys[0][:3] == (side, D(15), D('.60'))
    assert client.buys[0][3]['ioc'] is True
    observed, _, _ = bot.reconcile('T', record)
    assert bot.exposure(record, observed) == D('9.45')
    assert not bot.attempt('T', record, DIRECTIONAL, observed, client.held)
    assert len(client.buys) == 1


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_a_current_size_conflict_blocks_without_creating_intent(side):
    bot, client, record, events = setup(side, '.96', 120)
    assert not bot.attempt('T', record, FINAL, {}, D(0))
    assert not client.buys and not record['trades']
    event, detail = events[-1]
    assert event == 'TWO_RULE_BUDGET_SIZE_CONFLICT'
    assert detail['required'] == '10.89' and detail['requested_quantity'] == 11
    assert detail['rule_budget'] == '10'


@pytest.mark.parametrize('ask,allowed', [('.6366', True), ('.6367', False), ('.64', False), ('.96', False), ('.999', False)])
def test_b_size_boundary_keeps_fifteen_contracts(ask, allowed):
    bot, client, record, events = setup(ask=ask)
    assert bot.attempt('T', record, DIRECTIONAL, {}, D(0)) is allowed
    assert len(client.buys) == int(allowed)
    if allowed:
        assert client.buys[0][1] == 15
    else:
        assert not record['trades']


@pytest.mark.parametrize('rule', [FINAL, DIRECTIONAL])
def test_accounting_does_not_borrow_other_partition(rule):
    bot, _, _, events = setup()
    record, observed = accounting(rule, '1')
    assert not bot.funding_allowed('T', record, observed, selected(rule, '.60', 15), '100')
    assert events[-1][1]['reason'] == 'rule_partition_exhausted'
    assert D(events[-1][1]['market_used']) == 1


def test_a_loss_does_not_spend_b_partition():
    bot, _, _, _ = setup()
    record, observed = accounting(FINAL, '10', '0')
    assert bot.funding_allowed('T', record, observed, selected(), '100')


@pytest.mark.parametrize('proceeds,allowed', [('0', False), ('1', False), ('1.50', True), ('50', True)])
def test_only_own_verified_sale_principal_releases_own_budget(proceeds, allowed):
    bot, _, _, _ = setup()
    record, observed = accounting(DIRECTIONAL, '2', proceeds)
    assert bot.funding_allowed('T', record, observed, selected(), '100') is allowed


def test_other_rule_profit_cannot_pay_this_rule_loss():
    bot, _, _, _ = setup()
    record, observed = accounting(DIRECTIONAL, '1')
    record['trades'].append({'id': 'profit', 'rule': FINAL.name, 'orders': []})
    observed['profit'] = Receipt(D(0), D(0), D(1), D(100))
    assert not bot.funding_allowed('T', record, observed, selected(), '100')


@pytest.mark.parametrize('confirmed,entered,cost,terminal,used', [
    ('0', '0', '0', False, '9.45'), ('5', '5', '3', False, '9.45'),
    ('5', '5', '3', True, '3.15'), ('0', '0', '0', True, '0')])
def test_pending_and_partial_accounting(confirmed, entered, cost, terminal, used):
    bot, _, _, _ = setup()
    order = {'role': 'entry', 'quantity': '15', 'price': '.60', 'confirmed': confirmed, 'terminal': terminal}
    record, observed = accounting(DIRECTIONAL, cost, entered=entered, orders=[order])
    assert bot.exposure(record, observed) == D(used)
    assert bot.funding_allowed('T', record, observed, selected(), '100') is (D(used)+D('9.45') <= 10)


def test_smaller_shared_budget_still_enforced():
    bot, _, record, events = setup(budget='8')
    assert not bot.funding_allowed('T', record, {}, selected(), '100')
    assert events[-1][1]['reason'] == 'shared_market_budget_exhausted'


def test_shared_cap_applies_to_preexisting_over_partition_inventory():
    bot, _, _, events = setup()
    record, observed = accounting(FINAL, '11')
    assert not bot.funding_allowed('T', record, observed, selected(), '100')
    assert events[-1][1]['reason'] == 'shared_market_budget_exhausted'


@pytest.mark.parametrize('cash', ['0', '9.4499'])
def test_insufficient_actual_cash(cash):
    bot, _, record, events = setup()
    assert not bot.funding_allowed('T', record, {}, selected(), cash)
    assert events[-1][1]['reason'] == 'insufficient_market_cash'


@pytest.mark.parametrize('cash', ['NaN', 'Infinity', '-1'])
def test_invalid_cash_fails_closed(cash):
    bot, _, record, _ = setup()
    with pytest.raises(ValueError):
        bot.funding_allowed('T', record, {}, selected(), cash)


def test_unknown_rule_ownership_fails_closed():
    bot, _, _, _ = setup()
    with pytest.raises(Pending):
        bot.funding_allowed('T', {'trades': [{'rule': 'retired'}]}, {}, selected(), '100')


def test_restart_does_not_reset_partition_charges(tmp_path):
    store = AtomicStore(tmp_path/'state.json')
    bot, _, _, _ = setup(store=store)
    record, observed = accounting(DIRECTIONAL, '1')
    store.data['markets']['T'] = dict(record, close=1900)
    store.save()
    saved = (tmp_path/'state.json').read_bytes()
    restarted, _, record, _ = setup(store=AtomicStore(tmp_path/'state.json'))
    assert not restarted.funding_allowed('T', record, observed, selected(), '100')
    assert (tmp_path/'state.json').read_bytes() == saved


def test_lost_ack_recovers_without_duplicate():
    bot, client, record, _ = setup()
    client.lose_ack = True
    with pytest.raises(TimeoutError):
        bot.attempt('T', record, DIRECTIONAL, {}, D(0))
    observed, held, _ = bot.reconcile('T', record)
    assert not bot.attempt('T', record, DIRECTIONAL, observed, held)
    assert len(client.buys) == 1


@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_exit_target_unchanged_and_exits_do_not_use_entry_partition_gate(side):
    bot, client, record, _ = setup(side)
    assert bot.attempt('T', record, DIRECTIONAL, {}, D(0))
    observed, _, _ = bot.reconcile('T', record)
    def forbidden(*args, **kwargs):
        raise AssertionError('Exit must not require fresh entry budget')
    bot.funding_allowed = forbidden
    client.bid = D('.70')
    assert bot.exits_for_market('T', record, observed)
    assert client.sells == [(D(15) if side == 'YES' else D(-15), D('.667'))]


def test_rule_signals_and_profit_targets_not_resized():
    assert FINAL.contracts == 11 and FINAL.profit == D('.40')
    assert DIRECTIONAL.contracts == 15 and DIRECTIONAL.profit == 1
    assert DIRECTIONAL.exact_ask is None and DIRECTIONAL.last_seconds is None
    market = Fake(ask='.96').market('T')
    assert candidate(DIRECTIONAL, market, '100100', 900) is not None
    assert target({'side': 'YES', 'profit': '.40'}, Receipt(D(10), D(0), D('9.60'), D(0)), market['price_ranges']) is None


def test_locked_start_reports_split_without_live_import(monkeypatch, capsys):
    monkeypatch.setenv('TRADING_ENABLED', 'false')
    monkeypatch.setenv('MARKET_BUDGET_DOLLARS', '20')
    monkeypatch.setattr(module.time, 'sleep', lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr('sys.argv', ['two_rule_bot.py', '--live'])
    with pytest.raises(KeyboardInterrupt):
        module.main()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]['rule_budgets'] == {FINAL.name: '10', DIRECTIONAL.name: '10'}
    assert rows[0]['trading_enabled'] is False
    assert rows[1]['event'] == 'TWO_RULE_LOCKED'
