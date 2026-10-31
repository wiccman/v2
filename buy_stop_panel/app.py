"""Paper-only web panel. Outbound access is restricted to public market GETs.

No API keys, trading client, authenticated requests, or real order route exists.
Run: python app.py. Python 3.12+, standard library only.
"""
import concurrent.futures
from datetime import datetime, timezone
from decimal import Decimal as D
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from trigger import BuyStop, Settings, Quote

ROOT = Path(__file__).parent
SERIES = {'BTC': 'KXBTC15M', 'ETH': 'KXETH15M', 'SOL': 'KXSOL15M',
          'XRP': 'KXXRP15M', 'HYPE': 'KXHYPE15M'}
TICKER = re.compile(r'^[A-Z0-9]{2,32}15M-[A-Z0-9-]{1,70}$')
ACTIVE = {'WAITING', 'ARMED', 'PENDING'}


def stamp():
    return int(time.time() * 1000)


def close_ms(market):
    return int(datetime.fromisoformat(market['close_time'].replace('Z', '+00:00')).timestamp() * 1000)


def decimal(value):
    x = D(str(value))
    if not x.is_finite():
        raise ValueError('A finite number is required')
    return x


class PublicData:
    """No credentials are loaded. The only possible request method is GET."""
    BASE = 'https://external-api.kalshi.com/trade-api/v2'

    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, path, params=None, ttl=0):
        if not (path == '/markets' or re.fullmatch(r'/markets/[A-Z0-9-]+(?:/orderbook)?', path)):
            raise ValueError('Only public market-data routes are allowed')
        query = '?' + urlencode(params) if params else ''
        key = path + query
        with self.lock:
            old = self.cache.get(key)
            if old and time.monotonic() - old[0] < ttl:
                return old[1]
        start = stamp()
        req = Request(self.BASE + key, headers={'Accept': 'application/json',
                      'User-Agent': 'BuyStopPaperPanel/1.0'}, method='GET')
        with urlopen(req, timeout=4) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('Market-data response too large')
        result = (json.loads(body), start, stamp())
        with self.lock:
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[key] = (time.monotonic(), result)
        return result

    def markets(self, coin):
        if coin not in SERIES:
            raise ValueError('Choose BTC, ETH, SOL, XRP, or HYPE')
        payload, _, _ = self.get('/markets', {'series_ticker': SERIES[coin],
                                 'status': 'open', 'limit': 100}, ttl=5)
        result = []
        for m in payload.get('markets', []):
            try:
                end = close_ms(m)
                if m.get('status') in ('active', 'open') and end > stamp():
                    result.append({'ticker': m['ticker'], 'title': m.get('title', m['ticker']),
                                   'close_ms': end, 'subtitle': m.get('subtitle', '')})
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda m: m['close_ms'])

    def market(self, ticker):
        validate_ticker(ticker)
        return self.get('/markets/' + ticker, ttl=1)[0]['market']

    def book(self, ticker):
        validate_ticker(ticker)
        payload, start, end = self.get('/markets/' + ticker + '/orderbook')
        raw = payload.get('orderbook_fp')
        if not isinstance(raw, dict):
            raise ValueError('Unsupported order-book response')
        sides = {}
        for name in ('yes', 'no'):
            levels = []
            for row in raw.get(name + '_dollars', []):
                price, qty = decimal(row[0]), decimal(row[1])
                if not D('0') <= price <= D('1') or qty < 0:
                    raise ValueError('Invalid order-book level')
                if qty:
                    levels.append((price, qty))
            sides[name] = sorted(levels, reverse=True)
        return {'sides': sides, 'start_ms': start, 'received_ms': end}


def validate_ticker(ticker):
    if not isinstance(ticker, str) or not TICKER.fullmatch(ticker):
        raise ValueError('Enter an exact Kalshi 15-minute market ticker')


def outcome_book(book, side):
    bids = book['sides'][side.lower()]
    opposite = book['sides']['no' if side == 'YES' else 'yes']
    asks = sorted((D('1') - price, qty) for price, qty in opposite)
    return (bids[0][0] if bids else None, asks[0][0] if asks else None, asks)


def paper_fill(asks, cap, quantity):
    remaining, cost = D(quantity), D('0')
    for price, size in sorted(asks):
        if price > cap or not remaining:
            break
        n = min(size, remaining)
        remaining -= n
        cost += price * n
    filled = D(quantity) - remaining
    return {'filled': str(filled), 'unfilled': str(remaining), 'resting': '0',
            'average_cents': str(cost / filled * 100) if filled else None,
            'cost_dollars': str(cost), 'fees_included': False,
            'kind': 'displayed_liquidity_estimate'}


