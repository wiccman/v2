import copy
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from kalshi import KalshiAPIError, KalshiClient
from market_maker import MMConfig, MarketMaker, ladder, ledger, quote_prices


class Exchange:
    """Exchange double with cumulative partial fills and cancellation races."""
    def __init__(self):
        self.now = 1000
        self.remote = {}
        self.batches = []
        self.cancelled = []
        self.balance_value = "10"
        self.bid, self.ask = D("0.49"), D("0.51")
        self.timeout_after_accept = False
        self.timeout_before_accept = False
        self.reject_index = None
        self.cancel_race = None
        self.cancel_pending = False
        self.manual_position = D(0)
        self.slow_book = False
        self.settled = {}
        self.fill_history = []
        self.ioc_fill_limit = D(100)
        self.before_ioc = None

    def market(self, ticker):
        if ticker in self.settled:
            return {"ticker": ticker, "status": "settled", "result": self.settled[ticker]}
        return {"ticker": ticker, "status": "active"}

    def all_fills(self, ticker):
        return copy.deepcopy([f for f in self.fill_history if f["ticker"] == ticker])

    def positions(self, ticker):
        held = self.manual_position + sum((D(o["fill_count_fp"]) * (1 if o["side"] == "bid" else -1)
                    for o in self.remote.values() if o["ticker"] == ticker), D(0))
        return [{"ticker": ticker, "position_fp": str(held)}]

    def balance(self):
        return {"balance_dollars": self.balance_value}

    def all_orders(self, ticker, status=None):
        return [copy.deepcopy(o) for o in self.remote.values()
                if o["ticker"] == ticker and (status is None or o["status"] == status)]

    def order(self, order_id):
        return copy.deepcopy(self.remote[order_id])

    def orderbook(self, ticker):
        if self.slow_book:
            self.now += 6
        result = {"yes_dollars": [[str(self.bid), "100"]],
                  "no_dollars": [[str(1 - self.ask), "100"]]}
        for order in self.all_orders(ticker, "resting"):
            key = "yes_dollars" if order["side"] == "bid" else "no_dollars"
            price = D(order["price"]) if order["side"] == "bid" else 1 - D(order["price"])
            result[key].append([str(price), order["remaining_count_fp"]])
        return result

    def place_mm_batch(self, intents):
        self.batches.append(copy.deepcopy(intents))
        if self.timeout_before_accept:
            raise TimeoutError("POST timed out before response; acceptance unknown")
        result = []
        for index, intent in enumerate(intents):
            cid = intent["client_id"]
            if index == self.reject_index:
                result.append({"client_order_id": cid, "error": {"code": "post_only_cross"}})
                continue
            order_id = f"order-{len(self.remote)}"
            self.remote[order_id] = dict(intent, order_id=order_id, client_order_id=cid,
                status="resting", fill_count_fp="0", remaining_count_fp=intent["quantity"],
                maker_fees_dollars="0", taker_fees_dollars="0")
            if intent['reduce_only']:
                if self.before_ioc:
                    self.before_ioc(self, intent)
                    self.before_ioc = None
                held = D(self.positions(intent['ticker'])[0]['position_fp'])
                direction = D(1) if intent['side']=='bid' else D(-1)
                crossed = D(intent['price']) >= self.ask if direction > 0 else D(intent['price']) <= self.bid
                count = min(abs(held), D(intent['quantity']), self.ioc_fill_limit) if crossed and direction*held < 0 else D(0)
                if count:
                    self.fill(order_id, str(count), '.01')
                if self.remote[order_id]['status'] != 'executed':
                    self.remote[order_id].update(status='canceled', remaining_count_fp='0')
            result.append({"client_order_id": cid, "order_id": order_id})
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise TimeoutError("accepted but response was lost")
        return result

    def fill(self, order_id, count, fee="0"):
        order = self.remote[order_id]
        self.fill_history.append(dict(fill_id=f"fill-{len(self.fill_history):04}", order_id=order_id,
            ticker=order['ticker'], book_side=order['side'], outcome_side='yes' if order['side']=='bid' else 'no',
            count_fp=count, yes_price_dollars=order['price'], fee_cost=fee,
            created_time=datetime.fromtimestamp(self.now, timezone.utc).isoformat()))
        order["fill_count_fp"] = str(D(order["fill_count_fp"]) + D(count))
        order["remaining_count_fp"] = str(D(order["remaining_count_fp"]) - D(count))
        order["maker_fees_dollars"] = str(D(order["maker_fees_dollars"]) + D(fee))
        if D(order["remaining_count_fp"]) == 0:
            order["status"] = "executed"

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_pending:
            return {}
        if self.cancel_race == order_id:
            self.cancel_race = None
            self.fill(order_id, self.remote[order_id]["remaining_count_fp"])
        if self.remote[order_id]["status"] != "executed":
            self.remote[order_id].update(status="canceled", remaining_count_fp="0")
        return {"order_id": order_id}


