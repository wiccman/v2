"""Mobile market buys and exact-order recovery for the resting exit runner."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService
from test_manual_limit_exits import T
from test_resting_manual_exits import RestingExchange


def start(tmp_path):
    from manual_exit_runner import Settings
    path = tmp_path / "state.json"
    for p in (path, path.with_name("state_take_profit.json")):
        p.write_text('{"markets": {}}')
    config = replace(Settings.read({"STATE_PATH": str(path), "MANUAL_EXIT_INCREMENT_CENTS": "3",
                                    "MANUAL_STOP_LOSS_CENTS": "15"}), dry_run=False)
    e = RestingExchange()
    return e, ManualExitService(e, config, clock=e.clock, emit=lambda *a, **kw: None), config


def omit_from_aggregate(e, monkeypatch, hidden):
    original = e.all_orders
    def listed(ticker, status=None):
        rows = original(ticker, status)
        return rows if status else [r for r in rows if r["order_id"] not in hidden]
    monkeypatch.setattr(e, "all_orders", listed)


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_verified_mobile_fills_get_exact_targets_despite_lag(tmp_path, monkeypatch, sign, order_type):
    e, s, config = start(tmp_path)
    buy = e.make_order(count=5, price=".50", sign=sign)
    e.remote[buy]["type"] = order_type
    e.fill(buy, 2, ".46")
    e.fill(buy, 3, ".47")
    omit_from_aggregate(e, monkeypatch, {buy})
    assert s.run_once()
    assert [(r["count"], r["price"]) for r in e.sent] == [
        ("2", "0.4900" if sign == 1 else "0.5100"), ("3", "0.5000")]
    assert any(p.endswith("/" + buy) for p, _ in e.reads)
    resumed = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **kw: None)
    assert resumed.run_once() and len(e.sent) == 2


def test_separately_listed_sell_reserves_size_during_order_recovery(tmp_path, monkeypatch):
    e, s, _ = start(tmp_path)
    buy = e.buy(10, ".42")
    e.remote[buy]["type"] = "market"
    sell = e.make_order(count=7, price=".60", action="sell")
    omit_from_aggregate(e, monkeypatch, {buy, sell})
    assert s.run_once()
    assert [(r["count"], r["price"]) for r in e.sent] == [("3", "0.4500")]
    assert sell not in e.cancelled


@pytest.mark.parametrize("failure", ["not_found", "wrong_account", "wrong_id", "wrong_quantity"])
def test_lookup_must_prove_entry_identity_and_quantity(tmp_path, monkeypatch, failure):
    e, s, _ = start(tmp_path)
    buy = e.buy(5, ".42")
    e.remote[buy]["type"] = "market"
    omit_from_aggregate(e, monkeypatch, {buy})
    if failure == "not_found":
        e.hidden_orders.add(buy)
    else:
        change = {"wrong_account": {"subaccount_number": 1}, "wrong_id": {"order_id": "wrong"},
                  "wrong_quantity": {"fill_count_fp": "4"}}[failure]
        monkeypatch.setattr(e, "order", lambda oid, ticker: {**e.remote[oid], **change})
    assert not s.run_once() and not e.sent


@pytest.mark.parametrize("action", ["sell", "", "unknown"])
def test_market_order_requires_explicit_buy(tmp_path, action):
    e, s, _ = start(tmp_path)
    buy = e.buy(5, ".42")
    e.remote[buy].update(type="market", action=action)
    assert s.run_once() and not e.sent
