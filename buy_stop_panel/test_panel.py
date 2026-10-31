import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from decimal import Decimal as D
from http.server import ThreadingHTTPServer

from app import Panel, PublicData, handler, outcome_book, paper_fill, stamp

TICKER='KXBTC15M-26OCT311430-30'
NOW=1_800_000_000_000


def book(ask='.55',qty='10',start=NOW):
    ask=D(ask)
    return {'sides':{'yes':[(ask-D('.01'),D(qty))],
                     'no':[(1-ask,D(qty))]},'start_ms':start,'received_ms':start+10}


class FakeData:
    def __init__(self):
        self.current=book()
        self.error=False
        self.status='active'
        self.closes=NOW+100000
    def market(self,ticker):
        return {'ticker':ticker,'status':self.status,
                'close_time':datetime.fromtimestamp(self.closes/1000,timezone.utc).isoformat()}
    def book(self,ticker):
        if self.error:
            raise TimeoutError('synthetic failure')
        return self.current
    def markets(self,coin):
        return [{'ticker':TICKER,'title':'Fixture market','close_ms':self.closes}]


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.clock=patch('app.stamp',return_value=NOW+20)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.api=FakeData()
        self.panel=Panel(self.api)
        self.sid=self.panel.session()
        self.data={'ticker':TICKER,'side':'YES','trigger_cents':'60','cap_cents':'62','quantity':'10'}
    def arm(self):
        return self.panel.arm(self.sid,self.data)['id']
    def cross(self,rid):
        self.panel.process(rid,book('.55'))
        self.panel.process(rid,book('.60'))
    def record(self,rid):
        return self.panel.records[rid]

    def test_arm_waits_for_below(self):
        rid=self.arm()
        self.panel.process(rid,book('.61'))
        self.assertEqual(self.record(rid)['state'],'WAITING')

    def test_full_flow_needs_second_snapshot(self):
        rid=self.arm()
        self.cross(rid)
        self.assertEqual(self.record(rid)['state'],'PENDING')
        self.panel.process(rid,book('.61'))
        self.assertIsNone(self.record(rid)['fill'])
        with patch('app.stamp',return_value=NOW+1000):
            self.panel.process(rid,book('.61',start=NOW+900))
        self.assertEqual(self.record(rid)['state'],'PAPER_FILLED')
        self.assertEqual(self.record(rid)['fill']['average_cents'],'61.00')

    def test_gap_skips(self):
        rid=self.arm()
        self.panel.process(rid,book('.55'))
        self.panel.process(rid,book('.70'))
        self.assertEqual(self.record(rid)['state'],'SKIPPED')

    def test_latency_gap_no_fill(self):
        rid=self.arm()
        self.cross(rid)
        with patch('app.stamp',return_value=NOW+1000):
            self.panel.process(rid,book('.70',start=NOW+900))
        self.assertEqual(self.record(rid)['state'],'NO_FILL')
        self.assertEqual(self.record(rid)['fill']['resting'],'0')

    def test_fractional_partial_fill(self):
        rid=self.arm()
        self.cross(rid)
        with patch('app.stamp',return_value=NOW+1000):
            self.panel.process(rid,book('.61',qty='2.50',start=NOW+900))
        self.assertEqual(self.record(rid)['fill']['filled'],'2.50')
        self.assertEqual(self.record(rid)['fill']['unfilled'],'7.50')

    def test_feed_failure_disarms(self):
        rid=self.arm()
        self.panel.process(rid,book('.55'))
        self.panel.process(rid,error=True)
        self.panel.process(rid,book('.60'))
        self.assertEqual(self.record(rid)['state'],'WAITING')

    def test_pending_failure_no_assumed_fill(self):
        rid=self.arm();self.cross(rid)
        self.panel.process(rid,error=True)
        self.assertEqual(self.record(rid)['state'],'MISSED')
        self.assertIsNone(self.record(rid)['fill'])

    def test_stale_book(self):
        rid=self.arm()
        self.panel.process(rid,book('.55'))
        self.panel.process(rid,book('.60',start=NOW-3000))
        self.assertEqual(self.record(rid)['state'],'WAITING')

    def test_future_book(self):
        rid=self.arm()
        self.panel.process(rid,book('.55'))
        self.panel.process(rid,book('.60',start=NOW+500))
        self.assertEqual(self.record(rid)['state'],'WAITING')

    def test_expired_trigger_even_during_outage(self):
        rid=self.arm()
        with patch('app.stamp',return_value=NOW+100001):
            self.panel.process(rid,error=True)
        self.assertEqual(self.record(rid)['state'],'EXPIRED')

    def test_duplicate_arm_rejected(self):
        self.arm()
        with self.assertRaises(ValueError):self.arm()

    def test_cancel_stops_pending(self):
        rid=self.arm();self.cross(rid)
        self.panel.cancel(self.sid,rid)
        with patch('app.stamp',return_value=NOW+1000):
            self.panel.process(rid,book('.61',start=NOW+900))
        self.assertEqual(self.record(rid)['state'],'CANCELED')
        self.assertIsNone(self.record(rid)['fill'])

    def test_session_isolation(self):
        rid=self.arm();sid2=self.panel.session()
        self.assertEqual(self.panel.view(sid2)['records'],[])
        with self.assertRaises(ValueError):self.panel.cancel(sid2,rid)
        self.assertNotIn('owner',self.panel.view(self.sid)['records'][0])

    def test_terminal_does_not_refill(self):
        rid=self.arm();self.cross(rid)
        with patch('app.stamp',return_value=NOW+1000):
            self.panel.process(rid,book('.61',start=NOW+900))
            self.panel.process(rid,book('.60',start=NOW+900))
        self.assertEqual(self.record(rid)['fill']['average_cents'],'61.00')

    def test_restart_cancels_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            a=Panel(self.api,p);sid=a.session();a.arm(sid,self.data)
            b=Panel(self.api,p)
            self.assertEqual(b.view(sid)['records'][0]['state'],'CANCELED')
            self.assertFalse(b.engines)

    def test_inputs_reject_invalid_limits(self):
        for k,v in [('cap_cents','59'),('trigger_cents','NaN'),('quantity','0'),
                    ('quantity','1.5'),('side','BOTH'),('ticker','../../portfolio/orders')]:
            with self.subTest(k=k,v=v),self.assertRaises((ValueError,ArithmeticError)):
                self.panel.arm(self.sid,{**self.data,k:v})

    def test_closed_market_rejected(self):
        self.api.status='closed'
        with self.assertRaises(ValueError):self.arm()

    def test_extra_live_mode_field_rejected(self):
        with self.assertRaises(ValueError):self.panel.arm(self.sid,{**self.data,'live':True})

    def test_no_side_uses_opposite_book(self):
        b=book('.40')
        bid,ask,_=outcome_book(b,'NO')
        self.assertEqual((bid,ask),(D('.60'),D('.61')))

    def test_empty_book_not_a_signal(self):
        rid=self.arm()
        b=book();b['sides']['no']=[]
        self.panel.process(rid,b)
        self.assertEqual(self.record(rid)['state'],'WAITING')

    def test_cost_and_cap_across_levels(self):
        r=paper_fill([(D('.60'),D('3')),(D('.62'),D('2')),(D('.70'),D('10'))],D('.62'),10)
        self.assertEqual(r['filled'],'5')
        self.assertEqual(r['cost_dollars'],'3.04')
        self.assertEqual(r['average_cents'],'60.800')

    def test_worker_uses_public_book(self):
        rid=self.arm();self.panel.cycle()
        self.assertEqual(self.record(rid)['state'],'ARMED')

    def test_public_transport_rejects_trading_routes(self):
        a=PublicData()
        for path in ['/portfolio/events/orders','https://evil.invalid','/markets/../../portfolio/orders']:
            with self.assertRaises(ValueError):a.get(path)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.panel=Panel(FakeData())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.panel))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        c.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers or {})
        r=c.getresponse();status=r.status;headers=dict(r.getheaders());payload=r.read();c.close()
        return status,headers,payload
    def test_health(self):
        status,_,body=self.request('GET','/health')
        self.assertEqual(status,200);self.assertFalse(json.loads(body)['can_trade'])
    def test_cookie_httponly(self):
        _,h,_=self.request('GET','/api/state')
        self.assertIn('HttpOnly',h['Set-Cookie'])
        self.assertIn('SameSite=Strict',h['Set-Cookie'])
    def test_unknown_order_route(self):
        s,_,_=self.request('POST','/api/live-order',{})
        self.assertEqual(s,404)
    def test_csrf_rejected(self):
        s,_,_=self.request('POST','/api/arm',{},headers={'Origin':'https://evil.invalid','X-Paper-Panel':'1'})
        self.assertEqual(s,403)
    def test_static_page(self):
        s,_,body=self.request('GET','/')
        self.assertEqual(s,200)
        self.assertIn(b'Arm paper buy stop',body)
        self.assertIn(b'No real orders can be sent',body)


if __name__=='__main__':unittest.main(verbosity=2)
