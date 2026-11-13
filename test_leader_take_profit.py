"""Offline-only checks for independently paused entries and owned Leader exits."""
from decimal import Decimal as D
from unittest.mock import Mock

import pytest

import leader_mean15 as leader
import minute8_top2 as router
from kalshi import KalshiClient
from test_leader_mean15 import START, TICKER, Feed, Exchange


class RestingExchange(Exchange):
    unknown_ack = False
    accepted_without_ack = False

    def place_resting_take_profit(self, ticker, signed_quantity, limit, **kwargs):
        assert signed_quantity < 0 and kwargs["reduce_only"] is True
        assert kwargs["expiration_time"] == START + 900
        oid = "TP" + str(len(self.submissions))
        self.submissions.append(("sell", ticker, -signed_quantity, limit, kwargs))
        if self.unknown_ack:
            raise TimeoutError("unknown submission")
        self.orders[oid] = {"ticker": ticker, "order_id": oid,
                            "client_order_id": kwargs["client_order_id"],
                            "status": "resting", "fill_count_fp": "0"}
        return {} if self.accepted_without_ack else {"order": {"order_id": oid}}


def setup_position(tmp_path):
    at = [START + 183]
    venue = RestingExchange()
    svc = leader.LeaderRunner(tmp_path / "leader.json", provider=Feed(lambda: at[0]),
                              clock=lambda: at[0])
    svc.run(venue, live=True)
    at[0] += 10
    return svc, venue, at


def exit_pass(svc, venue, *, live=True):
    svc.run(venue, live=live, entries_enabled=False, take_profit_spread=D(".07"))


def test_72_cent_fill_posts_79_cent_owned_reduce_only_limit_once(tmp_path):
    svc, venue, at = setup_position(tmp_path)
    venue.book["no_bid_dollars"] = ".10"  # Resting TP does not require a high bid.
    exit_pass(svc, venue)
    assert [s[0] for s in venue.submissions] == ["buy", "sell"]
    assert venue.submissions[-1][3] == D(".79")
    assert venue.submissions[-1][2] == -venue.held
    exit_pass(svc, venue)
    restarted = leader.LeaderRunner(svc.path, provider=Feed(lambda: at[0]),
                                    clock=lambda: at[0])
    exit_pass(restarted, venue)
    assert len(venue.submissions) == 2


def test_target_uses_actual_weighted_fills_and_rounds_up(tmp_path):
    svc, venue, _ = setup_position(tmp_path)
    fill = venue.fills.pop()
    half = D(fill["count_fp"]) / 2
    for i, price in enumerate((D(".70"), D(".71"))):
        venue.fills.append(dict(fill, fill_id=f"split-{i}", count_fp=str(half),
                                no_price_dollars=str(price),
                                yes_price_dollars=str(1-price)))
    exit_pass(svc, venue)
    assert venue.submissions[-1][3] == D(".78")  # .705 + .07 => .78


def test_preview_never_posts_tp(tmp_path):
    svc, venue, _ = setup_position(tmp_path)
    exit_pass(svc, venue, live=False)
    assert len(venue.submissions) == 1


@pytest.mark.parametrize("recoverable", [False, True])
def test_lost_tp_ack_never_reposts(tmp_path, recoverable):
    svc, venue, at = setup_position(tmp_path)
    venue.unknown_ack = not recoverable
    venue.accepted_without_ack = recoverable
    exit_pass(svc, venue)
    restarted = leader.LeaderRunner(svc.path, clock=lambda: at[0])
    exit_pass(restarted, venue)
    exit_pass(restarted, venue)
    assert len(venue.submissions) == 2
    rec = restarted.journal.data["windows"][str(START)]
    assert bool(rec["sells"][0].get("order_id")) == recoverable


@pytest.mark.parametrize("reason", ["foreign_fill", "wrong_position", "other_order", "closed"])
def test_no_exit_with_unsafe_ownership_or_closed_market(tmp_path, reason):
    svc, venue, _ = setup_position(tmp_path)
    if reason == "foreign_fill":
        venue.fills.append(dict(venue.fills[0], order_id="foreign", fill_id="other"))
    elif reason == "wrong_position":
        venue.held -= 1
    elif reason == "other_order":
        venue.orders["other"] = {"ticker": TICKER, "order_id": "other", "status": "resting"}
    else:
        venue.book["status"] = "closed"
    exit_pass(svc, venue)
    assert len(venue.submissions) == 1


def test_does_not_replace_canceled_tp(tmp_path):
    svc, venue, _ = setup_position(tmp_path)
    exit_pass(svc, venue)
    intent = svc.journal.data["windows"][str(START)]["sells"][0]
    venue.orders[intent["order_id"]]["status"] = "canceled"
    exit_pass(svc, venue)
    assert len(venue.submissions) == 2


def test_partial_resting_sell_never_creates_another_and_final_fill_reconciles(tmp_path):
    svc, venue, _ = setup_position(tmp_path)
    exit_pass(svc, venue)
    rec = svc.journal.data["windows"][str(START)]
    intent = rec["sells"][0]
    oid = intent["order_id"]
    qty = D(intent["quantity"])
    for i in range(2):
        venue.fills.append({"ticker": TICKER, "order_id": oid, "fill_id": f"sell-{i}",
                            "count_fp": str(qty / 2), "no_price_dollars": ".79",
                            "yes_price_dollars": ".21", "fee_cost": ".01", "book_side": "bid"})
        venue.held += qty / 2
        venue.orders[oid]["fill_count_fp"] = str(qty / 2 * (i+1))
        venue.orders[oid]["status"] = "resting" if i == 0 else "executed"
        exit_pass(svc, venue)
        assert len(venue.submissions) == 2
    assert rec["outcome"]["status"] == "win"
    assert D(rec["outcome"]["sold"]) == qty


def test_unachievable_target_not_capped_below_requested_profit(tmp_path):
    svc, venue, _ = setup_position(tmp_path)
    venue.fills[0].update(no_price_dollars=".95", yes_price_dollars=".05")
    exit_pass(svc, venue)
    assert len(venue.submissions) == 1


def test_pause_blocks_buys_without_any_position(tmp_path):
    svc = leader.LeaderRunner(tmp_path / "leader.json", clock=lambda: START + 200,
                              provider=Mock(side_effect=AssertionError("no entry feed")))
    venue = RestingExchange()
    exit_pass(svc, venue)
    assert venue.submissions == []


def test_router_runs_exit_management_with_buying_disabled(monkeypatch):
    monkeypatch.setenv("LEADER_TRADING_ENABLED", "false")
    monkeypatch.setenv("LEADER_EXITS_ENABLED", "true")
    monkeypatch.setenv("LEADER_TAKE_PROFIT_CENTS", "7")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    runner = Mock()
    monkeypatch.setattr(leader, "LeaderRunner", Mock(return_value=runner))
    monkeypatch.setattr(router, "run_pass", Mock())
    with router.installed_policy(provider=Mock()):
        router.core.run_pass("client", "journal", (), 10, 100, True, START + 200)
    runner.run.assert_called_once_with("client", live=True, entries_enabled=False,
                                       take_profit_spread=D(".07"))


def test_api_tp_is_reduce_only_gtc_and_correct_no_price():
    client = object.__new__(KalshiClient)
    client._order = Mock(return_value={})
    client.place_resting_take_profit(TICKER, D(-5), D(".79"),
                                    expiration_time=START+900, client_order_id="saved-id",
                                    reduce_only=True)
    client._order.assert_called_once_with(TICKER, "bid", D(5), D(".21"),
                                          expiration_time=START+900,
                                          client_order_id="saved-id", reduce_only=True)
