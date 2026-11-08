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
    assert journal.data["windows"][ticker]["entry_kind"] == late.LATE_KIND
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
    (".95", False), (".9599", False), (".96", True),
    (".975", True), (".99", True), (".999", False), ("1.00", False),
])
def test_late_price_band(price, allowed):
    from test_single_coin_minute9 import market
    m = market(yes=price, no=".03", series="KXETH15M")
    result = late.late_plan(m, START, START + 740, "KXETH15M", "YES")
    assert bool(result) == allowed
    if allowed:
        assert D(result["limit"]) >= D(".96") and D(result["limit"]) <= D(".99")


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
    # BTC is excluded and second-ranked SOL has no qualifying 96c quote.
    client.initial["KXSOL15M"] = ".93"
    client.fresh["KXSOL15M"] = ".93"
    run(journal, client, now, feed)
    assert client.posted == []  # Rank-three ETH must not be promoted.


def test_high_price_fill_has_no_impossible_5cent_tp_but_keeps_stop(scenario):
    now, _, journal, _ = scenario
    class FilledLate(CashExchange):
        def market(self, ticker):
            data = super().market(ticker)
            data["yes_bid_dollars"] = ".96"
            return data
    client = FilledLate(outcomes={"KXETH15M": "partial"})
    client.initial = {x: ".97" for x in core.DEFAULT_SERIES}
    client.fresh = dict(client.initial)
    run(journal, client, now, Feed(now))
    record = next(iter(journal.data["windows"].values()))
    assert record["entry_kind"] == late.LATE_KIND
    assert not record["sells"]  # A 102c take-profit is impossible.
    assert core.exit_decision(D(".97"), D(".76")) == ("stop_loss", D(".76"))
    assert core.exit_decision(D(".97"), D(".96")) == (None, None)


def test_preview_never_writes_or_sends_orders(scenario):
    now, events, journal, client = scenario
    initial = journal.path.read_bytes()
    run(journal, client, now, Feed(now), live=False)
    assert client.posted == []
    assert journal.path.read_bytes() == initial
    assert any(name == "M9_LATE_PREVIEW" for name, _ in events)
