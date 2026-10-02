"""Manual flip/regression tests: local fakes only, network explicitly blocked."""
from __future__ import annotations

import ast
import copy
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from two_rule_bot import TwoRuleBot, Pending
from two_rule_policy import D, FINAL, DIRECTIONAL


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network forbidden in manual-guard tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


class Store:
    def __init__(self):
        self.data = {'schema': 1, 'markets': {}}
        self.persisted = copy.deepcopy(self.data)
    def save(self):
        self.persisted = copy.deepcopy(self.data)


class Fake:
    def __init__(self, side='YES'):
        self.now, self.side, self.held = 1780.0, side, D(0)
        self.spot = D('100050' if side == 'YES' else '99950')
        self.market_data = {'ticker': 'T', 'floor_strike': '100000',
            'close_time': datetime.fromtimestamp(1900, timezone.utc).isoformat(),
            'yes_ask_dollars': '.96', 'no_ask_dollars': '.96',
            'yes_bid_dollars': '.95', 'no_bid_dollars': '.95',
            'price_ranges': [{'start': '0', 'end': '1', 'step': '.001'}]}
        self.history, self.remote, self.entries, self.sales, self.cancelled = [], {}, [], [], []
        self.entry_liquidity = D(100)
        self.market_hook = None
        self.lose_entry_ack = False
        self.cancel_failure = False
    def market(self, ticker):
        if self.market_hook:
            hook, self.market_hook = self.market_hook, None
            hook()
        return copy.deepcopy(self.market_data)
    def markets(self, **kwargs):
        return [self.market('T')]
    def btc_reference_price(self):
        return self.spot
    def market_cash(self, ticker):
        return {'cash_dollars': '100', 'exchange_index': 2}
    def positions(self, ticker):
        return [{'ticker': ticker, 'position_fp': str(self.held)}]
    def all_orders(self, ticker):
        return copy.deepcopy(list(self.remote.values()))
    def order(self, oid, ticker):
        return copy.deepcopy(self.remote[oid])
    def all_fills(self, ticker):
        return copy.deepcopy([f for f in self.history if f['ticker'] == ticker])
    def fill(self, oid, sign, q, price='.96', ticker='T'):
        q, price = D(q), D(price)
        if not q:
            return
        if ticker == 'T':
            self.held += sign * q
        self.history.append({'fill_id': 'f' + str(len(self.history)), 'order_id': oid,
            'ticker': ticker, 'book_side': 'bid' if sign > 0 else 'ask', 'ts': str(self.now),
            'count_fp': str(q), 'yes_price_dollars': str(price), 'subaccount_number': 0})
    def place_entry(self, ticker, side, quantity, price, expiration_time, **kwargs):
        oid = 'o' + str(len(self.remote))
        q = min(D(quantity), self.entry_liquidity)
        self.remote[oid] = {'order_id': oid, 'ticker': ticker,
            'client_order_id': kwargs['client_order_id'], 'fill_count_fp': str(q),
            'status': 'executed' if q == quantity else 'canceled' if kwargs['ioc'] else 'resting'}
        self.entries.append(oid)
        self.fill(oid, 1 if side == 'YES' else -1, q, price if side == 'YES' else 1-price)
        if self.lose_entry_ack:
            raise TimeoutError('Unknown entry ACK')
        return {'order_id': oid}
    def place_take_profit(self, ticker, signed_quantity, price, expiration_time, **kwargs):
        oid = 'o' + str(len(self.remote))
        side = 'YES' if signed_quantity > 0 else 'NO'
        bid = D(self.market_data[side.lower() + '_bid_dollars'])
        q = min(abs(signed_quantity), abs(self.held)) if self.held*signed_quantity > 0 and bid >= price else D(0)
        self.remote[oid] = {'order_id': oid, 'ticker': ticker,
            'client_order_id': kwargs['client_order_id'], 'fill_count_fp': str(q), 'status': 'executed'}
        self.sales.append({'quantity': abs(signed_quantity), 'price': price})
        self.fill(oid, -1 if side == 'YES' else 1, q, bid if side == 'YES' else 1-bid)
        return {'order_id': oid}
    def cancel(self, oid, ticker):
        self.cancelled.append(oid)
        if self.cancel_failure:
            raise TimeoutError('Cancel ACK lost')
        self.remote[oid]['status'] = 'canceled'
        return {'order_id': oid}


