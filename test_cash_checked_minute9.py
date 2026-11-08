"""Cash/rejection regressions: mocked exchange only, no credentials or network."""
import copy
from decimal import Decimal as D

import pytest
import cash_checked_minute9 as safe
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from kalshi import KalshiAPIError
from test_minute9_queue import QueueExchange
from test_single_coin_minute9 import START, StopExchange, fake_record


class CashExchange(QueueExchange):
    def __init__(self, outcomes=None, payload=None):
        super().__init__(outcomes)
        self.payload = payload if payload is not None else {
            'balance':1600, 'balance_dollars':'16.00',
            'balance_breakdown':[{'exchange_index':0,'balance':'6.00'},
                                 {'exchange_index':2,'balance':'10.00'}]}
        self.balance_calls = 0
        self.failure = None
        self.read_failure = None

    def market(self, ticker):
        value = super().market(ticker)
        value['exchange_index'] = 2
        return value

    def balance(self):
        self.balance_calls += 1
        if self.read_failure:
            raise self.read_failure
        return copy.deepcopy(self.payload)

    def place_entry(self, *args, **kwargs):
        if self.failure:
            self.posted.append((args[0],args[1],args[2],args[3]))
            raise self.failure
        return super().place_entry(*args, **kwargs)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now, events = [START+550.], []
    monkeypatch.setattr(core.time,'time',lambda:now[0])
    monkeypatch.setattr(core,'event',lambda name,**data:events.append((name,data)))
    j=core.Journal(tmp_path/'cash.json')
    with safe.installed_guard():
        yield j,now,events


def run(c,j,now,live=True):
    return safe.run_cash_checked_pass(c,j,c.series,D(10),100,live,now[0])


def test_aggregate_cash_can_dispatch_even_when_market_breakdown_is_low(setup):
    j,now,events=setup
    c=CashExchange(payload={'balance':1600,'balance_dollars':'16.00',
                           'balance_breakdown':[{'exchange_index':0,'balance':'16.00'},
                                                {'exchange_index':2,'balance':'0.00'}]})
    run(c,j,now)
    assert len(c.posted)==1 and c.balance_calls==1
    assert core.window_entry_count(j,START*1000)==1
    check=next(d for n,d in events if n=='M9_CASH_CHECK')
    assert check['cash_basis']=='aggregate_available_cash'
    assert check['total_cash_dollars']=='16.00'
    assert check['market_cash_dollars']=='0.00'


def test_observed_twenty_five_dollars_with_one_dollar_shard_dispatches(setup):
    j,now,events=setup
    c=CashExchange(payload={'balance':2588,'balance_dollars':'25.8800',
                           'balance_breakdown':[{'exchange_index':0,'balance':'24.8011'},
                                                {'exchange_index':2,'balance':'1.0789'}]})
    run(c,j,now)
    assert len(c.posted)==1
    check=next(d for n,d in events if n=='M9_CASH_CHECK')
    assert check['cash_basis']=='aggregate_available_cash'
    assert check['total_cash_dollars']=='25.8800'
    assert check['market_cash_dollars']=='1.0789'
    assert D(check['required_with_fee_reserve_dollars']) < D(check['total_cash_dollars'])


def test_funded_shard_preserves_budget_quantity_side_and_price(setup):
    j,now,_=setup
    c=CashExchange()
    run(c,j,now)
    assert len(c.posted)==1
    ticker,side,qty,limit=c.posted[0]
    assert side=='YES' and qty==12 and limit==D('.75')
    assert qty*(limit+core.FEE_RESERVE)<=D(10)
    assert j.data['windows'][ticker]['allocated_dollars']=='10'
    assert core.window_entry_count(j,START*1000)==1


def test_explicit_insufficient_rejection_is_terminal_without_immediate_backup(setup):
    j,now,events=setup
    c=CashExchange({'KXBTC15M':'api_error'})
    for _ in range(3):j.data['streak']=core.next_streak(j.data['streak'],'win')
    before=copy.deepcopy(j.data['streak'])
    run(c,j,now)
    assert len(c.posted)==1 and c.posted[0][2]==4
    r=j.data['windows'][c.posted[0][0]]
    assert r['entry_no_order_proof']['kind']=='exchange_rejected'
    assert r['outcome']['reason']=='insufficient_balance'
    assert r['outcome']['counted'] is True
    assert core.window_entry_count(j,START*1000)==0
    assert j.data['streak']==before
    assert 'private raw' not in str(events)
    now[0]+=2
    reopened=core.Journal(j.path)
    run(c,reopened,now)
    assert [x[0].split('-')[0] for x in c.posted]==['KXBTC15M','KXETH15M']
    assert core.window_entry_count(reopened,START*1000)==1
    assert not any(n=='M9_BUY_ACK_UNRESOLVED' and d['ticker']==r['ticker'] for n,d in events)


@pytest.mark.parametrize('error',[
    TimeoutError('do-not-log-token'),KalshiAPIError(500,'do-not-log-token',code='insufficient_balance'),
    KalshiAPIError(400,'do-not-log-token',code='other_error'),
    KalshiAPIError(429,'do-not-log-token',code='rate_limit'),
    KalshiAPIError(403,'do-not-log-token',code='insufficient_balance')])
def test_unknown_errors_never_release_slot(setup,error):
    j,now,events=setup
    c=CashExchange();c.failure=error
    with pytest.raises(type(error)):run(c,j,now)
    assert len(c.posted)==1 and core.window_entry_count(j,START*1000)==1
    r=next(iter(j.data['windows'].values()))
    assert 'entry_no_order_proof' not in r and 'outcome' not in r
    assert 'do-not-log-token' not in str(events)


