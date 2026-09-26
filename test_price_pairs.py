"""Offline tests for entry-tier attribution and paired reduce-only exits."""
import copy
import json
from decimal import Decimal as D
from datetime import datetime, timezone

import pytest
import bot
from kalshi import KalshiClient
from price_pairs import parse_pairs, paired_inventory
from take_profit import TakeProfitMonitor
from test_take_profit_monitor import Exchange
from test_five_minute_exits import cycle_setup


class PairExchange(Exchange):
    def __init__(self, bid="0.20", liquidity="100"):
        super().__init__(quantity="0", bid=bid, liquidity=liquidity)
        self.history = []
        self.intents = []

    def fill(self, order_id, sign, quantity, price="0.20"):
        index = len(self.history) + 1
        self.history.append(dict(fill_id=f"f{index:04}", order_id=order_id, ticker="T",
            book_side="bid" if sign > 0 else "ask", count_fp=str(quantity),
            ts=100 + index, yes_price_dollars=price, subaccount_number=0))

    def buy(self, price, quantity, sign=1, actual_price="0.20"):
        order_id = f"entry-{len(self.intents)}"
        self.intents.append(dict(order_id=order_id, client_id=order_id + "-client",
            side="YES" if sign > 0 else "NO", price=str(price),
            exit_target=str(parse_pairs("32:39,39:46")[D(price)])))
        self.remote[order_id] = dict(order_id=order_id, client_order_id=order_id + "-client",
                                    status="executed", fill_count_fp=str(quantity))
        self.held += sign * D(quantity)
        self.fill(order_id, sign, D(quantity), actual_price)
        return order_id

    def manual(self, sign, quantity):
        self.held += sign * D(quantity)
        self.fill("manual", sign, D(quantity))

    def all_fills(self, ticker):
        return copy.deepcopy(list(reversed(self.history)))

    def request(self, method, path, params=None, body=None, auth=False):
        before = self.held
        try:
            return super().request(method, path, params, body, auth)
        finally:
            delta = self.held - before
            if delta:
                order = next(o for o in self.remote.values() if o["client_order_id"] == body["client_order_id"])
                self.fill(order["order_id"], 1 if delta > 0 else -1, abs(delta))


def paired_monitor(tmp_path, exchange, path=None):
    entries = {"markets": {"T": {"close_timestamp": 1900, "entry_intents": exchange.intents}}}
    events = []
    svc = TakeProfitMonitor(exchange, lambda: entries, path or tmp_path / "pairs.json",
        pairs=parse_pairs("32:39,39:46"), clock=lambda: 1000, emit=lambda event, **d: events.append((event, d)))
    return svc, entries, events


@pytest.mark.parametrize("sign,wire", [(1, ["0.3900", "0.4600"]), (-1, ["0.6100", "0.5400"])])
def test_mixed_tiers_exit_only_their_own_quantities_at_each_target(tmp_path, sign, wire):
    e = PairExchange(bid="0.40")
    e.buy("0.32", "2", sign)
    e.buy("0.39", "3", sign, actual_price="0.32")  # Better fill retains 39->46 mapping.
    m, _, events = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == sign * D(3)
    assert e.submissions[0]["count"] == "2" and e.submissions[0]["price"] == wire[0]
    m.run_once()
    assert e.held == sign * D(3)
    assert e.submissions[-1]["count"] == "3" and e.submissions[-1]["price"] == wire[1]
    e.bid = D("0.46")
    m.run_once()
    m.run_once()
    assert e.held == 0 and m.healthy
    assert all(o["reduce_only"] and o["time_in_force"] == "immediate_or_cancel" for o in e.submissions)
    assert {d["target"] for event, d in events if event == "TP_FILL"} == {"0.39", "0.46"}