def setup(side='YES'):
    e, store, events = Fake(side), Store(), []
    bot = TwoRuleBot(e, store, clock=lambda: e.now,
                    emit=lambda event, **data: events.append((event, data)))
    return e, store, events, bot


@pytest.mark.parametrize('side,sign', [('YES', 1), ('NO', -1)])
@pytest.mark.parametrize('sold', [2, 11, 15])
def test_manual_reduction_flatten_or_flip_pauses_both_bot_routes(side, sign, sold):
    e, s, events, b = setup(side)
    b.cycle()
    e.now += 1
    e.fill('manual', -sign, sold)
    e.market_data[side.lower()+'_bid_dollars'] = '.999'
    b.cycle()
    assert s.data['markets']['T']['manual_control_pause']
    assert not e.sales and len(e.entries) == 1
    e.now += 1
    e.spot = D('100100' if side == 'YES' else '99900')
    b.cycle()
    assert len(e.entries) == 1


@pytest.mark.parametrize('side,sign', [('YES', 1), ('NO', -1)])
def test_close_rebuy_same_net_size_is_detected_and_restart_does_not_clear_pause(side, sign):
    e, s, events, b = setup(side)
    b.cycle(); e.now += 1
    e.fill('manual-close', -sign, 11); e.fill('manual-rebuy', sign, 11)
    e.market_data[side.lower()+'_bid_dollars'] = '.999'
    b.cycle()
    assert e.held == sign * 11 and not e.sales
    restored = Store(); restored.data = copy.deepcopy(s.persisted)
    TwoRuleBot(e, restored, clock=lambda:e.now, emit=lambda *a,**k:None).cycle()
    assert not e.sales and len(e.entries) == 1


@pytest.mark.parametrize('side,sign', [('YES', 1), ('NO', -1)])
def test_manual_flip_during_exit_quote_read_prevents_stale_sell(side, sign):
    e, s, events, b = setup(side)
    b.cycle(); e.now += 1
    e.market_data[side.lower()+'_bid_dollars'] = '.999'
    e.market_hook = lambda: e.fill('manual-flip', -sign, 15)
    b.cycle()
    assert e.held == -sign*4 and not e.sales
    assert s.data['markets']['T']['manual_control_pause']


def test_manual_change_during_entry_quote_recheck_blocks_purchase():
    e, s, events, b = setup()
    e.spot = D('100000'); b.cycle()
    e.spot = D('100050')
    e.market_hook = lambda: e.fill('manual', -1, 5)
    with pytest.raises(Pending):
        b.cycle()
    assert not e.entries


def test_external_working_order_is_not_canceled_and_blocks_entry():
    e, s, events, b = setup()
    e.remote['manual'] = {'ticker':'T', 'order_id':'manual', 'client_order_id':'human',
                          'status':'resting', 'fill_count_fp':'0'}
    with pytest.raises(Pending):
        b.cycle()
    assert not e.entries and not e.cancelled
    e.remote['manual']['status']='canceled'
    b.cycle()
    assert len(e.entries)==1


def test_manual_fill_cancels_only_bot_resting_entry_and_retains_reservations():
    e, s, events, b = setup()
    e.entry_liquidity=D(0); b.cycle(); e.now+=1
    e.fill('manual-buy',1,2)
    e.remote['manual']={'ticker':'T','order_id':'manual','client_order_id':'human','status':'resting'}
    b.cycle()
    rec=s.data['markets']['T']; order=rec['trades'][0]['orders'][0]
    assert e.cancelled==[e.entries[0]] and not order['terminal']
    assert rec['manual_control_pause'] and not e.sales
    assert e.remote['manual']['status']=='resting'
    e.now+=4; b.cycle()
    assert order['guard_cancel_confirmed'] and not e.sales