@pytest.mark.parametrize('payload',[
    {}, {'balance':True,'balance_breakdown':[]},
    {'balance':1600,'balance_dollars':'15.00','balance_breakdown':[]},
])
def test_unverifiable_total_cash_fails_closed_without_post(setup,payload):
    j,now,_=setup
    c=CashExchange(payload=payload)
    run(c,j,now)
    assert not c.posted
    assert next(iter(j.data['windows'].values()))['outcome']['reason']=='cash_check_unavailable'
    assert core.window_entry_count(j,START*1000)==0


@pytest.mark.parametrize('payload',[
    {'balance':1600},
    {'balance':1600,'balance_breakdown':'malformed'},
    {'balance':1600,'balance_breakdown':[{'exchange_index':0,'balance':'16.00'}]},
    {'balance':1600,'balance_breakdown':[{'exchange_index':2,'balance':'NaN'}]},
    {'balance':1600,'balance_breakdown':[{'exchange_index':2,'balance':'-1'}]},
    {'balance':1600,'balance_breakdown':[{'exchange_index':2,'balance':'1'}, {'exchange_index':2,'balance':'2'}]},
])
def test_breakdown_is_diagnostic_and_does_not_block_valid_total(setup,payload):
    j,now,_=setup
    c=CashExchange(payload=payload)
    run(c,j,now)
    assert len(c.posted)==1
    assert core.window_entry_count(j,START*1000)==1


def test_cash_read_error_is_not_order_acceptance_uncertainty(setup):
    j,now,events=setup
    c=CashExchange();c.read_failure=TimeoutError('secret header')
    run(c,j,now)
    assert not c.posted and c.balance_calls==1
    assert core.window_entry_count(j,START*1000)==0
    assert 'secret header' not in str(events)


def test_cash_preflight_never_extends_entry_window(setup):
    j,now,_=setup
    class SlowCash(CashExchange):
        def balance(self):
            result=super().balance();now[0]=START+601
            return result
    c=SlowCash();run(c,j,now)
    assert not c.posted
    assert next(iter(j.data['windows'].values()))['outcome']['reason']=='entry_deadline_passed'


def test_insufficient_aggregate_cash_never_partial_funds_or_increases_budget(setup):
    j,now,_=setup
    c=CashExchange(payload={'balance':800,'balance_breakdown':[{'exchange_index':2,'balance':'8.00'}]})
    run(c,j,now)
    assert not c.posted
    record=next(iter(j.data['windows'].values()))
    assert record['quantity']=='12'
    assert record['outcome']['reason']=='insufficient_total_cash'
    assert safe.release_proven_no_order(record)
    assert core.STOP_BID==D('.45') and core.PROFIT==D('.05')


def test_rejection_proof_cannot_release_existing_or_mismatched_order(setup):
    j,now,_=setup
    c=CashExchange({'KXBTC15M':'api_error'});run(c,j,now)
    r=next(iter(j.data['windows'].values()))
    for key,value in [('client_id','wrong'),('ticker','wrong'),('http_status',500),('api_code','wrong')]:
        altered=copy.deepcopy(r);altered['entry_no_order_proof'][key]=value
        assert not safe.release_proven_no_order(altered)
    identified=copy.deepcopy(r);identified['buy']['order_id']='possibly-filled'
    assert not safe.release_proven_no_order(identified)
    unknown={'buy':{'client_id':'x'},'ticker':'x','window_start_ms':START*1000,'sells':[]}
    assert not safe.release_proven_no_order(unknown)


def test_preview_has_no_cash_reads_or_writes(setup):
    j,now,_=setup;c=CashExchange()
    before=j.path.read_text();run(c,j,now,live=False)
    assert c.balance_calls==0 and c.posted==[] and j.path.read_text()==before


def test_owned_exits_continue_even_when_cash_reads_fail(setup):
    j,now,_=setup
    c=StopExchange(bid='.44')
    c.series=()
    def no_cash():raise TimeoutError('private')
    c.balance=no_cash
    record=fake_record(c,fully_sold=False)
    j.data['windows'][c.ticker]=record
    run(c,j,now)
    assert len(c.submissions)==1 and record['sells'][0]['purpose']=='stop_loss'


def test_entry_guard_requires_matching_durable_identity(setup):
    j,now,_=setup;c=CashExchange()
    proxy=safe.CashCheckedClient(c,j,safe.CashState())
    with pytest.raises(RuntimeError):
        proxy.place_entry('missing','YES',1,D('.75'),START+900,client_order_id='x',ioc=True)
    assert not c.posted and c.balance_calls==0


def test_cash_units_are_explicit():
    assert safe.cash_snapshot({'balance':1600,'balance_breakdown':[{'exchange_index':2,'balance':'1.25'}]})==(D(16),{2:D('1.25')})
    assert safe.cash_snapshot({'balance_dollars':'0','balance_breakdown':[]})==(D(0),{})


def test_adapter_does_not_erase_performance_fallback_or_leak_overrides():
    old_slot,old_pass=core.entry_slot_released,ranked._CORE_RUN_PASS
    with safe.installed_guard():
        assert ranked._CORE_RUN_PASS is safe.run_cash_checked_pass
    assert ranked._CORE_RUN_PASS is old_pass and core.entry_slot_released is old_slot