@pytest.mark.parametrize("sign", [1, -1])
def test_partial_exits_and_restart_preserve_remaining_tier_quantities(tmp_path, sign):
    e = PairExchange(bid="0.40", liquidity="0.75")
    e.buy("0.32", "2", sign); e.buy("0.39", "3", sign)
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert e.held == sign * D("4.25")
    m2, _, _ = paired_monitor(tmp_path, e, m.path)
    m2.run_once()  # 46 target has not been reached.
    e.liquidity = D(100)
    m2.run_once()
    assert e.submissions[-1]["count"] == "1.25"
    assert e.held == sign * D(3)
    e.bid = D("0.46")
    m2.run_once(); m2.run_once()
    assert e.held == 0 and m2.healthy


def test_lost_exit_ack_is_recovered_without_duplicate_or_target_mixup(tmp_path):
    e = PairExchange(bid="0.40")
    e.buy("0.32", "2"); e.buy("0.39", "3")
    e.lose_ack = True
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert e.held == 3 and not m.healthy
    e.lose_ack = False
    m2, _, _ = paired_monitor(tmp_path, e, m.path)
    m2.run_once()
    assert len(e.submissions) == 2
    assert e.submissions[-1]["price"] == "0.4600" and e.submissions[-1]["count"] == "3"


def test_lost_entry_ack_matches_saved_client_id_to_fills(tmp_path):
    e = PairExchange(bid="0.39")
    e.buy("0.32", "0.37")
    del e.intents[0]["order_id"]
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.submissions[0]["count"] == "0.37"
    assert e.held == 0


def test_delayed_fill_history_pauses_instead_of_selling_at_wrong_target(tmp_path):
    e = PairExchange(bid="0.50")
    e.buy("0.32", "2"); e.buy("0.39", "3")
    missing = e.history.pop()
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions
    e.history.append(missing)
    m.run_once()
    assert m.healthy and e.submissions[0]["count"] == "2"


def test_confirmed_exit_must_appear_in_fill_history_before_reusing_inventory(tmp_path):
    e = PairExchange(bid="0.40")
    e.buy("0.32", "2"); e.buy("0.39", "3")
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    missing = e.history.pop()
    m.run_once()
    assert not m.healthy and len(e.submissions) == 1
    e.history.append(missing)
    m.run_once()
    assert m.healthy and e.submissions[-1]["price"] == "0.4600"


def test_manual_reduction_uses_fifo_and_does_not_sell_expensive_tier_at_31(tmp_path):
    e = PairExchange(bid="0.40")
    e.buy("0.32", "2"); e.buy("0.39", "3")
    e.manual(-1, "2")
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 3
    assert e.submissions[0]["price"] == "0.4600"


def test_opposing_entry_nets_oldest_lots_before_arming_remainder(tmp_path):
    e = PairExchange(bid="0.40")
    e.buy("0.32", "2"); e.buy("0.39", "3"); e.buy("0.32", "3", sign=-1)
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 2
    assert e.submissions[0]["price"] == "0.4600" and e.submissions[0]["count"] == "2"


def test_unknown_manual_inventory_is_not_given_an_invented_target(tmp_path):
    e = PairExchange(bid="0.50")
    e.manual(1, "2")
    m, _, events = paired_monitor(tmp_path, e)
    m.run_once()
    assert not m.healthy and not e.submissions
    assert any("Untracked inventory" in d.get("error", "") for _, d in events)


def test_unfilled_then_partial_entry_only_arms_verified_quantity(tmp_path):
    e = PairExchange()
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and not e.submissions
    e.buy("0.39", "0.13")
    m.run_once()
    assert e.submissions[-1]["count"] == "0.13" and e.submissions[-1]["price"] == "0.4600"


def test_old_25_cent_entries_migrate_without_resetting_state(tmp_path):
    e = PairExchange(bid="0.39")
    e.buy("0.32", "1")
    del e.intents[0]["exit_target"]  # v0.9.4 intent.
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert e.submissions[0]["price"] == "0.3900" and e.held == 0


