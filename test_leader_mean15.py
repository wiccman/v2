"""Offline Crypto Leader safety tests: no real Kalshi submissions."""
from datetime import datetime, timezone
from decimal import Decimal as D
import pytest

import leader_mean15 as leader
import performance_ranked_minute9 as ranked


START = 1793763000
TICKER = "KXCRYPTOLEAD15M-26NOV031500-ETH"
CLOSE = datetime.fromtimestamp(START + 900, timezone.utc).isoformat()


def market(coin="ETH", no=".72", status="open"):
    return {"ticker": TICKER.rsplit("-", 1)[0] + "-" + coin,
            "status": status, "no_ask_dollars": no, "no_bid_dollars": ".80",
            "close_time": CLOSE, "exchange_index": 2}


def observations(at):
    # Returns: BTC +4, ETH +2, SOL +0, XRP +1, HYPE +3.
    # Mean = +2, so ETH is uniquely closest.
    points = {"BTC": 4, "ETH": 2, "SOL": 0, "XRP": 1, "HYPE": 3}
    return [ranked.Observation(leader.COINS[k], START, D(100),
                               D(100 + v), at) for k,v in points.items()]


class Feed:
    def __init__(self, clock):
        self.clock = clock
    def snapshot(self, names, start):
        assert tuple(names) == leader.SOURCE_SERIES and start == START
        return observations(self.clock())


class Exchange:
    def __init__(self):
        self.book = market()
        self.submissions = []
        self.orders = {}
        self.fills = []
        self.held = D(0)
        self.lose_ack = False
        self.sell_partial = False
        self.cash = D("50")

    def markets(self, **params):
        assert params["series_ticker"] == "KXCRYPTOLEAD15M"
        return [dict(self.book)]
    def market(self, ticker):
        assert ticker == self.book["ticker"]
        return dict(self.book)
    def balance(self):
        return {"balance_dollars": str(self.cash), "balance_breakdown":
                [{"exchange_index": 2, "balance": str(self.cash)}]}
    def positions(self, ticker):
        return [{"ticker": ticker, "position_fp": str(self.held)}] if self.held else []
    def all_orders(self, ticker, status=None):
        return [o for o in self.orders.values() if o["ticker"] == ticker
                and (status is None or o["status"] == status)]
    def all_fills(self, ticker):
        return [f for f in self.fills if f["ticker"] == ticker]
    def order(self, order_id, ticker):
        row = self.orders[order_id]
        assert row["ticker"] == ticker
        return dict(row)
    def place_entry(self, ticker, side, quantity, limit, close, **kwargs):
        assert side == "NO" and kwargs.get("ioc") is True
        assert kwargs["client_order_id"]
        oid = "B" + str(len(self.submissions))
        self.submissions.append(("buy", ticker, quantity, limit, kwargs))
        if self.lose_ack:
            return {}
        self.orders[oid] = {"ticker":ticker,"order_id":oid,
            "client_order_id":kwargs["client_order_id"],
            "status":"executed", "fill_count_fp":str(quantity)}
        self.held -= D(quantity)
        self.fills.append({"ticker":ticker,"order_id":oid,"fill_id":"FB"+oid,
            "count_fp":str(quantity), "no_price_dollars":str(limit),
            "yes_price_dollars":str(D(1)-D(limit)), "fee_cost":"0.01",
            "book_side":"ask"})
        return {"order":{"order_id":oid}}
    def place_take_profit(self, ticker, signed_quantity, limit, **kwargs):
        assert D(signed_quantity) < 0
        oid = "S" + str(len(self.submissions))
        qty = -D(signed_quantity)
        filled = qty / 2 if self.sell_partial else qty
        self.submissions.append(("sell", ticker, qty, D(limit), kwargs))
        self.orders[oid] = {"ticker":ticker,"order_id":oid,
            "client_order_id":kwargs["client_order_id"],
            "status":"canceled" if self.sell_partial else "executed",
            "fill_count_fp":str(filled)}
        self.held += filled
        self.fills.append({"ticker":ticker,"order_id":oid,"fill_id":"FS"+oid,
            "count_fp":str(filled), "no_price_dollars":str(limit),
            "yes_price_dollars":str(D(1)-D(limit)), "fee_cost":"0.01",
            "book_side":"bid"})
        return {"order":{"order_id":oid}}
    def cancel(self, order_id, ticker):
        assert order_id in self.orders and self.orders[order_id]["ticker"] == ticker
        self.orders[order_id]["status"] = "canceled"


@pytest.mark.parametrize("elapsed,eligible", [
    (0, False), (60, False), (179.99, False), (180, True), (181, True),
    (839.99, True), (840, False), (899, False), (900, False)])
def test_entry_window_strict(elapsed, eligible):
    assert bool(leader.leader_plan([market()], "ETH", START, START+elapsed)) == eligible


@pytest.mark.parametrize("price,expected", [
    (".64", None), (".6499", None), (".65", ".65"),
    (".6501", ".66"), (".9751", ".98"), (".99", ".99"),
    (".9901", None), ("1.00", None)])
def test_no_ask_gate_and_rounding(price, expected):
    p = leader.leader_plan([market(no=price)], "ETH", START, START+185)
    assert (str(p["limit"]) if p else None) == expected


