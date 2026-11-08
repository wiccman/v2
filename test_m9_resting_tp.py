from decimal import Decimal as D
import pytest
import single_coin_minute9 as core
from kalshi import KalshiClient
from test_single_coin_minute9 import StopExchange, fake_record, START

@pytest.mark.parametrize('side', ['YES', 'NO'])
def test_resting_before_target_no_duplicates_restart_cancel_race(tmp_path, side):
    c = StopExchange(side=side, bid='.60')
    j = core.Journal(tmp_path/'j.json')
    r = fake_record(c, fully_sold=False)
    j.data['windows'][c.ticker] = r
    core.reconcile_exit(c,j,r,START+570)
    assert len(c.submissions)==1
    assert c.submissions[0]['limit']==D('.77')
    assert r['sells'][0]['execution']=='gtc'
    j = core.Journal(j.path); r = j.data['windows'][c.ticker]
    core.reconcile_exit(c,j,r,START+571)
    assert len(c.submissions)==1
    c.bid=D('.30')
    core.reconcile_exit(c,j,r,START+572)
    assert len(c.submissions)==1 and r['stop_armed']
    # TP fills one contract during cancellation; stop must only sell one.
    c.simulate_partial_fill(quantity='1',price='.77')
    c.bid=D('.60') # Stop remains latched despite price recovery.
    core.reconcile_exit(c,j,r,START+573)
    assert len(c.submissions)==2
    assert abs(c.submissions[-1]['signed'])==1
    assert r['sells'][-1]['purpose']=='stop_loss'

@pytest.mark.parametrize('side',['YES','NO'])
def test_lost_resting_ack_never_duplicates(tmp_path,side):
    c=StopExchange(side=side,bid='.60'); c.lost_ack=True
    j=core.Journal(tmp_path/'j.json');r=fake_record(c,fully_sold=False)
    j.data['windows'][c.ticker]=r
    with pytest.raises(TimeoutError): core.reconcile_exit(c,j,r,START+570)
    core.reconcile_exit(c,j,r,START+571)
    assert len(c.submissions)==1

@pytest.mark.parametrize('qty,book,price',[(2,'ask','0.7700'),(-2,'bid','0.2300')])
def test_gtc_wire_payload(qty,book,price):
    c=object.__new__(KalshiClient); sent=[]
    c.request=lambda *a,**k: sent.append(k['body']) or {'order_id':'tp'}
    c.place_resting_take_profit('TEST',qty,D('.77'),12345,client_order_id='id')
    p=sent[0]
    assert p['time_in_force']=='good_till_canceled'
    assert p['side']==book and p['price']==price
    assert p['count']=='2' and p['expiration_time']==12345
    assert p['reduce_only'] is False


def test_cancel_failure_and_lag_never_allow_second_sell(tmp_path):
    c=StopExchange(bid='.60');j=core.Journal(tmp_path/'j.json')
    r=fake_record(c,fully_sold=False);j.data['windows'][c.ticker]=r
    core.reconcile_exit(c,j,r,START+570)
    c.bid=D('.30')
    def failed_cancel(*a): raise TimeoutError('cancel unknown')
    c.cancel=failed_cancel
    with pytest.raises(TimeoutError):core.reconcile_exit(c,j,r,START+571)
    assert r['stop_armed'] and len(c.submissions)==1
    c.cancel=lambda *a: None
    core.reconcile_exit(c,j,r,START+572)
    assert len(c.submissions)==1
    # Terminal receipt knows about a fill before fill history catches up.
    c.orders['exit-1'].update(status='canceled',fill_count_fp='1')
    core.reconcile_exit(c,j,r,START+573)
    assert len(c.submissions)==1