def test_both_targets_keep_running_after_entry_cutoff_until_close(tmp_path):
    e = PairExchange(bid="0.20")
    e.buy("0.32", "1"); e.buy("0.39", "1")
    m, _, _ = paired_monitor(tmp_path, e)
    m.clock = lambda: 1850
    m.run_once(); m.run_once()
    assert [o["price"] for o in e.submissions] == ["0.3900", "0.4600"]
    m.clock = lambda: 1900
    m.run_once()
    assert len(e.submissions) == 2


def test_pair_target_is_saved_before_five_contract_entry_post(monkeypatch):
    e, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    saved = []
    monkeypatch.setattr(bot, "save_state", lambda s: saved.append(copy.deepcopy(s)))
    place = e.place_entry
    def checked(ticker, side, quantity, price, *args, **kwargs):
        intent = saved[-1]["markets"]["TEST"]["entry_intents"][-1]
        assert D(intent["exit_target"]) == {D(p): D(t) for p, t in [("0.38", "0.43"), ("0.39", "0.46"), ("0.49", "0.59"), ("0.55", "0.62"), ("0.56", "0.61"), ("0.61", "0.70")]}[price]
        assert quantity == D("5")
        return place(ticker, side, quantity, price, *args, **kwargs)
    monkeypatch.setattr(e, "place_entry", checked)
    result = list(bot.paired_entries(record, state, "TEST", "YES", closed, "regular"))
    assert [p for p, _, _ in result] == [D(p) for p in ("0.38", "0.39", "0.49", "0.55", "0.56", "0.61")]
    assert sum(p * q for p, _, q in result) == D("14.90")
    assert sum(D(i["reserved_dollars"]) for i in record["entry_intents"]) <= bot.MARKET_BUDGET
    bot.reconcile_entries(state)
    assert not e.cancelled  # A 39-cent order is a supported price, not a legacy mismatch.


def test_no_single_target_fallback_when_exit_monitor_missing(monkeypatch):
    e, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    monkeypatch.setattr(bot, "EXIT_MONITOR", None)
    bot.cycle(state)
    assert not e.entries and not e.exits


def test_fill_pagination_and_primary_account_filter(monkeypatch):
    client = KalshiClient()
    seen = []
    def response(method, path, params=None, **kwargs):
        seen.append(dict(params))
        return {"fills": [{"fill_id": "second"}], "cursor": ""} if params.get("cursor") else {"fills": [{"fill_id": "first"}], "cursor": "next"}
    monkeypatch.setattr(client, "request", response)
    assert [f["fill_id"] for f in client.all_fills("T")] == ["first", "second"]
    assert all(p["subaccount"] == 0 and p["ticker"] == "T" for p in seen)
    monkeypatch.setattr(client, "request", lambda *a, **k: {"fills": [], "cursor": "loop"})
    with pytest.raises(RuntimeError, match="repeated"):
        client.all_fills("T")


@pytest.mark.parametrize("value", ["25:25", "39:31", "25:31,25:46", "NaN:31", "25.5:31", "25:100", "25", ""])
def test_invalid_pairs_fail_closed(value):
    with pytest.raises((ValueError, ArithmeticError)):
        parse_pairs(value)


def test_duplicate_fills_do_not_duplicate_inventory():
    e = PairExchange()
    oid = e.buy("0.32", "1.5")
    entries = {oid: {"side": "YES", "target": "0.39"}}
    assert paired_inventory(e.history * 2, entries, {}, e.held, "T") == {D("0.39"): D("1.5")}
    altered = copy.deepcopy(e.history[0]); altered["count_fp"] = "3"
    with pytest.raises(ValueError, match="Conflicting"):
        paired_inventory(e.history + [altered], entries, {}, e.held, "T")


def test_ambiguous_simultaneous_opposite_fills_do_not_guess_tier_allocation():
    e = PairExchange()
    a = e.buy("0.32", "2")
    b = e.buy("0.39", "1", sign=-1)
    e.history[1]["ts"] = e.history[0]["ts"]
    entries = {a: {"side": "YES", "target": "0.39"}, b: {"side": "NO", "target": "0.46"}}
    with pytest.raises(ValueError, match="ambiguous"):
        paired_inventory(e.history, entries, {}, e.held, "T")