def setup(config=None):
    exchange, state, saved, events = Exchange(), {"markets": {}}, [], []
    mm = MarketMaker(exchange, lambda s: saved.append(json.loads(json.dumps(s))),
                     lambda *a, **k: events.append((a, k)), config, clock=lambda: exchange.now)
    return exchange, state, mm, saved, events


def cycle(mm, state, ticker="BTC", close=1900):
    mm.cycle(state, {"ticker": ticker}, datetime.fromtimestamp(close, timezone.utc))


def test_five_distinct_prices_per_side_budget_and_restart():
    ex, state, mm, saved, _ = setup()
    cycle(mm, state)
    batch = ex.batches[0]
    assert [D(o["price"]) for o in batch[:5]] == list(map(D, [".46", ".45", ".44", ".43", ".42"]))
    assert [D(o["price"]) for o in batch[5:]] == list(map(D, [".54", ".55", ".56", ".57", ".58"]))
    assert all(D(o["quantity"]) == 1 and o["expiry"] == 1015 for o in batch)
    assert len({o["client_id"] for o in batch}) == 10
    # Intents for every order must be durable BEFORE the exchange call.
    assert len(saved[1]["mm"]["markets"]["BTC"]["orders"]) == 10
    assert not any("order_id" in o for o in saved[1]["mm"]["markets"]["BTC"]["orders"])
    restored = json.loads(json.dumps(state))
    restarted = MarketMaker(ex, lambda s: None, lambda *a, **k: None, clock=lambda: ex.now)
    cycle(restarted, restored)
    assert len(ex.batches) == 1  # stable quotes retain queue position


@pytest.mark.parametrize("side_index,exit_side", [(0, "ask"), (5, "bid")])
def test_partial_fill_only_exits_exact_quantity(side_index, exit_side):
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.fill(f"order-{side_index}", ".37", ".003")
    cycle(mm, state)  # cancel first, including the partial remainder
    assert not ex.all_orders("BTC", "resting")
    cycle(mm, state)
    exits = ex.batches[-1]
    assert len(exits) == 1
    assert exits[0]["side"] == exit_side and exits[0]["reduce_only"]
    assert D(exits[0]["quantity"]) == D(".37")


def test_all_five_fills_exit_exact_inventory_with_protected_ioc():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    for i in range(5):
        ex.fill(f"order-{i}", "1")
    cycle(mm, state)
    cycle(mm, state)
    exits = ex.batches[-1]
    assert len(exits) == 1
    assert exits[0]["side"] == "ask" and exits[0]["reduce_only"]
    assert D(exits[0]["quantity"]) == 5 and D(exits[0]["price"]) == ex.bid
    assert D(ex.positions('BTC')[0]['position_fp']) == 0
    assert not ex.all_orders('BTC','resting')


def test_cancel_race_fill_is_reconciled_before_replacement():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.cancel_race = "order-0"
    ex.now += 11  # renewal
    cycle(mm, state)
    assert len(ex.batches) == 1
    cycle(mm, state)
    assert len(ex.batches[-1]) == 1 and ex.batches[-1][0]["reduce_only"]
    assert ledger(state["mm"]["markets"]["BTC"])[0] == 1


def test_acknowledged_but_unconfirmed_cancel_never_duplicates():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.cancel_pending = True
    ex.now += 11
    cycle(mm, state)
    cycle(mm, state)
    assert len(ex.batches) == 1


