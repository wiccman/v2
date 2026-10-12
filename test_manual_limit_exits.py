"""Offline lifecycle integration: real API client, replay and both exit workers."""
import copy
from decimal import Decimal as D
from datetime import datetime, timezone

import pytest
from kalshi import KalshiClient, KalshiAPIError
from manual_limit_exits import ManualLimitExitWorker, ManualExitBlocked, round_target, TargetUnreachable
from price_pairs import InventorySyncError
from take_profit import TakeProfitMonitor

T = "KXBTC15M-TEST"


class Exchange(KalshiClient):
    def __init__(self):
        super().__init__(key_id="offline-test-key")
        self.clock_value = 1000.0
        self.clock = lambda: self.clock_value
        self.close = 2000.0
        self.bid = D(".40")
        self.no_bid = D(".40")
        self.liquidity = D("10000")
        self.history, self.remote, self.held, self.sent = [], {}, {}, []
        self.reads, self.cancelled = [], []
        self.fail_post = None
        self.lose_ack = False
        self.hidden_orders = set()
        self.hidden_fills = set()
        self.position_override = None
        self.on_market = None
        self.status = "active"
        self.price_ranges = [{"start": "0", "end": "1", "step": ".01"}]

    def clock(self):
        return self.clock_value

    def market(self, ticker):
        if self.on_market:
            callback, self.on_market = self.on_market, None
            callback()
        return {"ticker": ticker, "status": self.status,
                "close_time": datetime.fromtimestamp(self.close, timezone.utc).isoformat(),
                "yes_bid_dollars": str(self.bid), "no_bid_dollars": str(self.no_bid),
                "price_ranges": copy.deepcopy(self.price_ranges)}

    def make_order(self, ticker=T, sign=1, count=10, price=".40", oid=None,
                   client_id=None, action="buy", subaccount=0):
        oid = oid or "order-" + str(len(self.remote) + 1)
        price, count = D(str(price)), D(str(count))
        yes_price = price if sign > 0 else 1 - price
        wire_sign = sign if action == "buy" else -sign
        self.remote[oid] = {"order_id": oid, "client_order_id": client_id or "app-" + oid,
            "ticker": ticker, "type": "limit", "status": "resting",
            "side": "yes" if sign > 0 else "no", "action": action,
            "book_side": "bid" if wire_sign > 0 else "ask",
            "outcome_side": "yes" if wire_sign > 0 else "no",
            "yes_price_dollars": str(yes_price), "no_price_dollars": str(1-yes_price),
            "initial_count_fp": str(count), "remaining_count_fp": str(count),
            "fill_count_fp": "0", "subaccount_number": subaccount}
        return oid

    def fill(self, oid, count, price=None):
        row = self.remote[oid]
        count = D(str(count))
        assert 0 < count <= D(row["remaining_count_fp"])
        yes_price = D(row["yes_price_dollars"])
        if price is not None:
            yes_price = D(str(price)) if row["side"] == "yes" else 1-D(str(price))
        f = {"fill_id": "fill-" + str(len(self.history) + 1), "order_id": oid,
             "ticker": row["ticker"], "book_side": row["book_side"],
             "outcome_side": row["outcome_side"], "side": row["side"], "action": row["action"],
             "yes_price_dollars": str(yes_price), "no_price_dollars": str(1-yes_price),
             "count_fp": str(count), "subaccount_number": row["subaccount_number"],
             "ts": str(D(str(self.clock_value)) + D(len(self.history)+1)/10000)}
        self.history.append(f)
        sign = 1 if row["book_side"] == "bid" else -1
        self.held[row["ticker"]] = self.held.get(row["ticker"], D(0)) + sign*count
        row["fill_count_fp"] = str(D(row["fill_count_fp"])+count)
        row["remaining_count_fp"] = str(D(row["remaining_count_fp"])-count)
        row["status"] = "resting" if D(row["remaining_count_fp"]) else "executed"
        return f["fill_id"]

    def buy(self, count=10, price=".40", sign=1, oid=None, ticker=T):
        oid = self.make_order(ticker, sign, count, price, oid)
        self.fill(oid, count)
        return oid

    def request(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        if method == "GET":
            self.reads.append((path, copy.deepcopy(params)))
            ticker = params.get("ticker")
            if path == "/portfolio/positions":
                if self.position_override is not None:
                    return {"market_positions": copy.deepcopy(self.position_override), "cursor": ""}
                return {"market_positions": [{"ticker": t, "position_fp": str(q), "subaccount_number": 0}
                    for t,q in self.held.items() if q and (not ticker or t==ticker)], "cursor": ""}
            if path == "/portfolio/orders":
                rows = [r for oid,r in self.remote.items() if oid not in self.hidden_orders and
                        (not ticker or r["ticker"]==ticker) and (not params.get("status") or r["status"]==params["status"])]
                return {"orders": copy.deepcopy(rows), "cursor": ""}
            if path == "/portfolio/fills":
                return {"fills": copy.deepcopy([f for f in reversed(self.history)
                    if f["fill_id"] not in self.hidden_fills and (not ticker or f["ticker"]==ticker)
                    and f["subaccount_number"]==params.get("subaccount", f["subaccount_number"])]), "cursor": ""}
            if path.startswith("/portfolio/orders/"):
                oid = path.rsplit("/",1)[-1]
                if oid not in self.remote or oid in self.hidden_orders:
                    raise KalshiAPIError(404, "not found")
                return {"order": copy.deepcopy(self.remote[oid])}
            raise AssertionError((method,path,params))
        if method == "DELETE":
            oid = path.rsplit("/",1)[-1]
            self.cancelled.append(oid)
            self.remote[oid].update(status="canceled", remaining_count_fp="0")
            return {"order_id": oid, "reduced_by": "0"}
        assert method == "POST" and path == "/portfolio/events/orders"
        assert body["time_in_force"] == "immediate_or_cancel", "V2 reduce-only must be IOC"
        assert body["reduce_only"] is True
        assert "expiration_time" not in body
        if self.fail_post:
            raise self.fail_post
        self.sent.append(copy.deepcopy(body))
        held = self.held.get(body["ticker"], D(0))
        selling_yes = body["side"] == "ask"
        target = D(body["price"]) if selling_yes else 1-D(body["price"])
        bid = self.bid if selling_yes else self.no_bid
        owned = max(D(0), held if selling_yes else -held)
        qty = min(owned, D(body["count"]), self.liquidity) if bid >= target else D(0)
        oid = self.make_order(body["ticker"], 1 if selling_yes else -1,
            body["count"], target, client_id=body["client_order_id"], action="sell")
        if qty:
            self.fill(oid, qty, bid)
        self.remote[oid].update(status="executed" if qty==D(body["count"]) else "canceled",
                                remaining_count_fp="0")
        result = {"order_id": oid, "client_order_id": body["client_order_id"],
                  "fill_count": str(qty), "remaining_count": "0"}
        if self.lose_ack:
            raise TimeoutError("Lost order acknowledgement")
        return result


def setup(tmp_path, *, dry_run=False, sign=1):
    e = Exchange()
    events = []
    state = {"markets": {}}
    manual = ManualLimitExitWorker(e, tmp_path/"manual.json", clock=e.clock,
        emit=lambda event, **data: events.append((event,data)), dry_run=dry_run)
    auto = TakeProfitMonitor(e, lambda: copy.deepcopy(state), tmp_path/"auto.json",
        pairs={D(".45"):D(".55")}, per_order_profit=D(".20"), quote_gate=True,
        clock=e.clock, manual_exits=manual, emit=lambda event, **data: events.append((event,data)))
    return e, state, manual, auto, events


def auto_buy(e, state, count=10, price=".45", sign=1):
    oid = e.buy(count,price,sign,oid="automatic-"+str(len(e.remote)))
    state["markets"].setdefault(T, {"close_timestamp": e.close, "entry_intents": []})["entry_intents"].append({
        "order_id": oid, "client_id": e.remote[oid]["client_order_id"],
        "side": "YES" if sign>0 else "NO", "price": price, "exit_target": ".55",
        "quantity": str(count), "entry_closed": True})
    return oid


@pytest.mark.parametrize("sign", [1,-1])
def test_manual_buy_is_detected_and_exited_without_auto_entry_record(tmp_path,sign):
    e,state,m,a,_=setup(tmp_path)
    oid=e.buy(sign=sign)
    a.run_once()
    assert a.healthy and not e.sent
    assert m._plans[T][0]["raw_target"]==D(".425")
    e.bid=e.no_bid=D(".43")
    a.run_once();a.run_once()
    assert e.held[T]==0 and len(e.sent)==1 and a.healthy
    assert e.sent[0]["side"]==("ask" if sign>0 else "bid")
    assert e.sent[0]["price"]==("0.4300" if sign>0 else "0.5700")
    assert state=={"markets":{}}  # Manual activity never changes the auto budget/state.
    assert D(m.ledger(T)["profit_summary"][oid]["realized_gross"])==D(".30")
    assert not a.state["markets"][T].get("exit_orders")


@pytest.mark.parametrize("sign", [1,-1])
def test_manual_exit_allocations_preserve_older_same_side_bot_lots(tmp_path,sign):
    e,state,m,a,_=setup(tmp_path)
    auto_id=auto_buy(e,state,sign=sign)
    manual_id=e.buy(sign=sign)
    e.bid=e.no_bid=D(".43")
    a.run_once();a.run_once()
    assert e.held[T]==sign*10
    assert a.bot_inventory(T,state["markets"][T],e.held[T])==sign*10
    assert len(e.sent)==1
    manual_exit=next(iter(m.ledger(T)["exit_orders"].values()))
    assert {f["order_id"] for f in e.history if f["fill_id"] in {x["fill_id"] for x in manual_exit["allocations"]}}=={manual_id}
    e.bid=e.no_bid=D(".47")
    a.run_once();a.run_once()
    assert e.held[T]==0 and a.healthy
    assert len(a.state["markets"][T]["exit_orders"])==1
    assert auto_id not in m.ledger(T)["order_goals"]


@pytest.mark.parametrize("sign", [1,-1])
def test_auto_exit_preserves_more_expensive_manual_lots(tmp_path,sign):
    e,state,m,a,_=setup(tmp_path)
    e.buy(5,".50",sign)
    auto_buy(e,state,sign=sign)
    e.bid=e.no_bid=D(".47")
    a.run_once();a.run_once()
    assert e.held[T]==sign*5 and len(e.sent)==1
    assert a.bot_inventory(T,state["markets"][T],e.held[T])==0
    e.bid=e.no_bid=D(".55")
    a.run_once();a.run_once()
    assert e.held[T]==0 and len(e.sent)==2


def test_partial_entry_reprices_one_goal_per_original_order(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.make_order(count=10)
    e.fill(oid,5)
    a.run_once()
    assert m._plans[T][0]["raw_target"]==D(".45")
    e.fill(oid,5,price=".42")
    a.run_once()
    assert len(m.ledger(T)["order_goals"])==1
    assert m._plans[T][0]["quantity"]==10
    assert m._plans[T][0]["raw_target"]==D(".435")
    e.bid=D(".44")
    a.run_once();a.run_once()
    assert D(m.ledger(T)["profit_summary"][oid]["realized_gross"])==D(".30")


def test_partial_exit_deducts_actual_realized_profit_not_another_25c(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.buy()
    e.bid=D(".45");e.liquidity=D(4)
    a.run_once()  # Four fill at .45: .20 realized, six remain.
    e.bid=D(".40")
    a.run_once()
    plan=m._plans[T][0]
    assert plan["realized"]==D(".20")
    assert round_target(plan["raw_target"],e.price_ranges,1)==D(".41")
    e.bid=D(".41");e.liquidity=D(100)
    a.run_once();a.run_once()
    assert e.held[T]==0
    assert D(m.ledger(T)["profit_summary"][oid]["realized_gross"])==D(".26")


def test_later_fill_of_same_buy_order_does_not_restart_profit_goal(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.make_order(count=10)
    e.fill(oid,5);e.bid=D(".45")
    a.run_once();a.run_once()
    assert e.held[T]==0
    e.fill(oid,5);e.bid=D(".40")
    a.run_once();a.run_once()
    assert len(e.sent)==2 and e.sent[1]["price"]=="0.4000"
    assert D(m.ledger(T)["profit_summary"][oid]["realized_gross"])==D(".25")


@pytest.mark.parametrize("sign", [1,-1])
def test_fractional_contracts_are_not_truncated(tmp_path,sign):
    e,_,m,a,_=setup(tmp_path)
    e.buy("1.25",".40",sign);e.bid=e.no_bid=D(".60")
    a.run_once();a.run_once()
    assert e.sent[0]["count"]=="1.25" and e.held[T]==0


def test_duplicate_identical_fills_are_idempotent(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy();e.history.append(copy.deepcopy(e.history[0]));e.bid=D(".43")
    a.run_once();a.run_once()
    assert len(e.sent)==1 and e.sent[0]["count"]=="10"


def test_conflicting_duplicate_fills_fail_closed(tmp_path):
    e,_,_,a,_=setup(tmp_path)
    e.buy();e.history.append({**e.history[0],"count_fp":"20"});e.bid=D(".99")
    a.run_once()
    assert not a.healthy and not e.sent


def test_unknown_bot_entry_acknowledgement_blocks_manual_adoption(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".99")
    state["markets"][T]={"close_timestamp":e.close,"entry_intents":[{"client_id":"lost-bot-ack","side":"YES","price":".45"}]}
    a.run_once()
    assert not e.sent and not a.healthy and not m.ledger(T).get("managed_fills")


def test_lost_bot_entry_ack_recovered_by_client_id_is_not_adopted(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    auto_buy(e,state)
    del state["markets"][T]["entry_intents"][0]["order_id"]
    a.run_once()
    assert a.healthy and not m.ledger(T)["managed_fills"]


@pytest.mark.parametrize("mutation", ["fill","position","record","resting_order"])
def test_changes_during_quote_block_manual_submission(tmp_path,mutation):
    e,state,m,a,_=setup(tmp_path)
    e.buy();a.run_once();e.bid=D(".43")
    # Observe has completed before maybe_submit reads this quote.
    def change():
        if mutation=="fill": e.buy(1,".40")
        elif mutation=="position": e.held[T]+=1
        elif mutation=="record": state["markets"][T]={"entry_intents":[{"client_id":"racing-bot"}]}
        else: e.make_order(count=10,action="sell")
    e.on_market=change
    a.run_once()
    assert not e.sent and not a.healthy


def test_stale_snapshot_blocks_manual_order(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy();a.run_once();e.clock_value+=11
    with pytest.raises(ManualExitBlocked):m.verify_snapshot(T)


@pytest.mark.parametrize("action,sign", [("sell",1),("buy",-1)])
def test_external_resting_reduction_blocks_both_workers_without_cancel(tmp_path,action,sign):
    e,state,m,a,_=setup(tmp_path)
    auto_buy(e,state);e.buy()
    e.make_order(count=2,action=action,sign=sign)
    e.bid=D(".99")
    a.run_once()
    assert not e.sent and not e.cancelled and not a.healthy


def test_user_partial_sale_updates_remaining_inventory_and_realized_pnl(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.buy();a.run_once()
    sale=e.make_order(count=4,price=".45",action="sell");e.fill(sale,4)
    a.run_once()
    assert m._plans[T][0]["quantity"]==6 and m._plans[T][0]["realized"]==D(".20")
    e.bid=D(".41");a.run_once();a.run_once()
    assert e.held[T]==0 and D(m.ledger(T)["profit_summary"][oid]["realized_gross"])==D(".26")


def test_pre_activation_inventory_is_not_adopted_on_first_enable(tmp_path):
    e=Exchange();e.buy();e.clock_value+=2
    m=ManualLimitExitWorker(e,tmp_path/"manual.json",clock=e.clock,emit=lambda *a,**k:None)
    a=TakeProfitMonitor(e,lambda:{"markets":{}},tmp_path/"auto.json",pairs={D(".45"):D(".55")},clock=e.clock,manual_exits=m)
    e.bid=D(".99");a.run_once()
    assert not e.sent and not m.ledger(T)["managed_fills"]


def test_filled_manual_sell_is_not_adopted_as_a_new_manual_buy(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.make_order(count=5,action="sell");e.fill(oid,5)
    e.no_bid=D(".99");a.run_once()
    assert not e.sent and not m.ledger(T)["managed_fills"]


def test_out_of_scope_positions_are_ignored(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy(ticker="OTHER-TEST");e.bid=D(".99");a.run_once()
    assert not e.sent and not m.state["markets"]


def test_market_close_preserves_unresolved_ack_but_never_reposts(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".43");e.lose_ack=True
    a.run_once();assert m.pending(T)
    e.clock_value=e.close
    a.run_once()
    assert m.pending(T) and len(e.sent)==1 and m.ledger(T)["window_closed"]


def test_restart_recovers_lost_ack_without_duplicate_sale(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".43");e.lose_ack=True
    a.run_once();assert not a.healthy and m.pending(T)
    e.lose_ack=False
    restarted=ManualLimitExitWorker(e,m.path,clock=e.clock,emit=lambda *a,**k:None)
    a2=TakeProfitMonitor(e,lambda:copy.deepcopy(state),a.path,pairs={D(".45"):D(".55")},clock=e.clock,manual_exits=restarted)
    a2.run_once();a2.run_once()
    assert len(e.sent)==1 and not restarted.pending(T) and e.held[T]==0 and a2.healthy


def test_unknown_ack_missing_from_order_history_stays_blocked(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".43");e.lose_ack=True;a.run_once()
    e.hidden_orders.add(list(e.remote)[-1]);e.lose_ack=False
    for _ in range(4):e.clock_value+=4;a.run_once()
    assert m.pending(T) and len(e.sent)==1 and not a.healthy


def test_delayed_manual_exit_fills_cannot_be_reused_as_bot_inventory(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    auto_buy(e,state);e.buy();e.bid=D(".43");a.run_once()
    e.hidden_fills.add(e.history[-1]["fill_id"])
    a.run_once()
    assert not a.healthy and len(e.sent)==1
    e.hidden_fills.clear();a.run_once()
    assert a.healthy and a.bot_inventory(T,state["markets"][T],e.held[T])==10


@pytest.mark.parametrize("code", [400,401,403,404,422,429])
def test_definitive_post_rejections_release_only_manual_pending_intent(tmp_path,code):
    e,state,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".43");e.fail_post=KalshiAPIError(code,"rejected",retry_after=5)
    a.run_once()
    assert not m.pending(T) and not e.sent and state=={"markets":{}}
    assert m.ledger(T)["retry_after"]>e.clock()


@pytest.mark.parametrize("code", [409,500,503])
def test_ambiguous_post_errors_preserve_manual_intent(tmp_path,code):
    e,_,m,a,_=setup(tmp_path)
    e.buy();e.bid=D(".43");e.fail_post=KalshiAPIError(code,"unknown")
    a.run_once()
    assert m.pending(T) and not e.sent


def test_persist_failure_prevents_submission(tmp_path,monkeypatch):
    e,_,m,a,_=setup(tmp_path)
    e.buy();a.run_once();e.bid=D(".43")
    monkeypatch.setattr(m.transport,"save",lambda:(_ for _ in ()).throw(OSError("disk full")))
    a.run_once()
    assert not e.sent and not a.healthy


def test_dry_run_computes_target_without_a_post(tmp_path):
    e,_,m,a,events=setup(tmp_path,dry_run=True)
    e.buy();e.bid=D(".43");a.run_once()
    assert not e.sent and not m.pending(T)
    assert any(name=="MANUAL_DRY_RUN_TARGET" for name,_ in events)


def test_unreachable_goal_has_no_silent_lower_profit_fallback(tmp_path):
    e,_,m,a,events=setup(tmp_path)
    e.buy(1,".98");e.bid=D(".99");a.run_once()
    assert a.healthy and not e.sent
    assert any(name=="MANUAL_TARGET_UNREACHABLE" for name,_ in events)


def test_market_pause_does_not_post_manual_exit(tmp_path):
    e,_,_,a,_=setup(tmp_path)
    e.buy();e.bid=D(".99");e.status="inactive";a.run_once()
    assert not e.sent


def test_schema_or_account_change_fails_closed(tmp_path):
    e,_,m,_,_=setup(tmp_path)
    e.key_id="another-account"
    with pytest.raises(ManualExitBlocked):ManualLimitExitWorker(e,m.path,clock=e.clock)


@pytest.mark.parametrize("sign",[1,-1])
@pytest.mark.parametrize("raw,step,expected",[(".425",".01",".43"),(".425",".001",".425"),(".9011",".005",".905")])
def test_price_grid_rounding_preserves_goal_for_yes_and_no(sign,raw,step,expected):
    assert round_target(D(raw),[{"start":"0","end":"1","step":step}],sign)==D(expected)


@pytest.mark.parametrize("raw",["1","1.1","2"])
def test_unreachable_price_rejected(raw):
    with pytest.raises(TargetUnreachable):round_target(D(raw),None,1)


@pytest.mark.parametrize("step",["0","NaN",".00001","-1"])
def test_invalid_grid_is_an_error_not_a_lower_price_fallback(step):
    with pytest.raises(ManualExitBlocked):round_target(D(".45"),[{"start":"0","end":"1","step":step}],1)


def test_discovery_follows_all_pages_and_only_primary_allowed_series(tmp_path,monkeypatch):
    e,_,m,_,_=setup(tmp_path)
    calls=[]
    def request(method,path,params=None,**kwargs):
        calls.append(dict(params))
        assert params['subaccount']==0
        if not params.get('cursor'):
            return {'market_positions':[{'ticker':'OTHER-TEST','position_fp':'2'}], 'cursor':'next'}
        return {'market_positions':[{'ticker':T,'position_fp':'3','subaccount_number':0},
            {'ticker':'KXBTC15M-SECONDARY','position_fp':'4','subaccount_number':1}], 'cursor':''}
    monkeypatch.setattr(e,'request',request)
    assert m.discover()=={T} and len(calls)==2


def test_discovery_repeated_cursor_fails_closed(tmp_path,monkeypatch):
    e,_,m,_,_=setup(tmp_path)
    monkeypatch.setattr(e,'request',lambda *a,**k:{'market_positions':[], 'cursor':'loop'})
    with pytest.raises(ManualExitBlocked):m.discover()


def test_previously_managed_opening_cannot_disappear(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    oid=e.buy();a.run_once()
    e.history.clear();e.held[T]=D(0)
    e.remote[oid].update(fill_count_fp='0',remaining_count_fp='0')
    a.run_once()
    assert not a.healthy and not e.sent


def test_managed_fill_cannot_be_reassigned_to_bot(tmp_path):
    e,state,m,a,_=setup(tmp_path)
    oid=e.buy(10,'.45');a.run_once()
    state['markets'][T]={'close_timestamp':e.close,'entry_intents':[{
        'order_id':oid,'client_id':e.remote[oid]['client_order_id'],'side':'YES',
        'price':'.45','exit_target':'.55','quantity':'10','entry_closed':True}]}
    e.bid=D('.90');a.run_once()
    assert not a.healthy and not e.sent


def test_zero_fill_ioc_pauses_then_retries_with_new_id_after_terminal_proof(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy();e.bid=D('.43');e.liquidity=D(0);a.run_once();a.run_once()
    assert len(e.sent)==1 and not m.pending(T)
    a.run_once();assert len(e.sent)==1
    e.clock_value+=4;e.liquidity=D(10);a.run_once();a.run_once()
    assert len(e.sent)==2 and e.held[T]==0
    assert e.sent[0]['client_order_id']!=e.sent[1]['client_order_id']


def test_legacy_action_fields_optional_when_canonical_direction_present(tmp_path):
    e,_,m,a,_=setup(tmp_path)
    e.buy()
    for row in [*e.remote.values(),*e.history]:
        row.pop('side');row.pop('action')
    # Initial observation needs no deprecated side/action fields.
    a.run_once()
    assert a.healthy and len(m._plans[T])==1


def test_unknown_order_status_does_not_create_exit(tmp_path):
    e,_,_,a,_=setup(tmp_path)
    oid=e.buy();e.remote[oid]['status']='unrecognized';e.bid=D('.90');a.run_once()
    assert not e.sent and not a.healthy


def test_other_subaccount_resting_sell_cannot_block_primary(tmp_path):
    e,_,_,a,_=setup(tmp_path)
    e.buy();e.make_order(action='sell',subaccount=1);e.bid=D('.43');a.run_once();a.run_once()
    assert a.healthy and len(e.sent)==1 and e.held[T]==0


@pytest.mark.parametrize('seed',range(20))
def test_random_partial_fills_preserve_goal_and_quantity(tmp_path,seed):
    import random
    rng=random.Random(seed)
    sign=rng.choice([1,-1]);e,_,m,a,_=setup(tmp_path)
    count=rng.randint(3,20)
    oid=e.make_order(sign=sign,count=count,price='.60')
    first=rng.randint(1,count)
    e.fill(oid,first,price=str(D(rng.randint(20,60))/100));a.run_once()
    if first<count:
        e.fill(oid,count-first,price=str(D(rng.randint(20,60))/100));a.run_once()
    assert len(m._plans[T])==1
    e.bid=e.no_bid=D('.99')
    for _ in range(count*2+4):
        e.liquidity=D(rng.randint(1,3));a.run_once()
        if e.held[T]==0 and not m.pending(T):break
    assert e.held[T]==0 and a.healthy
    summary=m.ledger(T)['profit_summary'][oid]
    assert D(summary['realized_gross'])>=D('.25')
    exits=list(m.ledger(T)['exit_orders'].values())
    assert sum(D(exit['filled']) for exit in exits)==count
