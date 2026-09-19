"""Offline integration tests for the market cap, expiry and cancellation recovery."""
import copy
import json
from decimal import Decimal as D

import pytest
import bot
import entry_policy
from kalshi import KalshiClient, KalshiAPIError
from test_five_minute_exits import cycle_setup


def test_all_entry_routes_share_five_dollars_and_restart_does_not_refund(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 60)
    monkeypatch.setattr(bot, "BUDGET", D("2"))
    monkeypatch.setattr(bot, "MARKET_BUDGET", D("5"))
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
    bot.funded_entry(record, state, "TEST", "YES", D("0.32"), closed, "spot")
    bot.place_dual_limit_buys(record, "TEST", closed, state=state)
    bot.place_historical_strike_entries(record, "TEST", D("100010"), closed, state=state)
    bot.funded_entry(record, state, "TEST", "NO", D("0.32"), closed, "regular")
    spent = sum(D(i["reserved_dollars"]) for i in record["entry_intents"])
    assert D("4.99") < spent <= D("5")
    assert sum(q * (p if side == "bid" else 1 - p) for side, q, p, _ in fake.entries) <= D("5")
    assert len(fake.entries) == 6
    restored = json.loads(json.dumps(state))
    record = restored["markets"]["TEST"]
    for i in record["entry_intents"]:
        i["entry_closed"] = True  # Cancel, fill or sale never replenishes allowance.
    result, quantity = bot.funded_entry(record, restored, "TEST", "YES", D("0.32"), closed, "regular")
    assert result == {} and quantity == 0
    new_record = {}
    assert entry_policy.reserve(new_record, "YES", D("0.32"), D("2"), D("5"), 360, "spot")


def test_failed_or_ambiguous_post_retains_reservation(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    monkeypatch.setattr(bot, "BUDGET", D("5"))
    monkeypatch.setattr(fake, "place_entry", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("ack lost")))
    with pytest.raises(TimeoutError):
        bot.funded_entry(record, state, "TEST", "YES", D("0.32"), closed, "regular")
    restored = json.loads(json.dumps(state))
    intent = restored["markets"]["TEST"]["entry_intents"][0]
    assert intent["client_id"] and not intent["entry_closed"]
    assert D(intent["reserved_dollars"]) > D("4.99")
    assert entry_policy.reserve(restored["markets"]["TEST"], "YES", D("0.32"), D("5"), D("5"), 360, "regular") is None


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
    assert fake.place_entry("M", "YES", D("1"), D("0.32"), 360, submit_before=300) == {}
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


def test_budget_setting_cannot_exceed_five(monkeypatch):
    monkeypatch.setenv("MARKET_BUDGET_DOLLARS", "7")
    assert entry_policy.market_budget() == 5
    monkeypatch.setenv("MARKET_BUDGET_DOLLARS", "4")
    assert entry_policy.market_budget() == 4