def test_failed_cancel_retries_without_claiming_cancellation():
    e, s, events, b = setup()
    e.entry_liquidity=D(0); b.cycle(); e.now+=1
    e.fill('manual-buy',1,2); e.cancel_failure=True
    b.cycle()
    order=s.data['markets']['T']['trades'][0]['orders'][0]
    assert not order.get('guard_cancel_confirmed') and not order['terminal']
    e.cancel_failure=False; e.now+=4; b.cycle()
    assert len(e.cancelled)==2 and not e.sales


def test_cancel_ownership_mismatch_never_cancels_external_order():
    e, s, events, b = setup()
    e.entry_liquidity=D(0); b.cycle(); e.now+=1
    e.fill('manual-buy',1,2)
    e.remote[e.entries[0]]['client_order_id']='different-owner'
    b.cycle()
    assert not e.cancelled and not e.sales


def test_lost_bot_ack_is_recovered_not_classified_as_manual():
    e, s, events, b = setup()
    e.lose_entry_ack=True
    with pytest.raises(TimeoutError):
        b.cycle()
    e.lose_entry_ack=False; b.cycle()
    assert not s.data['markets']['T'].get('manual_control_pause')
    assert len(e.entries)==1


def test_manual_activity_on_other_ticker_does_not_pause_current_contract():
    e, s, events, b = setup()
    b.cycle(); e.fill('elsewhere',-1,100,ticker='OTHER')
    e.market_data['yes_bid_dollars']='.999'; b.cycle()
    assert e.sales[0]['quantity']==11
    assert not s.data['markets']['T'].get('manual_control_pause')


def test_preexisting_manual_same_side_inventory_stays_outside_bot_exit():
    e, s, events, b = setup()
    e.fill('manual-before',1,5); b.cycle()
    e.market_data['yes_bid_dollars']='.999'; b.cycle()
    assert e.sales[0]['quantity']==11 and e.held==5


def test_upgrade_does_not_baseline_away_manual_fill_after_bot_request():
    e, s, events, b = setup()
    b.cycle(); del s.data['markets']['T']['manual_fill_baseline']; e.now+=1
    e.fill('manual-after',1,3); b.cycle()
    assert s.data['markets']['T']['manual_control_pause']


def test_manual_flatten_and_rebuy_during_guarded_exit_itself():
    e, s, events, b = setup()
    b.cycle(); e.now+=1
    rec=s.data['markets']['T']; observations,_,_=b.reconcile('T',rec)
    e.fill('manual-close',-1,11); e.fill('manual-rebuy',1,11)
    e.market_data['yes_bid_dollars']='.999'
    with pytest.raises(Pending):
        b.exits_for_market('T',rec,observations)
    assert not e.sales


def test_pending_exit_cannot_be_reused_by_other_rule():
    e, s, events, b = setup()
    b.cycle(); e.market_data['yes_bid_dollars']='.999'; b.cycle()
    sale_id=next(oid for oid in e.remote if oid not in e.entries)
    e.remote[sale_id]['status']='resting'
    rec=s.data['markets']['T']; observations,account,_=b.reconcile('T',rec)
    with pytest.raises(Pending):
        b.manual_guard.before_submit('T',rec,observations,account)
    assert len(e.sales)==1


@pytest.mark.parametrize('quantity,wire', [(D(11),'ask'), (D(-15),'bid')])
def test_real_adapter_serializes_reduce_only_ioc_for_both_sides(quantity,wire):
    # Execute only these two actual adapter methods with a recording transport;
    # never import credentials, initialization code, or the real HTTP client.
    source=ast.parse(Path(__file__).with_name('kalshi.py').read_text())
    cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='KalshiClient')
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in {'_order','place_take_profit'}]
    assert len(methods)==2
    shell=ast.ClassDef(name='Adapter',bases=[],keywords=[],body=methods,decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[shell],type_ignores=[]))
    ns={'Decimal':D,'uuid':uuid}; exec(compile(module,'<isolated-adapter>','exec'),ns)
    adapter=ns['Adapter'](); sent=[]
    adapter.request=lambda *a,**kw: sent.append(kw['body']) or {'order_id':'test'}
    adapter.place_take_profit('T',quantity,D('.997'),1900,client_order_id='test-client')
    assert sent[0]['reduce_only'] is True and sent[0]['time_in_force']=='immediate_or_cancel'
    assert sent[0]['side']==wire and D(sent[0]['count'])==abs(quantity)