def test_rate_limit_and_disk_failure_cannot_send_untracked_paired_exits(tmp_path, monkeypatch):
    from kalshi import KalshiAPIError
    e = PairExchange()
    e.buy("0.39", "1")
    m, _, _ = paired_monitor(tmp_path, e)
    e.read_failure = KalshiAPIError(429, "limit", retry_after="30")
    m.run_once()
    assert not e.submissions and not m.healthy
    e.read_failure = None
    m.clock = lambda: 1029
    m.run_once()
    assert not e.submissions
    m.clock = lambda: 1030
    monkeypatch.setattr(m, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    m.run_once()
    assert not e.submissions and not m.healthy



@pytest.mark.parametrize("sign,wire", [(1, "0.3100"), (-1, "0.6900")])
def test_retired_tier_keeps_saved_exit_after_pair_change(tmp_path, sign, wire):
    e = PairExchange(bid="0.35")
    e.buy("0.32", "2", sign)
    e.intents[0].update(price="0.25", exit_target="0.31")
    m, _, _ = paired_monitor(tmp_path, e)
    m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[0]["price"] == wire


def test_uppercase_kalshi_fill_enums_reconcile_like_lowercase():
    e = PairExchange(bid="0.40")
    oid = e.buy("0.32", "2")
    e.history[0].update(book_side="BID", outcome_side="YES", action="BUY")
    entries = {oid: {"side": "YES", "target": "0.39"}}
    assert paired_inventory(e.history, entries, {}, e.held, "T") == {D("0.39"): D("2")}


def test_fill_direction_error_reports_only_relevant_fields():
    fill = {"fill_id": "f", "ticker": "T", "count_fp": "1", "ts": 1,
            "outcome_side": "MAYBE", "action": "BUY", "order_id": "o"}
    with pytest.raises(ValueError, match="outcome_side"):
        paired_inventory([fill], {}, {}, D("1"), "T")


def test_yes_entry_fill_uses_book_side_when_action_is_sell():
    fill = {"fill_id": "f", "ticker": "T", "count_fp": "1", "ts": 1,
            "book_side": "bid", "outcome_side": "yes", "action": "sell", "order_id": "entry"}
    entries = {"entry": {"side": "YES", "target": "0.39"}}
    assert paired_inventory([fill], entries, {}, D("1"), "T") == {D("0.39"): D("1")}


def test_no_entry_fill_uses_book_side_when_action_is_sell():
    fill = {"fill_id": "f", "ticker": "T", "count_fp": "1", "ts": 1,
            "book_side": "ask", "outcome_side": "no", "action": "sell", "order_id": "entry"}
    entries = {"entry": {"side": "NO", "target": "0.39"}}
    assert paired_inventory([fill], entries, {}, D("-1"), "T") == {D("0.39"): D("-1")}


def test_removed_32_cent_inventory_keeps_original_exit(tmp_path):
    e = PairExchange(bid='0.39')
    e.buy('0.32', '2')
    entries = {'markets': {'T': {'close_timestamp': 1900, 'entry_intents': e.intents}}}
    m = TakeProfitMonitor(e, lambda: entries, tmp_path / 'retired.json',
        pairs=bot.ALL_ENTRY_EXIT_PAIRS, clock=lambda: 1000, emit=lambda *a, **k: None)
    m.run_once()
    assert m.healthy and e.held == 0
    assert e.submissions[0]['price'] == '0.3900'


def test_retired_entry_is_rejected_before_submission(monkeypatch):
    e, record, state, clock, closed = cycle_setup(monkeypatch, 120)
    with pytest.raises(ValueError, match='configured fixed entry limit'):
        bot.funded_entry(record, state, 'TEST', 'YES', D('0.32'), closed, 'regular')
    assert not e.entries and not record['entry_intents']