class Panel:
    def __init__(self, api=None, state_path=None):
        self.api = api or PublicData()
        self.path = Path(state_path) if state_path else None
        self.lock = threading.RLock()
        self.sessions = {}
        self.records = {}
        self.engines = {}
        self.quotes = {}
        self.stop = threading.Event()
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text())
            self.sessions = saved.get('sessions', {})
            self.records = saved.get('records', {})
            for rid, r in self.records.items():
                if r['state'] in ACTIVE:
                    r['state'] = 'CANCELED'
                    r['message'] = 'Panel restarted. Arm a new paper trigger to continue.'
            self.persist()

    def persist(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'sessions': self.sessions, 'records': self.records}))
            tmp.chmod(0o600)
            tmp.replace(self.path)

    def session(self, candidate=None):
        with self.lock:
            if candidate in self.sessions:
                self.sessions[candidate] = stamp()
                return candidate
            cutoff = stamp() - 86_400_000
            stale = [k for k,v in self.sessions.items() if v < cutoff]
            for sid in stale:
                self.sessions.pop(sid, None)
                for rid in [k for k,v in self.records.items() if v['owner'] == sid]:
                    self.records.pop(rid, None)
                    self.engines.pop(rid, None)
            if len(self.sessions) >= 100:
                raise ValueError('Panel is busy; try later')
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = stamp()
            self.persist()
            return sid

    def view(self, sid):
        with self.lock:
            result = []
            for r in self.records.values():
                if r['owner'] == sid:
                    result.append({k:v for k,v in r.items() if k != 'owner'})
            return {'mode': 'paper_only', 'can_trade': False,
                    'records': sorted(result,key=lambda x:x['created_ms'],reverse=True),
                    'server_ms': stamp(), 'poll_seconds': 1}

    def arm(self, sid, data):
        allowed = {'ticker','side','trigger_cents','cap_cents','quantity'}
        if set(data) != allowed:
            raise ValueError('Supply market, side, trigger, cap, and quantity only')
        ticker, side = data['ticker'], data['side']
        validate_ticker(ticker)
        if side not in ('YES','NO'):
            raise ValueError('Choose YES or NO')
        trigger, cap, qty = (decimal(data[k]) for k in ('trigger_cents','cap_cents','quantity'))
        if (not 1 <= trigger <= cap <= 99 or trigger % 1 or cap % 1
                or not 1 <= qty <= 100 or qty % 1):
            raise ValueError('Use whole cents 1–99, cap ≥ trigger, and 1–100 contracts')
        market = self.api.market(ticker)
        if market.get('ticker') != ticker or market.get('status') not in ('active','open'):
            raise ValueError('That market is not open')
        expires = close_ms(market)
        if expires <= stamp():
            raise ValueError('That market has already closed')
        with self.lock:
            active = [r for r in self.records.values() if r['state'] in ACTIVE]
            if len(active) >= 30 or sum(r['owner']==sid for r in active) >= 3:
                raise ValueError('Maximum 3 active paper triggers per browser')
            if any(r['owner']==sid and r['ticker']==ticker and r['side']==side for r in active):
                raise ValueError('Cancel the existing trigger for this market and side first')
            own = sorted([r for r in self.records.values() if r['owner']==sid],key=lambda r:r['created_ms'])
            for old in own[:-49]:
                if old['state'] not in ACTIVE:
                    self.records.pop(old['id'],None)
            rid = secrets.token_hex(12)
            self.engines[rid] = BuyStop(Settings(ticker=ticker,side=side,trigger=trigger/100,
                                      cap=cap/100,quantity=int(qty),expires_ms=expires,max_age_ms=2000))
            self.records[rid] = {'id':rid,'owner':sid,'ticker':ticker,'side':side,
                'trigger_cents':str(trigger),'cap_cents':str(cap),'quantity':int(qty),
                'expires_ms':expires,'created_ms':stamp(),'state':'WAITING',
                'message':'Waiting for a fresh price below your trigger','sequence':0,
                'quote':None,'fill':None}
            self.persist()
            return {k:v for k,v in self.records[rid].items() if k!='owner'}

    def cancel(self, sid, rid):
        with self.lock:
            r=self.records.get(rid)
            if not r or r['owner']!=sid:
                raise ValueError('Paper trigger not found')
            if r['state'] in ACTIVE:
                r['state'],r['message']='CANCELED','Canceled by you'
                self.engines.pop(rid,None)
                self.persist()

    def process(self, rid, book=None, error=None):
        now=stamp()
        with self.lock:
            r=self.records.get(rid)
            if not r or r['state'] not in ACTIVE:
                return
            engine=self.engines[rid]
            if now>=r['expires_ms']:
                r['state'],r['message']='EXPIRED','This 15-minute market has ended'
            elif error or not book or now-book['start_ms']>2000 or book['start_ms']>now:
                if r['state']=='PENDING':
                    r['state'],r['message']='MISSED','Fresh execution snapshot unavailable; no fill assumed'
                else:
                    engine.disconnect()
                    r['state'],r['message']='WAITING','Price feed interrupted; waiting for a fresh baseline'
                    r['quote']=None
            else:
                bid,ask,asks=outcome_book(book,r['side'])
                r['quote']={'bid_cents':str(bid*100) if bid is not None else None,
                            'ask_cents':str(ask*100) if ask is not None else None,
                            'received_ms':book['received_ms']}
                if r['state']=='PENDING':
                    # Must use a separately fetched snapshot after the trigger.
                    if book['start_ms'] <= r['triggered_ms']:
                        return
                    fill=paper_fill(asks,engine.settings.cap,r['quantity'])
                    r['fill']=fill
                    r['state']='PAPER_FILLED' if decimal(fill['filled']) else 'NO_FILL'
                    r['message']='Estimated fill from displayed liquidity; fees excluded' if decimal(fill['filled']) else 'No displayed sellers within your cap'
                elif bid is None or ask is None or not D('0')<ask<D('1') or not D('0')<bid<D('1'):
                    engine.disconnect()
                    r['state'],r['message']='WAITING','Waiting for a usable two-sided quote'
                else:
                    r['sequence']+=1
                    intent=engine.observe(Quote(r['ticker'],r['side'],bid,ask,book['start_ms'],r['sequence']),now)
                    r['state'],r['message']=engine.state,engine.reason
                    if intent:
                        r['state'],r['triggered_ms']='PENDING',now
                        r['message']='Trigger crossed; fetching a new book to estimate the fill'
            if r['state'] not in ACTIVE:
                self.engines.pop(rid,None)
            self.persist()

    def cycle(self):
        with self.lock:
            todo=[(rid,r['ticker']) for rid,r in self.records.items() if r['state'] in ACTIVE]
        by_ticker={}
        for rid,ticker in todo:
            by_ticker.setdefault(ticker,[]).append(rid)
        def get_one(item):
            ticker,ids=item
            try:
                b=self.api.book(ticker)
                for rid in ids:
                    self.process(rid,b)
            except Exception:
                for rid in ids:
                    self.process(rid,error=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(get_one,by_ticker.items()))

    def worker(self):
        while not self.stop.is_set():
            start=time.monotonic()
            self.cycle()
            self.stop.wait(max(.05,1-(time.monotonic()-start)))


