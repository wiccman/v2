import ast
import copy
import sys
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

# All exchange calls below use test doubles; no credentials or live orders.
import market_maker as mm

class Client:
    def __init__(self):
        self.held = D('0'); self.bid='0.45'; self.no='0.45'
    def positions(self, ticker): return [{'ticker':ticker,'position_fp':str(self.held)}]
    def all_orders(self,*args): return []
    def balance(self): return {'balance_dollars':'20'}
    def market(self,ticker): return {'ticker':ticker,'status':'open'}
    def orderbook(self,ticker): return {'yes_dollars':[[self.bid,'100']], 'no_dollars':[[self.no,'100']]}

class Tests(unittest.TestCase):
    def setup_mm(self, elapsed=0, held='0'):
        client=Client(); client.held=D(held); now=[1000+elapsed]; state={'mm':{'markets':{'TEST':{'orders':[]}}}}
        if D(held):
            state['mm']['markets']['TEST']['orders']=[{'ticker':'TEST','order_id':'existing','side':'bid' if D(held)>0 else 'ask','filled':str(abs(D(held))),'price':'0.50','quantity':str(abs(D(held))),'fees':'0','status':'executed'}]
        saved=[]; events=[]; placed=[]
        maker=mm.MarketMaker(client,lambda s:saved.append(copy.deepcopy(s)),lambda *a,**kw:events.append((a,kw)),clock=lambda:now[0])
        maker._place=lambda *args:placed.append(args[3])
        def run(): maker.cycle(state,{'ticker':'TEST'},datetime.fromtimestamp(1900,timezone.utc))
        return client,now,state,saved,events,placed,run,maker
    def test_boundaries(self):
        for t,m in [(-1,None),(0,0),(59.9,0),(60,1),(299.9,4),(300,None),(359.9,None),(420,None),(899,None)]:
            self.assertEqual(mm.entry_minute(1000+t,1900),m)
    def test_once_per_minute_and_restart(self):
        c,n,s,saves,e,p,run,m=self.setup_mm()
        for minute in range(5):
            n[0]=1000+minute*60; run(); run()
        self.assertEqual(len(p),5)
        self.assertEqual([x[1]['details'] for x in e if x[0][0]=='MM_ENTRY_MINUTE'],[str(x) for x in range(5)])
        self.assertEqual(saves[-1]['mm']['markets']['TEST']['last_entry_minute'],4)
        restarted=mm.MarketMaker(c,lambda s:None,lambda *a,**k:None,clock=lambda:n[0]); restarted._place=lambda *args:p.append(args[3])
        restarted.cycle(copy.deepcopy(saves[-1]),{'ticker':'TEST'},datetime.fromtimestamp(1900,timezone.utc))
        self.assertEqual(len(p),5)
        n[0]=1420; run(); n[0]=1600; run(); self.assertEqual(len(p),5)
    def test_skipped_minutes_not_replayed(self):
        c,n,s,sv,e,p,run,m=self.setup_mm(245);run();self.assertEqual(len(p),1)
        self.assertEqual(s['mm']['markets']['TEST']['last_entry_minute'],4)
    def test_long_exit_after_window(self):
        c,n,s,sv,e,p,run,m=self.setup_mm(600,'2');run();self.assertEqual(p,[])
        self.assertEqual(s['mm']['markets']['TEST']['exit_anchor'],'0.55')
        c.bid='0.56';c.no='0.40';n[0]+=3;run()
        self.assertEqual(sum(q[2] for q in p[-1]),D('2'))
        self.assertTrue(all(q[0]=='ask' and q[3] for q in p[-1]))
    def test_short_exit_after_window(self):
        c,n,s,sv,e,p,run,m=self.setup_mm(600,'-2');run();self.assertEqual(p,[])
        c.bid='0.40';c.no='0.56';n[0]+=3;run()
        self.assertEqual(sum(q[2] for q in p[-1]),D('2'))
        self.assertTrue(all(q[0]=='bid' and q[3] for q in p[-1]))
    def test_partial_position_exit_capped(self):
        c,n,s,sv,e,p,run,m=self.setup_mm(600,'0.5');run();c.bid='0.56';c.no='0.40';run()
        self.assertEqual(sum(q[2] for q in p[-1]),D('0.5'))
    def test_exits_ignore_entry_cooldown(self):
        c,n,s,sv,e,p,run,m=self.setup_mm(600,'1');s['mm']['markets']['TEST'].update(pause_until=2000,exit_side='ask',exit_anchor='0.44');run();self.assertTrue(p)
    def test_finalized_rollover(self):
        c,n,s,sv,e,p,run,m=self.setup_mm()
        s['mm']['markets']['OLD']={'orders':[], 'settled':False}
        c.market=lambda t: {'status':'finalized','result':'yes'} if t=='OLD' else {'status':'open'}
        run();self.assertTrue(s['mm']['markets']['OLD']['settled']);self.assertEqual(len(p),1)
    def test_batch_wire_payload(self):
        tree=ast.parse(Path(__file__).with_name('kalshi.py').read_text())
        method=next(m for cl in tree.body if isinstance(cl,ast.ClassDef) and cl.name=='KalshiClient' for m in cl.body if isinstance(m,ast.FunctionDef) and m.name=='place_mm_batch')
        namespace={'Decimal':D};exec(compile(ast.Module(body=[method],type_ignores=[]),'wire','exec'),namespace)
        bodies=[]
        class Wire:
            def request(self,*a,**kw): bodies.append(kw['body']);return {'orders':[]}
        intents=[dict(ticker='T',side=side,quantity='1',price='0.50',expiry=2000,client_id=str(i),reduce_only=reduce) for i,(side,reduce) in enumerate([('bid',False),('ask',True),('bid',True)])]
        namespace['place_mm_batch'](Wire(),intents)
        orders=bodies[0]['orders'];self.assertEqual(orders[0]['time_in_force'],'good_till_canceled');self.assertTrue(orders[0]['post_only'])
        for o in orders[1:]:
            self.assertEqual(o['time_in_force'],'immediate_or_cancel');self.assertFalse(o['post_only']);self.assertTrue(o['reduce_only']);self.assertNotIn('expiration_time',o)

if __name__=='__main__': unittest.main()