def test_lost_batch_response_recovers_by_client_id_after_restart():
    ex, state, mm, _, _ = setup()
    ex.timeout_after_accept = True
    with pytest.raises(TimeoutError):
        cycle(mm, state)
    assert len(ex.remote) == 10
    # Crash left only persisted client IDs. Recovery must not submit ten more.
    cycle(mm, json.loads(json.dumps(state)))
    assert len(ex.batches) == 1


def test_ambiguous_absent_orders_block_instead_of_retrying():
    ex, state, mm, _, _ = setup()
    ex.timeout_before_accept = True
    with pytest.raises(TimeoutError):
        cycle(mm, state)
    ex.now += 60
    with pytest.raises(RuntimeError, match="unresolved"):
        cycle(mm, state)
    assert len(ex.batches) == 1


def test_partial_batch_rejection_cancels_accepted_orders():
    ex, state, mm, _, _ = setup()
    ex.reject_index = 5
    with pytest.raises(RuntimeError, match="batch incomplete"):
        cycle(mm, state)
    assert len(ex.remote) == 9
    assert not ex.all_orders("BTC", "resting")


@pytest.mark.parametrize("balance", ["4.59", "0"])
def test_ten_quotes_must_fit_cash_including_fee_reserve(balance):
    ex, state, mm, _, _ = setup()
    ex.balance_value = balance
    cycle(mm, state)
    assert ex.batches == []


def test_full_ladder_reserves_460_cents_not_ten_dollars():
    ex, state, mm, _, _ = setup()
    ex.balance_value = "4.60"
    cycle(mm, state)
    assert len(ex.batches[0]) == 10


def test_closed_market_losses_persist_and_consume_budget():
    ex, state, mm, _, _ = setup()
    state["mm"] = {"markets": {"OLD": {"settled": True, "settlement": "0", "orders": [
        {"side": "bid", "filled": "10", "price": ".60", "fees": ".10"}
    ]}}}
    cycle(mm, state)
    assert not ex.batches  # only $3.90 of initial MM capital remains, despite $10 cash


def test_round_trip_ledger_deducts_actual_fees():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.fill("order-0", "1", ".01")
    ex.fill("order-5", "1", ".01")
    cycle(mm, state)
    assert ledger(state["mm"]["markets"]["BTC"]) == (D(0), D(".06"))


@pytest.mark.parametrize("foreign", ["position", "order", "legacy"])
def test_never_adopts_other_strategies(foreign):
    ex, state, mm, _, _ = setup()
    if foreign == "position":
        ex.manual_position = D(1)
    elif foreign == "order":
        ex.remote["other"] = {"order_id": "other", "ticker": "BTC", "status": "resting", "fill_count_fp": "0", "side": "bid"}
    else:
        state["markets"]["BTC"] = {"buys": 1}
    cycle(mm, state)
    assert not ex.batches
    assert not ex.cancelled


def test_foreign_order_added_later_cancels_only_our_orders():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.remote["other"] = {"order_id": "other", "ticker": "BTC", "status": "resting", "fill_count_fp": "0", "side": "bid"}
    cycle(mm, state)
    assert "other" not in ex.cancelled
    assert len(ex.cancelled) == 10


def test_stale_book_and_fast_price_jump_cancel_quotes():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.slow_book = True
    cycle(mm, state)
    assert not ex.all_orders("BTC", "resting")
    ex.slow_book = False
    ex.bid, ex.ask = D(".69"), D(".71")
    cycle(mm, state)
    assert state["mm"]["markets"]["BTC"]["pause_until"] == ex.now + 30
    cycle(mm, state)
    assert len(ex.batches) == 1


def test_cutoff_cancels_entries_but_still_quotes_held_inventory():
    ex, state, mm, _, _ = setup()
    ex.now = 1830
    cycle(mm, state)
    assert all(o["expiry"] == 1840 for o in ex.batches[0])
    ex.fill("order-0", "1")
    ex.now = 1841
    cycle(mm, state)
    cycle(mm, state)
    assert all(o["reduce_only"] and o["expiry"] < 1900 for o in ex.batches[-1])
    flat_ex, flat_state, flat_mm, _, _ = setup()
    flat_ex.now = 1841
    cycle(flat_mm, flat_state)
    assert not flat_ex.batches


