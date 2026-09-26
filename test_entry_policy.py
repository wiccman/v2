"""Offline integration tests for the market cap, expiry and cancellation recovery."""
import copy
import json
from decimal import Decimal as D

import pytest
import bot
import entry_policy
from kalshi import KalshiClient, KalshiAPIError
from test_five_minute_exits import cycle_setup


def test_all_entry_routes_share_six_dollars_and_restart_does_not_refund(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    monkeypatch.setattr(bot, "BUDGET", D("2"))
    monkeypatch.setattr(bot, "MARKET_BUDGET", D("6"))
    saved = []
    monkeypatch.setattr(bot, "save_state", lambda s: saved.append(copy.deepcopy(s)))
    original = fake._order

    def assert_reserved_before_post(*args, **kwargs):
        intent = saved[-1]["markets"]["TEST"]["entry_intents"][-1]
        assert intent["client_id"] == kwargs["client_order_id"]
        assert D(intent["reserved_dollars"]) > 0
        return original(*args, **kwargs)

    monkeypatch.setattr(fake, "_order", assert_reserved_before_post)
    # Spot uses the first allowance, then dual and historical compete with
    # regular signals for the same remaining dollars.
    bot.funded_entry(record, state, "TEST", "YES", D("0.45"), closed, "spot")
    bot.place_dual_limit_buys(record, "TEST", closed, state=state)
    bot.place_historical_strike_entries(record, "TEST", D("100010"), closed, state=state)
    bot.funded_entry(record, state, "TEST", "NO", D("0.45"), closed, "regular")
    spent = sum(D(i["reserved_dollars"]) for i in record["entry_intents"])
    assert spent == D("4.80")  # Two full orders; leftover cannot fund another five.
    assert sum(q * (p if side == "bid" else 1 - p) for side, q, p, _ in fake.entries) <= D("6")
    assert len(fake.entries) == 2
    assert all(q == D("5") for _, q, _, _ in fake.entries)
    restored = json.loads(json.dumps(state))
    record = restored["markets"]["TEST"]
    for i in record["entry_intents"]:
        i["entry_closed"] = True  # Cancel, fill or sale never replenishes allowance.
    result, quantity = bot.funded_entry(record, restored, "TEST", "YES", D("0.45"), closed, "regular")
    assert result == {} and quantity == 0
    new_record = {}
    assert entry_policy.reserve(new_record, "YES", D("0.45"), D("2"), D("5"), 360, "spot")


def test_failed_or_ambiguous_post_retains_reservation(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    monkeypatch.setattr(bot, "BUDGET", D("5"))
    monkeypatch.setattr(fake, "place_entry", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("ack lost")))
    with pytest.raises(TimeoutError):
        bot.funded_entry(record, state, "TEST", "YES", D("0.45"), closed, "regular")
    restored = json.loads(json.dumps(state))
    intent = restored["markets"]["TEST"]["entry_intents"][0]
    assert intent["client_id"] and not intent["entry_closed"]
    assert D(intent["reserved_dollars"]) == D("2.40")
    assert entry_policy.reserve(restored["markets"]["TEST"], "YES", D("0.45"), D("5"), D("4.19"), 360, "regular") is None


@pytest.mark.parametrize("elapsed", [299, 300, 359, 360, 361])
def test_cancellation_uses_six_minute_market_boundary(monkeypatch, elapsed):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    record.update(orders=["buy"], dual_limit_attempted=True)
    monkeypatch.setattr(bot, "DUAL_LIMIT_BUYS_ENABLED", False)
    monkeypatch.setattr(bot, "HISTORICAL_STRIKE_ENABLED", False)
    monkeypatch.setattr(bot, "MAX_BUYS", 0)
    bot.cycle(state)
    assert fake.cancelled == (["buy"] if elapsed >= 360 else [])
    assert not fake.entries
    assert fake.exits == []  # Exits are owned by the independent worker.


def test_cancel_error_does_not_skip_other_orders_or_exits_and_retries(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    record["orders"] = ["a-failed", "b-success"]
    cancel = fake.cancel
    def flaky(oid, ticker):
        if oid == "a-failed":
            raise KalshiAPIError(404, "wrong shard or unavailable")
        return cancel(oid, ticker)
    monkeypatch.setattr(fake, "cancel", flaky)
    monkeypatch.setattr(fake, "order", lambda oid: {"order_id": oid, "status": "resting"})
    bot.cycle(state)
    assert record["orders"] == ["a-failed"]
    assert fake.cancelled == ["b-success"]
    assert fake.exits == []  # Exits are owned by the independent worker.
    monkeypatch.setattr(fake, "cancel", cancel)
    bot.reconcile_entries(state)
    assert record["orders"] == []


def test_cancel_sweep_runs_when_market_discovery_fails(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 361)
    record["orders"] = ["old-buy"]
    monkeypatch.setattr(bot, "active_market", lambda now: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        bot.cycle(state)
    assert fake.cancelled == ["old-buy"]
    assert record["orders"] == []


def test_ambiguous_order_recovered_and_canceled_without_touching_foreign_exit(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    record["entry_intents"] = [dict(client_id="persisted", kind="historical", side="YES", cancel_at=clock[0], reserved_dollars="2.24")]
    remote = [{"order_id": "recovered", "client_order_id": "persisted"},
              {"order_id": "manual-exit", "client_order_id": "foreign"}]
    monkeypatch.setattr(fake, "all_orders", lambda ticker: remote)
    bot.reconcile_entries(state)
    assert fake.cancelled == ["recovered"]
    assert record["entry_intents"][0]["entry_closed"] is True
    assert record["historical_strike_orders"][-1]["order_id"] == "recovered"


def test_recovery_failure_does_not_block_known_order_cancellation(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    record["entry_intents"] = [dict(client_id="unknown", kind="dual", reserved_dollars="2.24")]
    record["orders"] = ["known"]
    monkeypatch.setattr(fake, "all_orders", lambda ticker: (_ for _ in ()).throw(TimeoutError()))
    bot.reconcile_entries(state)
    assert fake.cancelled == ["known"]
    assert not record["entry_intents"][0].get("entry_closed")


def test_upgrade_never_assumes_existing_market_has_full_budget(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    del record["entry_intents"]
    record.update(buys=2, orders=["legacy"])
    bot.cycle(state)
    assert record["entry_budget_legacy"] is True
    assert not fake.entries
    assert fake.cancelled == ["legacy"]
    assert fake.exits == []  # Exits are owned by the independent worker.


def test_wire_uses_routed_cancel_and_separate_submit_deadline(monkeypatch):
    fake = KalshiClient()
    calls = []
    monkeypatch.setattr(fake, "request", lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {})
    fake.cancel("order", "MARKET")
    assert calls[-1][2]["params"] == {"market_ticker": "MARKET", "exchange_index": -1}
    calls.clear()
    monkeypatch.setattr(bot.time, "time", lambda: 300)
    assert fake.place_entry("M", "YES", D("1"), D("0.39"), 360, submit_before=300) == {}
    assert not calls


def test_orders_follows_every_page(monkeypatch):
    fake = KalshiClient()
    seen = []
    def response(method, path, params=None, **kwargs):
        seen.append(dict(params))
        return {"orders": [{"order_id": "later"}], "cursor": ""} if params.get("cursor") else {"orders": [], "cursor": "next"}
    monkeypatch.setattr(fake, "request", response)
    assert fake.orders("M", "resting") == [{"order_id": "later"}]
    assert seen[-1]["cursor"] == "next"


@pytest.mark.parametrize("legacy", [None, "4", "6", "10", "100"])
def test_fixed_twenty_five_dollar_cap_ignores_legacy_setting(monkeypatch, legacy):
    if legacy is None:
        monkeypatch.delenv("MARKET_BUDGET_DOLLARS", raising=False)
    else:
        monkeypatch.setenv("MARKET_BUDGET_DOLLARS", legacy)
    assert entry_policy.market_budget() == D("25")


def test_twenty_five_dollar_allowance_is_shared_and_survives_restart():
    record = {}
    for price in ("0.45", "0.47", "0.49", "0.52", "0.55", "0.56", "0.61", "0.73", "0.85"):
        entry_policy.reserve(record, "YES", D(price), D("2"), entry_policy.market_budget(), 360, "test")
    while entry_policy.reserve(record, "YES", D("0.45"), D("0.01"), entry_policy.market_budget(), 360, "test"):
        pass
    spent = sum(D(i["reserved_dollars"]) for i in record["entry_intents"])
    assert D("12.90") < spent <= D("15")
    assert all(D(i["quantity"]) == 5 for i in record["entry_intents"])
    restored = copy.deepcopy(record)
    assert entry_policy.reserve(restored, "YES", D("0.45"), D("2"), entry_policy.market_budget(), 360, "test") is None


def test_entry_gateway_enforces_current_bias_and_logs_reason(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    record["signal"] = {"prediction": "YES", "base_confidence": "HIGH"}
    record["previous_bias"] = "YES"
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, ticker="", **values: events.append((event, values)))
    result, quantity = bot.funded_entry(record, state, "TEST", "NO", D("0.45"), closed, "spot")
    assert result == {} and quantity == 0
    decision = json.loads(events[-1][1]["details"])
    assert decision == {
        "selected_side": "NO", "current_bias": "YES", "previous_bias": "YES",
        "entry_price": "0.45", "entry_reason": "selected_side_opposes_current_bias", "decision": "SKIP",
    }
    assert not fake.entries


@pytest.mark.parametrize("current,previous", [("YES", "NO"), ("NO", "YES")])
def test_entry_gateway_buys_current_bias_despite_previous_conflict(monkeypatch, current, previous):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    record["signal"] = {"prediction": current, "base_confidence": "MODERATE"}
    record["previous_bias"] = previous
    result, quantity = bot.funded_entry(record, state, "TEST", current, D("0.45"), closed, "regular")
    assert result.get("order_id") and quantity > 0
    assert len(fake.entries) == 1


def test_balance_rejection_releases_only_failed_intent_and_survives_restart(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    monkeypatch.setattr(bot, 'BUDGET', D('2'))
    accepted = fake.place_entry
    bot.funded_entry(record, state, 'TEST', 'YES', D('0.45'), closed, 'regular')
    original_reserved = record['entry_intents'][0]['reserved_dollars']
    def rejected(*args, **kwargs):
        raise KalshiAPIError(400, 'insufficient balance', code='insufficient_balance')
    monkeypatch.setattr(fake, 'place_entry', rejected)
    for _ in range(3):
        with pytest.raises(KalshiAPIError):
            bot.funded_entry(record, state, 'TEST', 'YES', D('0.55'), closed, 'regular')
        assert bot.funded_entry(record, state, 'TEST', 'YES', D('0.55'), closed, 'regular') == ({}, 0)
        clock[0] += 30
    restored = json.loads(json.dumps(state))
    record = restored['markets']['TEST']
    assert record['entry_intents'][0]['reserved_dollars'] == original_reserved
    assert all(i['reserved_dollars'] == '0' and i['entry_closed'] and D(i['released_dollars']) > 0
               for i in record['entry_intents'][1:])
    monkeypatch.setattr(fake, 'place_entry', accepted)
    result, quantity = bot.funded_entry(record, restored, 'TEST', 'YES', D('0.55'), closed, 'regular')
    assert result['order_id'] and quantity > 0
    assert sum(D(i['reserved_dollars']) for i in record['entry_intents']) <= D('10')


@pytest.mark.parametrize('status,code', [(400, None), (400, 'unknown_error'),
    (409, 'insufficient_balance'), (500, 'insufficient_balance'), (429, None)])
def test_unproven_rejection_never_refunds(monkeypatch, status, code):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    def rejected(*args, **kwargs):
        raise KalshiAPIError(status, 'possibly ambiguous', code=code)
    monkeypatch.setattr(fake, 'place_entry', rejected)
    with pytest.raises(KalshiAPIError):
        bot.funded_entry(record, state, 'TEST', 'YES', D('0.45'), closed, 'regular')
    intent = record['entry_intents'][0]
    assert D(intent['reserved_dollars']) > 0
    assert 'released_dollars' not in intent


def test_cannot_refund_order_with_exchange_id():
    intent = {'order_id': 'accepted', 'reserved_dollars': '2'}
    with pytest.raises(ValueError):
        entry_policy.release_unsubmitted(intent, 'insufficient_balance')
    assert intent['reserved_dollars'] == '2'


def test_api_error_preserves_structured_rejection_code(monkeypatch):
    import requests
    import kalshi
    response = requests.Response()
    response.status_code = 400
    response._content = b'{"error":{"code":"insufficient_balance","message":"insufficient balance"}}'
    monkeypatch.setattr(kalshi.requests, 'request', lambda *a, **k: response)
    with pytest.raises(KalshiAPIError) as raised:
        KalshiClient().request('GET', '/test')
    assert raised.value.code == 'insufficient_balance'


@pytest.mark.parametrize("kind", ["opening_bias", "regular", "dual", "historical", "spot", "late_bias"])
def test_each_entry_route_requests_five_despite_old_dollar_budget(monkeypatch, kind):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    result, quantity = bot.funded_entry(record, state, "TEST", "YES", D("0.45"), closed, kind, order_budget=D("0.01"))
    assert result["order_id"] and quantity == D("5")
    assert fake.entries[0][1] == D("5")


def test_cap_boundary_never_shrinks_quantity_or_resets_old_spending():
    record = {"entry_intents": [{"quantity": "0.87", "reserved_dollars": "12.55"}]}
    assert entry_policy.reserve(record, "YES", D("0.45"), D("0.01"), D("25"), 480, "regular")["quantity"] == "5"
    assert sum(D(i["reserved_dollars"]) for i in record["entry_intents"]) == D("14.95")
    assert entry_policy.reserve(record, "YES", D("0.45"), D("100"), D("25"), 480, "regular") is None
    record = {"entry_intents": [{"reserved_dollars": "12.61"}]}
    assert entry_policy.reserve(record, "YES", D("0.45"), D("100"), D("25"), 480, "regular") is None
    assert len(record["entry_intents"]) == 1
