"""Offline last-three-minute basket tests. No real exchange/network activity."""
from decimal import Decimal as D
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import late_basket_minute9 as late
import minute8_top2 as policy
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from test_cash_checked_minute9 import CashExchange
from test_single_coin_minute9 import START


class Feed:
    def __init__(self, now, order=None):
        self.now = now
        self.order = order or ["KXETH15M", "KXSOL15M", "KXHYPE15M", "KXBTC15M", "KXXRP15M"]
        self.calls = 0

    def snapshot(self, names, start):
        self.calls += 1
        return [ranked.Observation(series, start, D("100"),
                                  D(str(105 - index)), self.now[0])
                for index, series in enumerate(self.order) if series in names]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    now = [START + 720]
    events = []
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    monkeypatch.setattr(core, "event", lambda event, **details: events.append((event, details)))
    journal = core.Journal(tmp_path / "late.json")
    client = CashExchange()
    client.initial = {series: ".97" for series in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    return now, events, journal, client


def run(journal, client, now, feed, *, live=True, blackout=lambda _: False):
    return late.run_late_pass(
        client, journal, core.DEFAULT_SERIES, D("6.66"), 100,
        live, now[0], provider=feed, clock=lambda: now[0],
        sol_blocked=blackout)


@pytest.mark.parametrize("elapsed,eligible", [
    (719.999, False), (720, True), (750, True),
    (899.5, True), (900, False), (901, False),
])
def test_only_last_three_minutes_have_late_entries(scenario, elapsed, eligible):
    now, _, journal, client = scenario
    now[0] = START + elapsed
    feed = Feed(now)
    run(journal, client, now, feed)
    assert bool(client.posted) == eligible
    if not eligible:
        assert feed.calls == 0


def test_regular_slot_untouched_and_half_active_budget_total(scenario):
    now, events, journal, client = scenario
    journal.data["streak"] = core.next_streak(core.starting_streak(), "loss")
    feed = Feed(now)
    run(journal, client, now, feed)
    assert len(client.posted) == 1
    ticker, side, quantity, price = client.posted[0]
    assert ticker.startswith("KXETH15M-")
    assert side == "YES" and quantity == 1 and price == D(".97")
    assert core.window_entry_count(journal, START * 1000) == 0
    assert journal.data["windows"][core.zone_record_key(ticker, core.ZONE2)]["entry_kind"] == late.LATE_KIND
    now[0] += 2.1
    run(journal, client, now, feed)
    assert len(client.posted) == 2
    assert client.posted[1][0].startswith("KXSOL15M-")
    assert client.posted[1][2] == 2
    assert late.half_budget_reservation(journal, START) == D("3.00")
    assert late.half_budget_reservation(journal, START) <= D("3.33")
    now[0] += 2.1
    run(journal, client, now, feed)
    assert len(client.posted) == 2  # No duplicate purchases on next poll.


def test_small_active_budget_gets_one_contract_total(scenario):
    now, _, journal, client = scenario
    feed = Feed(now)
    run(journal, client, now, feed)
    assert len(client.posted) == 1
    assert client.posted[0][2] == 1
    now[0] += 3
    run(journal, client, now, feed)
    assert len(client.posted) == 1
    assert late.half_budget_reservation(journal, START) <= D("1.11")


def test_btc_first_never_promotes_rank_three(scenario):
    now, _, journal, client = scenario
    feed = Feed(now, ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXHYPE15M", "KXXRP15M"])
    run(journal, client, now, feed)
    assert len(client.posted) == 1
    assert client.posted[0][0].startswith("KXETH15M-")
    now[0] += 2.1
    run(journal, client, now, feed)
    assert len(client.posted) == 1


def test_btc_and_blackout_sol_in_top_two_means_no_buy(scenario):
    now, _, journal, client = scenario
    feed = Feed(now, ["KXBTC15M", "KXSOL15M", "KXETH15M", "KXHYPE15M", "KXXRP15M"])
    run(journal, client, now, feed, blackout=lambda _: True)
    assert client.posted == []


def test_pre_dispatch_solar_blackout_not_bypassed(scenario):
    now, _, journal, client = scenario
    feed = Feed(now, ["KXSOL15M", "KXETH15M", "KXHYPE15M", "KXBTC15M", "KXXRP15M"])
    # One blocked SOL position is discarded, allowing rank-two ETH only.
    run(journal, client, now, feed, blackout=lambda _: True)
    assert len(client.posted) == 1
    assert client.posted[0][0].startswith("KXETH15M-")


@pytest.mark.parametrize("price,allowed", [
    (".88", False), (".8899", False), (".89", True),
    (".895", True), (".96", True), (".97", True),
    (".9701", False), (".975", False), (".98", False),
    (".99", False), (".999", False), ("1.00", False),
])
def test_late_price_band(price, allowed):
    from test_single_coin_minute9 import market
    m = market(yes=price, no=".03", series="KXETH15M")
    result = late.late_plan(m, START, START + 740, "KXETH15M", "YES")
    assert bool(result) == allowed
    if allowed:
        assert D(".89") <= D(result["limit"]) <= D(".97") < late.LATE_TAKE_PROFIT


def test_late_cash_deadline_crossing_never_dispatches(scenario):
    now, _, journal, original = scenario
    now[0] = START + 899.5
    class Slow(CashExchange):
        def balance(self):
            result = super().balance()
            now[0] = START + 900
            return result
    client = Slow()
    client.initial = {x: ".97" for x in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    run(journal, client, now, Feed(now))
    assert client.posted == []
    assert list(journal.data["windows"].values())[0]["outcome"]["status"] == "no_fill"


def test_late_pnl_does_not_change_regular_budget_streak(scenario):
    _, _, journal, _ = scenario
    journal.data["windows"]["regular"] = {"ticker": "regular", "outcome": {
        "status": "loss", "completed_at": 100, "counted": False}}
    journal.data["windows"]["late"] = {"ticker": "late",
        "entry_kind": late.LATE_KIND, "outcome": {
            "status": "win", "net_pnl_dollars": ".03",
            "completed_at": 200, "counted": False}}
    core.apply_new_outcomes(journal)
    assert journal.data["streak"] == core.next_streak(core.starting_streak(), "loss")
    assert journal.data["windows"]["late"]["outcome"]["counted"]
    assert core.Journal(journal.path).data == journal.data


def test_late_only_cannot_activate_ordinary_window_slot(scenario):
    now, _, journal, client = scenario
    run(journal, client, now, Feed(now))
    assert not core.window_entry_count(journal, START * 1000)
    assert policy.window_attempts(journal, START) == []


def test_no_rank_three_promotion_even_if_96c_eligible(scenario):
    now, _, journal, client = scenario
    feed = Feed(now, ["KXBTC15M", "KXSOL15M", "KXETH15M",
                      "KXHYPE15M", "KXXRP15M"])
    # BTC is excluded and second-ranked SOL has no qualifying 89c quote.
    client.initial["KXSOL15M"] = ".88"
    client.fresh["KXSOL15M"] = ".88"
    run(journal, client, now, feed)
    assert client.posted == []  # Rank-three ETH must not be promoted.


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("entry", [".89", ".90", ".95", ".97"])
def test_zone2_confirmed_fill_places_resting_98c_tp_and_preserves_stop(
        tmp_path, side, entry):
    from test_single_coin_minute9 import StopExchange, fake_record
    client = StopExchange(side=side, bid=str(D(entry) - D(".01")))
    yes_price = D(entry) if side == "YES" else D(1) - D(entry)
    client.rows[0]["yes_price_dollars"] = str(yes_price)
    client.rows[0]["no_price_dollars"] = str(D(1) - yes_price)
    journal = core.Journal(tmp_path / f"zone2-{side}-{entry}.json")
    record = fake_record(client, fully_sold=False)
    record["entry_kind"] = late.LATE_KIND
    record["entry_zone"] = core.ZONE2
    record["take_profit_target"] = ".98"
    journal.data["windows"][core.zone_record_key(client.ticker, core.ZONE2)] = record
    journal.save()
    core.reconcile_exit(client, journal, record, START + 725)
    assert len(client.submissions) == 1
    assert client.submissions[0]["limit"] == D(".98")
    assert client.submissions[0]["signed"] == (D(2) if side == "YES" else D(-2))
    assert record["sells"][0]["purpose"] == "take_profit"
    assert record["sells"][0]["execution"] == "gtc"
    assert client.orders["exit-1"]["status"] == "resting"
    # A restart must not create another resting sell for the same exposure.
    reloaded = core.Journal(journal.path)
    record = reloaded.data["windows"][core.zone_record_key(client.ticker, core.ZONE2)]
    core.reconcile_exit(client, reloaded, record, START + 727)
    assert len(client.submissions) == 1
    # The 21c stop still overrides and cancels the resting 98c order.
    client.bid = D(entry) - D(".21")
    core.reconcile_exit(client, reloaded, record, START + 729)
    assert record["stop_armed"]
    assert client.orders["exit-1"]["status"] == "canceled"
    assert len(client.submissions) == 1
    core.reconcile_exit(client, reloaded, record, START + 731)
    assert len(client.submissions) == 2
    assert record["sells"][-1]["purpose"] == "stop_loss"
    assert client.submissions[-1]["limit"] == D(entry) - D(".21")


def test_legacy_zone2_entry_at_98c_does_not_sell_below_cost(tmp_path):
    from test_single_coin_minute9 import StopExchange, fake_record
    client = StopExchange(bid=".90")
    client.rows[0]["yes_price_dollars"] = ".98"
    client.rows[0]["no_price_dollars"] = ".02"
    journal = core.Journal(tmp_path / "legacy-98.json")
    record = fake_record(client, fully_sold=False)
    record["entry_kind"] = late.LATE_KIND
    record["entry_zone"] = core.ZONE2
    journal.data["windows"][core.zone_record_key(client.ticker, core.ZONE2)] = record
    core.reconcile_exit(client, journal, record, START + 725)
    assert record["sells"] == []  # Do not sell 98c entry at an unprofitable 98c.
    client.bid = D(".77")
    core.reconcile_exit(client, journal, record, START + 727)
    assert record["sells"][-1]["purpose"] == "stop_loss"


def confirmed_zone1_zero_fill(ticker):
    return {
        "ticker": ticker, "series": ticker.split("-")[0],
        "entry_zone": core.ZONE1, "window_start_ms": START * 1000,
        "quantity": "2", "buy": {"client_id": "old-regular-client",
                                "order_id": "old-regular-order",
                                "limit": "0.84", "created_at": START + 600},
        "entry_zero_fill_verified": {
            "order_id": "old-regular-order", "client_id": "old-regular-client",
            "status": "canceled", "filled": "0", "remaining": "0"},
        "outcome": {"status": "no_fill", "counted": True,
                    "completed_at": START + 602},
        "sells": [],
    }


def test_zone1_verified_no_fill_does_not_block_zone2_eth_reentry(scenario):
    now, events, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    journal.data["windows"][ticker] = confirmed_zone1_zero_fill(ticker)
    journal.save()
    feed = Feed(now)
    run(journal, client, now, feed)
    key = core.zone_record_key(ticker, core.ZONE2)
    assert [x[0] for x in client.posted] == [ticker]
    assert journal.data["windows"][ticker]["entry_zone"] == core.ZONE1
    assert journal.data["windows"][key]["entry_zone"] == core.ZONE2
    assert journal.data["windows"][ticker]["outcome"]["status"] == "no_fill"
    assert journal.data["windows"][key]["buy"]["client_id"] != "old-regular-client"
    assert core.window_entry_count(journal, START * 1000) == 0
    assert late.zone2_attempt_exists(journal, ticker)
    assert any(n == "M9_LATE_BUY_SUBMIT" and d["entry_zone"] == core.ZONE2
               for n, d in events)


def test_zone2_restart_does_not_repeat_same_ticker_after_zone1_no_fill(scenario):
    now, _, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    journal.data["windows"][ticker] = confirmed_zone1_zero_fill(ticker)
    journal.save()
    feed = Feed(now)
    run(journal, client, now, feed)
    assert len(client.posted) == 1
    now[0] += 5
    run(core.Journal(journal.path), client, now, feed)
    assert len(client.posted) == 1


def test_zone1_ambiguous_order_blocks_zone2_even_without_exchange_inventory(scenario):
    now, events, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    record = confirmed_zone1_zero_fill(ticker)
    record.pop("entry_zero_fill_verified")
    record.pop("outcome")
    journal.data["windows"][ticker] = record
    journal.save()
    run(journal, client, now, Feed(now))
    assert all(not ticker.startswith("KXETH15M-") for ticker, _, _, _ in client.posted)
    # Rank-two SOL can still trade: the restriction belongs to ETH's Zone 1.
    assert client.posted and client.posted[0][0].startswith("KXSOL15M-")
    assert any(n == "M9_LATE_ZONE_BLOCKED" and d["reason"] == "zone1_order_unresolved"
               for n, d in events)


def test_zone1_no_fill_without_proof_cannot_enable_zone2(scenario):
    now, events, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    record = confirmed_zone1_zero_fill(ticker)
    record.pop("entry_zero_fill_verified")
    journal.data["windows"][ticker] = record
    run(journal, client, now, Feed(now))
    assert all(not ticker.startswith("KXETH15M-") for ticker, _, _, _ in client.posted)
    assert client.posted and client.posted[0][0].startswith("KXSOL15M-")
    assert any(n == "M9_LATE_ZONE_BLOCKED" and d["reason"] == "zone1_zero_fill_unverified"
               for n, d in events)


def test_zone1_completed_trade_allows_distinct_zone2_only_when_owned(scenario):
    now, events, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    regular = confirmed_zone1_zero_fill(ticker)
    regular["outcome"] = {"status": "win", "counted": True,
                          "completed_at": START + 650,
                          "bought": "2", "sold": "2",
                          "remaining_at_settlement": "0"}
    regular.pop("entry_zero_fill_verified")
    regular["sells"] = [{"order_id": "old-regular-sell", "client_id": "old-sell-id"}]
    journal.data["windows"][ticker] = regular
    client.fills_by_ticker[ticker] = [
        {"order_id": "old-regular-order", "count_fp": "2"},
        {"order_id": "old-regular-sell", "count_fp": "2"}]
    client.orders["old-regular-order"] = {
        "order_id": "old-regular-order", "ticker": ticker, "status": "executed"}
    client.orders["old-regular-sell"] = {
        "order_id": "old-regular-sell", "ticker": ticker, "status": "executed"}
    allowed, reason = late.zone2_entry_status(client, journal, ticker)
    assert allowed, reason
    client.fills_by_ticker[ticker].append({"order_id": "outside", "count_fp": "1"})
    allowed, reason = late.zone2_entry_status(client, journal, ticker)
    assert not allowed and reason == "outside_market_fills"


def test_zone1_complete_but_unclosed_inventory_blocks_zone2(scenario):
    now, _, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    regular = confirmed_zone1_zero_fill(ticker)
    regular["outcome"] = {"status": "win", "completed_at": START + 650,
                          "bought": "2", "sold": "1",
                          "remaining_at_settlement": "1"}
    journal.data["windows"][ticker] = regular
    assert late.zone2_entry_status(client, journal, ticker) == (
        False, "zone1_inventory_not_fully_closed")


def test_cash_guard_selects_zone2_by_client_order_id_not_zone1_ticker(scenario):
    now, _, journal, client = scenario
    ticker = client.market("KXETH15M-TEST")["ticker"]
    journal.data["windows"][ticker] = confirmed_zone1_zero_fill(ticker)
    feed = Feed(now)
    run(journal, client, now, feed)
    # Zone 1's saved intent must never be overwritten by the new Zone 2 entry.
    assert journal.data["windows"][ticker]["buy"]["client_id"] == "old-regular-client"
    assert len(client.posted) == 1


def test_preview_never_writes_or_sends_orders(scenario):
    now, events, journal, client = scenario
    initial = journal.path.read_bytes()
    run(journal, client, now, Feed(now), live=False)
    assert client.posted == []
    assert journal.path.read_bytes() == initial
    assert any(name == "M9_LATE_PREVIEW" for name, _ in events)