def test_waits_for_previous_inventory_settlement_and_records_payout():
    ex, state, mm, _, _ = setup()
    cycle(mm, state)
    ex.fill("order-0", "1")
    cycle(mm, state, ticker="NEXT")
    assert len(ex.batches) == 1
    ex.settled["BTC"] = "yes"
    cycle(mm, state, ticker="NEXT")
    assert len(ex.batches) == 2
    assert ledger(state["mm"]["markets"]["BTC"]) == (D(0), D(".54"))


def test_bad_or_self_only_book_is_not_used_as_fair_price():
    cfg = MMConfig()
    with pytest.raises(ValueError):
        quote_prices({"yes_dollars": [[".60", "2"]], "no_dollars": [[".50", "2"]]}, [], {}, cfg)
    own = [{"status": "resting", "side": "bid", "price": ".46", "remaining": "1"}]
    with pytest.raises(ValueError, match="external liquidity"):
        quote_prices({"yes_dollars": [[".46", "1"]], "no_dollars": [[".46", "2"]]}, own, {}, cfg)


def test_ladder_snaps_to_coarser_ticks_without_duplicate_prices():
    market = {"price_ranges": [{"start": ".02", "end": ".98", "step": ".02"}]}
    quotes = ladder(D(".46"), D(".54"), market, MMConfig())
    assert [q[1] for q in quotes[:5]] == list(map(D, [".46", ".44", ".42", ".40", ".38"]))
    assert len({q[1] for q in quotes}) == 10


@pytest.mark.parametrize("held,bid,ask,expected", [(D(5), ".98", ".99", ("ask", D(".99"), D(5), True)),
                                                   (D(-5), ".01", ".02", ("bid", D(".01"), D(5), True))])
def test_inventory_can_exit_at_price_boundaries(held, bid, ask, expected):
    book = {"yes_dollars": [[bid, "100"]], "no_dollars": [[str(1-D(ask)), "100"]]}
    b, a, _ = quote_prices(book, [], {}, MMConfig(), held)
    assert ladder(b, a, {}, MMConfig(), held) == [expected]


def test_previous_market_ambiguous_submission_can_recover_after_rollover():
    ex, state, mm, _, _ = setup()
    ex.timeout_after_accept = True
    with pytest.raises(TimeoutError):
        cycle(mm, state)
    ex.settled["BTC"] = "no"
    cycle(mm, state, ticker="NEXT")
    assert state["mm"]["markets"]["BTC"]["settled"]
    assert len(ex.batches) == 2


class RecordingClient(KalshiClient):
    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None, body=None, auth=False):
        self.calls.append((method, path, params, body, auth))
        return {"orders": []}


def test_batch_api_uses_ioc_for_reduce_only_without_post_only_or_expiry():
    client = RecordingClient()
    client.place_mm_batch([dict(ticker="BTC", side="ask", quantity=".37", price=".54",
                               expiry=1015, client_id="persisted-id", reduce_only=True)])
    method, path, _, body, auth = client.calls[0]
    assert method == "POST" and path == "/portfolio/events/orders/batched" and auth
    order = body["orders"][0]
    assert not order["post_only"] and order["reduce_only"] and order["cancel_order_on_pause"]
    assert order['time_in_force'] == 'immediate_or_cancel'
    assert order["client_order_id"] == "persisted-id"
    assert order["price"] == "0.5400" and order["count"] == ".37"
    assert 'expiration_time' not in order


def test_order_recovery_paginates():
    client = RecordingClient()
    seen = []
    def request(method, path, params=None, **kwargs):
        seen.append(dict(params))
        return {"orders": [{"order_id": "second"}], "cursor": ""} if params.get("cursor") else {"orders": [{"order_id": "first"}], "cursor": "next"}
    client.request = request
    assert len(client.all_orders("BTC")) == 2
    assert seen[1]["cursor"] == "next" and "status" not in seen[0]


@pytest.mark.parametrize("values", [dict(budget=D("11")), dict(levels=6), dict(quantity=D("2")),
                                    dict(quantity=D(".001")), dict(spread=D(".04")), dict(budget=D("NaN"))])
def test_invalid_config_cannot_start(values):
    with pytest.raises(ValueError):
        MMConfig(**values)