def handler(panel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,status,payload,kind='application/json',cookie=None):
            body=json.dumps(payload).encode() if kind=='application/json' else payload
            self.send_response(status)
            self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            if cookie:
                secure='; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''
                self.send_header('Set-Cookie',f'paper_session={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400'+secure)
            self.end_headers()
            self.wfile.write(body)

        def sid(self):
            c=SimpleCookie()
            try:
                c.load(self.headers.get('Cookie',''))
                token=c['paper_session'].value if 'paper_session' in c else None
            except Exception:
                token=None
            return panel.session(token)

        def do_GET(self):
            p=urlsplit(self.path)
            try:
                if p.path=='/':
                    return self.send(200,(ROOT/'index.html').read_bytes(),'text/html')
                if p.path=='/health':
                    return self.send(200,{'ok':True,'mode':'paper_only','can_trade':False})
                if p.path=='/api/state':
                    sid=self.sid()
                    return self.send(200,panel.view(sid),cookie=sid)
                if p.path=='/api/markets':
                    coin=parse_qs(p.query).get('coin',['BTC'])[0]
                    return self.send(200,{'markets':panel.api.markets(coin)})
                return self.send(404,{'error':'Not found'})
            except ValueError as e:
                self.send(400,{'error':str(e)})
            except Exception:
                self.send(502,{'error':'Live market data unavailable. Retry shortly.'})

        def do_POST(self):
            p=urlsplit(self.path)
            if p.path not in ('/api/arm','/api/cancel'):
                return self.send(404,{'error':'Only paper controls exist'})
            origin=self.headers.get('Origin')
            if (self.headers.get('X-Paper-Panel')!='1' or (origin and urlsplit(origin).netloc!=self.headers.get('Host'))):
                return self.send(403,{'error':'Use the controls on this panel'})
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=4096:
                    raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(n))
                if not isinstance(data,dict):
                    raise ValueError('Expected an object')
                sid=self.sid()
                if p.path=='/api/arm':
                    return self.send(201,panel.arm(sid,data),cookie=sid)
                panel.cancel(sid,data.get('id'))
                return self.send(200,{'ok':True},cookie=sid)
            except (ValueError,KeyError,TypeError,ArithmeticError) as e:
                self.send(400,{'error':str(e)})
            except Exception:
                self.send(502,{'error':'Market verification unavailable; trigger was not armed'})
    return Handler


if __name__=='__main__':
    state=os.environ.get('PAPER_STATE_PATH',str(ROOT/'data'/'paper.json'))
    app=Panel(state_path=state)
    threading.Thread(target=app.worker,daemon=True).start()
    port=int(os.environ.get('PORT','8080'))
    print(f'Paper-only panel listening on {port}; real orders are unsupported',flush=True)
    ThreadingHTTPServer(('0.0.0.0',port),handler(app)).serve_forever()