def test_exact_contract_only_and_no_sol():
    assert leader.leader_plan([market("SOL")], "SOL", START, START+200) is None
    assert leader.leader_plan([market("BTC")], "BTC", START, START+200)
    assert leader.leader_plan([market("ETH")], "BTC", START, START+200) is None
    assert leader.leader_plan([market("ETH"), market("ETH")], "ETH", START, START+200) is None
    assert leader.leader_plan([market("ETH", status="closed")], "ETH", START, START+200) is None


def test_5_coin_mean_selects_eth_and_does_not_allow_stale():
    coin, mean, returns = leader.coin_mean_choice(observations(START+181), START, START+181)
    assert coin == "ETH" and mean == D(2) and returns["SOL"] == D(0)
    with pytest.raises(ranked.PerformanceUnavailable):
        leader.coin_mean_choice(observations(START+181)[:-1], START, START+181)
    with pytest.raises(ranked.PerformanceUnavailable):
        leader.coin_mean_choice(observations(START+181), START, START+200)


def test_first_10_then_5_until_loss(tmp_path):
    from single_coin_minute9 import Journal
    j = Journal(tmp_path/"leader.json")
    assert leader.budget(j) == D(10)
    for status in ("win", "win", "flat"):
        j.data["windows"][str(len(j.data["windows"]))] = {
            "ticker":TICKER, "outcome":{"status":status, "completed_at":len(j.data["windows"])+1}}
        assert leader.budget(j) == D(5)
    j.data["windows"]["loss"] = {"ticker":TICKER,
        "outcome":{"status":"loss","completed_at":100}}
    assert leader.budget(j) == D(10)
    j.data["windows"]["pending"] = {"ticker":TICKER,"buy":{"client_id":"UNCERTAIN"}}
    assert leader.budget(j) is None


def test_actual_buy_exact_no_and_minute14_exit_without_stop(tmp_path):
    at = [START+183.]
    feed = Feed(lambda:at[0])
    svc = leader.LeaderRunner(tmp_path/"leader.json",provider=feed,clock=lambda:at[0])
    venue = Exchange()
    svc.run(venue,live=True)
    assert [s[0] for s in venue.submissions] == ["buy"]
    rec = svc.journal.data["windows"][str(START)]
    assert rec["coin"] == "ETH" and rec["side"] == "NO"
    assert rec["budget_dollars"] == "10" and rec["buy"]["order_id"]
    at[0] = START+400
    venue.book["no_bid_dollars"] = ".12"   # NO stop loss by request.
    svc.run(venue,live=True)
    assert [s[0] for s in venue.submissions] == ["buy"]
    at[0] = START+840
    venue.book["no_bid_dollars"] = ".80"
    svc.run(venue,live=True)
    assert [s[0] for s in venue.submissions] == ["buy", "sell"]
    assert venue.held == 0
    at[0] = START+842
    svc.run(venue,live=True)
    outcome=svc.journal.data["windows"][str(START)]["outcome"]
    assert outcome["status"] == "win"
    assert leader.budget(svc.journal) == D(5)
    assert len(venue.submissions) == 2
    assert venue.submissions[-1][-1]["client_order_id"]


def test_preview_mode_never_exits_existing_position(tmp_path):
    at = [START+183]
    svc = leader.LeaderRunner(tmp_path/"leader.json",provider=Feed(lambda:at[0]),
                              clock=lambda:at[0])
    venue = Exchange()
    svc.run(venue,live=True)
    at[0] = START+845
    svc.run(venue,live=False)
    assert len(venue.submissions) == 1


def test_unresolved_entry_prevents_repeat_and_unknown_sell(tmp_path):
    at=[START+180]
    svc=leader.LeaderRunner(tmp_path/"leader.json",provider=Feed(lambda:at[0]),
                            clock=lambda:at[0])
    venue=Exchange()
    venue.lose_ack=True
    svc.run(venue,live=True)
    at[0]=START+300
    svc.run(venue,live=True)
    at[0]=START+841
    svc.run(venue,live=True)
    assert [a[0] for a in venue.submissions]==["buy"]
    assert svc.journal.data["windows"][str(START)].get("outcome") is None


def test_insufficient_cash_does_not_create_order_intent(tmp_path):
    at=[START+180]
    svc=leader.LeaderRunner(tmp_path/"leader.json",provider=Feed(lambda:at[0]),
                            clock=lambda:at[0])
    venue=Exchange()
    venue.cash=D(".40")
    svc.run(venue,live=True)
    assert not venue.submissions and not svc.journal.data["windows"]


def test_partial_ioc_sell_retries_only_after_terminal_fill_proof(tmp_path):
    at=[START+180]
    svc=leader.LeaderRunner(tmp_path/"leader.json",provider=Feed(lambda:at[0]),
                            clock=lambda:at[0])
    venue=Exchange()
    svc.run(venue,live=True)
    venue.sell_partial=True
    at[0]=START+840
    svc.run(venue,live=True)
    assert len(venue.submissions)==2
    at[0]+=2
    svc.run(venue,live=True)
    assert len(venue.submissions)==3
    assert venue.submissions[2][2] < venue.submissions[1][2]