def test_legacy_execution_will_not_manage_an_mm_market(monkeypatch):
    import bot
    current = datetime.now(timezone.utc)
    monkeypatch.setattr(bot, "active_market", lambda now: ({"ticker": "BTC"}, current, current))
    monkeypatch.setattr(bot, "write_log", lambda *a, **k: None)
    bot.cycle({"markets": {}, "mm": {"markets": {"BTC": {"orders": []}}}})


def test_entry_batch_still_posts_five_levels_each_side_with_ttl():
    client=RecordingClient()
    intents=[dict(ticker='BTC',side=side,quantity='1',price=str(price),expiry=1015,client_id=str(i),reduce_only=False)
             for i,(side,price,_,_) in enumerate(ladder(D('.46'),D('.54'),{},MMConfig()))]
    client.place_mm_batch(intents)
    orders=client.calls[0][3]['orders']
    assert len(orders)==10
    assert all(o['post_only'] and not o['reduce_only'] and o['time_in_force']=='good_till_canceled' and o['expiration_time']==1015 for o in orders)


@pytest.mark.parametrize('index', [0,5])
def test_ioc_partial_fill_retries_only_remaining_inventory(index):
    ex,state,mm,_,_=setup();cycle(mm,state);ex.fill(f'order-{index}','1');cycle(mm,state)
    ex.ioc_fill_limit=D('.40');cycle(mm,state)
    assert abs(D(ex.positions('BTC')[0]['position_fp']))==D('.60')
    cycle(mm,state)
    assert D(ex.batches[-1][0]['quantity'])==D('.60')
    assert abs(D(ex.positions('BTC')[0]['position_fp']))==D('.20')


def test_ioc_position_change_at_exchange_cannot_reverse_position():
    ex,state,mm,_,_=setup();cycle(mm,state);ex.fill('order-0','1');cycle(mm,state)
    ex.before_ioc=lambda exchange,intent:setattr(exchange,'manual_position',D('-1'))
    cycle(mm,state)
    assert D(ex.positions('BTC')[0]['position_fp'])==0
    assert ex.remote['order-10']['fill_count_fp']=='0'
    assert ex.remote['order-10']['status']=='canceled'


def test_external_close_reconciles_and_waits_for_new_contract():
    ex,state,mm,_,events=setup();cycle(mm,state);ex.fill('order-5','1');cycle(mm,state)
    ex.now+=1;ex.manual_position=D(1)
    ex.fill_history.append(dict(fill_id='external',order_id='manual',ticker='BTC',book_side='bid',outcome_side='yes',
        count_fp='1',yes_price_dollars='.50',fee_cost='.01',created_time=datetime.fromtimestamp(ex.now,timezone.utc).isoformat()))
    cycle(mm,state)
    assert ledger(state['mm']['markets']['BTC'])==(D(0),D('.03'))
    cycle(mm,json.loads(json.dumps(state)))
    assert len(ex.batches)==1
    ex.settled['BTC']='no';ex.manual_position=D(0)
    cycle(mm,state,ticker='NEXT')
    assert len(ex.batches)==2
    assert ledger(state['mm']['markets']['BTC'])==(D(0),D('.03'))


def test_finalized_market_status_is_accepted_for_rollover():
    ex,state,mm,_,_=setup();cycle(mm,state);ex.fill('order-0','1')
    original=ex.market
    ex.market=lambda t: {'ticker':t,'status':'finalized','result':'yes'} if t=='BTC' else original(t)
    cycle(mm,state,ticker='NEXT')
    assert state['mm']['markets']['BTC']['settled']
    assert ledger(state['mm']['markets']['BTC'])==(D(0),D('.54'))


def test_fill_pagination_and_repeated_cursor_block():
    client=RecordingClient();seen=[]
    def request(method,path,params=None,**kwargs):
        seen.append(dict(params));return {'fills':[{'fill_id':'second'}], 'cursor':''} if params.get('cursor') else {'fills':[{'fill_id':'first'}], 'cursor':'next'}
    client.request=request
    assert len(client.all_fills('BTC'))==2 and seen[1]['cursor']=='next'
    client.request=lambda *a,**k: {'fills':[],'cursor':'same'}
    with pytest.raises(RuntimeError,match='cursor repeated'):client.all_fills('BTC')
