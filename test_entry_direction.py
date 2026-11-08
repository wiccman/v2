"""Offline regressions for the observed XRP counter-direction entry."""
from decimal import Decimal as D
import pytest
import minute8_top2 as bot
import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from test_cash_checked_minute9 import CashExchange
from test_single_coin_minute9 import START, market


class DirectionExchange(CashExchange):
    def __init__(self, sides=None):
        super().__init__()
        self.sides = sides or {"KXXRP15M": "NO"}

    def quote(self, series):
        no = self.sides.get(series, "YES") == "NO"
        return market(series=series, yes=".21" if no else ".80",
                      no=".80" if no else ".21")

    def markets(self, *, series_ticker, **kwargs):
        return [self.quote(series_ticker)]

    def market(self, ticker):
        value = self.quote(ticker.split("-")[0])
        value.update(exchange_index=2, yes_bid_dollars=".20", no_bid_dollars=".79")
        return value


class DirectionFeed:
    def __init__(self, now):
        self.now = now
        self.prices = [("KXXRP15M", D("1.1549"), D("1.1571")),
                       ("KXETH15M", D(100), D("100.04")),
                       ("KXHYPE15M", D(100), D("100.03")),
                       ("KXSOL15M", D(100), D("100.02")),
                       ("KXBTC15M", D(100), D("100.01"))]

    def snapshot(self, names, start):
        return [ranked.Observation(s, start, opening, price, self.now[0])
                for s, opening, price in self.prices]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    now, events = [START+540.0], []
    monkeypatch.setattr(core.time, "time", lambda: now[0])
    monkeypatch.setattr(core, "event", lambda name, **data: events.append((name, data)))
    feed = DirectionFeed(now)
    journal = core.Journal(tmp_path / "direction.json")
    with bot.installed_policy(feed):
        yield journal, now, events, feed


def run(client, journal, now, feed):
    bot.run_pass(client, journal, core.DEFAULT_SERIES, D("6.66"), 100,
                 True, now[0], provider=feed, clock=lambda: now[0])


def test_xrp_loss_setup_is_blocked_before_post_and_survives_restart(scenario):
    j, now, events, feed = scenario
    c = DirectionExchange()
    run(c, j, now, feed)
    assert not c.posted
    r = next(iter(j.data["windows"].values()))
    assert r["side"] == "NO" and r["buy"]["limit"] == "0.8"
    assert r["outcome"]["reason"] == "entry_direction_conflict"
    assert D(r["entry_direction_check"]["return_pct"]) > 0
    assert core.Journal(j.path).data == j.data
    assert core.window_entry_count(j, START*1000) == 0
    assert j.data["streak"] == core.starting_streak()
    assert any(n == "M9_ENTRY_NOT_SENT" for n, _ in events)


def test_same_setup_without_guard_would_submit_no(scenario, monkeypatch):
    j, now, _, feed = scenario
    monkeypatch.setattr(bot.DirectionCheckedQueue, "entry_direction_check", None)
    c = DirectionExchange()
    run(c, j, now, feed)
    assert len(c.posted) == 1 and c.posted[0][1] == "NO"


def test_backup_waits_sixty_seconds_and_pending_fill_blocks_more(scenario):
    j, now, _, feed = scenario
    c = DirectionExchange()
    run(c, j, now, feed)
    now[0] += 59.999
    run(c, core.Journal(j.path), now, feed)
    assert not c.posted
    now[0] += .001
    j = core.Journal(j.path)
    run(c, j, now, feed)
    assert len(c.posted) == 1 and c.posted[0][0].startswith("KXETH15M-")
    assert c.posted[0][1:] == ("YES", 2, D(".80"))
    now[0] += 60
    run(c, j, now, feed)
    assert len(c.posted) == 1 and core.window_entry_count(j, START*1000) == 1


def test_both_top_two_conflict_never_promotes_third(scenario):
    j, now, _, feed = scenario
    c = DirectionExchange({"KXXRP15M": "NO", "KXETH15M": "NO"})
    for _ in range(3):
        run(c, j, now, feed)
        now[0] += 60
    assert not c.posted and len(j.data["windows"]) == 2
    assert all(not t.startswith("KXHYPE15M") for t in j.data["windows"])


@pytest.mark.parametrize("move,side,allowed", [
    ("1", "YES", True), ("1", "NO", False),
    ("-1", "NO", True), ("-1", "YES", False),
    ("0", "YES", True), ("0", "NO", True)])
def test_symmetric_veto_preserves_neutral_behavior(move, side, allowed):
    proxy = bot.DirectionCheckedQueue(object(), clock=lambda: START+540, emit=lambda *a, **k: None)
    proxy.start = START
    proxy.observations = {"KXXRP15M": ranked.Observation("KXXRP15M", START, D(100), D(100)+D(move), START+540)}
    assert proxy.entry_direction_check("KXXRP15M-example", side, START*1000)["allowed"] is allowed


def test_stale_after_cash_read_never_posts(scenario):
    j, now, _, feed = scenario
    c = DirectionExchange({"KXXRP15M": "YES"})
    balance = c.balance
    def slow_balance():
        now[0] += 16
        return balance()
    c.balance = slow_balance
    run(c, j, now, feed)
    assert not c.posted
    assert next(iter(j.data["windows"].values()))["outcome"]["reason"] == "entry_direction_unavailable"


def test_btc_rank_one_is_excluded_without_promoting_rank_three(scenario):
    j, now, _, feed = scenario
    feed.prices[4] = ("KXBTC15M", D(100), D(102))
    c = DirectionExchange()
    run(c, j, now, feed)
    now[0] += 60
    run(c, j, now, feed)
    assert not c.posted
    assert all(t.startswith("KXXRP15M-") for t in j.data["windows"])


def test_wrong_window_or_missing_coin_fails_closed():
    proxy = bot.DirectionCheckedQueue(object(), clock=lambda: START+540, emit=lambda *a, **k: None)
    proxy.start, proxy.observations = START, {}
    assert not proxy.entry_direction_check("KXXRP15M-example", "NO", START*1000)["allowed"]
    proxy.observations["KXXRP15M"] = ranked.Observation("KXXRP15M", START-900, D(100), D(99), START+540)
    assert not proxy.entry_direction_check("KXXRP15M-example", "NO", START*1000)["allowed"]
