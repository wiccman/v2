"""Exercise the real order adapter with offline transports, never credentials."""
from copy import deepcopy
from decimal import Decimal as D

import pytest

import kalshi
import leader_mean15 as leader
from request_coordinator import RequestDeferred
from test_leader_mean15 import Exchange, Feed, START, market


class AdapterExchange(Exchange):
    place_entry = kalshi.KalshiClient.place_entry

    def _order(self, ticker, book_side, quantity, yes_price, **kwargs):
        assert book_side == "ask" and kwargs["ioc"] is True
        return super().place_entry(ticker, "NO", quantity, 1 - yes_price,
                                  kwargs["expiration_time"], **kwargs)


@pytest.mark.parametrize("coin", leader.TRADE_COINS)
def test_real_adapter_accepts_all_five_leader_coins(coin, monkeypatch):
    monkeypatch.setattr(kalshi.time, "time", lambda: START + 245)
    venue = AdapterExchange()
    venue.book = market(coin)
    venue.place_entry(venue.book["ticker"], "NO", 4, D(".98"), START + 900,
                      submit_before=START + 300, client_order_id="owned", ioc=True)
    assert venue.submissions[0][1].endswith("-" + coin)
    assert venue.submissions[0][2:4] == (4, D(".98"))


def runner(tmp_path, now):
    return leader.LeaderRunner(tmp_path / "leader.json", provider=Feed(now), clock=now)


@pytest.mark.parametrize("failure,reason", [
    (kalshi.EntryNotDispatched("invalid_entry_arguments"), "invalid_entry_arguments"),
    (RequestDeferred(2), "shared_api_cooldown"),
])
def test_proven_local_failure_is_durable_no_fill(tmp_path, failure, reason):
    now = lambda: START + 245
    venue = Exchange()
    calls = []
    def reject(*args, **kwargs):
        calls.append(1)
        raise failure
    venue.place_entry = reject
    svc = runner(tmp_path, now)
    svc.run(venue, live=True)
    rec = svc.journal.data["windows"][str(START)]
    assert rec["outcome"]["status"] == "no_fill"
    assert rec["submission_proof"] == {"kind": "not_dispatched", "reason": reason}
    assert leader.budget(svc.journal) == D(5)
    restored = runner(tmp_path, now)
    restored.run(venue, live=True)
    assert len(calls) == 1 and not venue.submissions


def test_deadline_passed_inside_adapter_does_not_leave_uncertain_intent(tmp_path, monkeypatch):
    now = lambda: START + 299
    # The runner was eligible, but the deadline passed before adapter dispatch.
    monkeypatch.setattr(kalshi.time, "time", lambda: START + 300)
    venue = AdapterExchange()
    svc = runner(tmp_path, now)
    svc.run(venue, live=True)
    rec = svc.journal.data["windows"][str(START)]
    assert rec["submission_proof"]["reason"] == "entry_deadline_passed"
    assert rec["outcome"]["status"] == "no_fill" and not venue.submissions


@pytest.mark.parametrize("quantity,price,side", [
    (0, ".75", "NO"), (1, "NaN", "NO"), (1, "Infinity", "NO"),
    (1, ".75", "MAYBE"), (1.5, ".75", "NO"), (1, "1", "NO"),
])
def test_invalid_adapter_arguments_are_proven_not_sent(monkeypatch, quantity, price, side):
    monkeypatch.setattr(kalshi.time, "time", lambda: START + 245)
    venue = AdapterExchange()
    with pytest.raises(kalshi.EntryNotDispatched):
        venue.place_entry(venue.book["ticker"], side, quantity, price, START+900)
    assert not venue.submissions


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("bad response"),
                                        kalshi.KalshiAPIError(500, "unknown")])
def test_errors_after_possible_dispatch_stay_blocked(tmp_path, failure):
    now = lambda: START + 245
    venue = AdapterExchange()
    calls = []
    def possible_post(*args, **kwargs):
        calls.append(1)
        raise failure
    venue.place_entry = possible_post
    svc = runner(tmp_path, now)
    svc.run(venue, live=True)
    restored = runner(tmp_path, now)
    restored.run(venue, live=True)
    rec = restored.journal.data["windows"][str(START)]
    assert not rec.get("outcome") and not rec.get("submission_proof")
    assert leader.budget(restored.journal) is None and len(calls) == 1


def incident():
    return {"ticker": "KXCRYPTOLEAD15M-26NOV132345-SOL", "coin": "SOL", "side": "NO",
            "window_start_ms": 1794630600000, "close_at": 1794631500,
            "entry_signal_source": leader.PREVIOUS_SIGNAL_SOURCE,
            "quantity": "4", "budget_dollars": "5", "sells": [],
            "buy": {"client_id": "old-owned-id", "created_at": 1794630841.516,
                    "limit": "0.98"}}


def recovery_setup(tmp_path):
    now = lambda: 1794632400 + 60  # Minute one; no entry is eligible.
    svc = runner(tmp_path, now)
    record = incident()
    svc.journal.data["windows"]["1794630600"] = record
    svc.journal.data["windows"]["prior-loss"] = {
        "ticker": "KXCRYPTOLEAD15M-26NOV132330-BTC",
        "entry_signal_source": leader.PREVIOUS_SIGNAL_SOURCE,
        "outcome": {"status": "loss", "completed_at": 1794630000}}
    svc.journal.save()
    venue = Exchange()
    venue.book = {"ticker": record["ticker"], "status": "finalized", "result": "no"}
    return svc, venue, record, now


def test_known_pre_http_sol_veto_recovers_without_orders_or_streak_change(tmp_path):
    svc, venue, record, now = recovery_setup(tmp_path)
    svc.run(venue, live=True)
    assert record["outcome"]["status"] == "no_fill"
    assert record["submission_proof"]["reason"] == "audited_20261114_sol_local_guard"
    assert leader.budget(svc.journal) == D("2.50")
    before = deepcopy(record)
    restored = runner(tmp_path, now)
    restored.run(venue, live=True)
    assert restored.journal.data["windows"]["1794630600"] == before
    assert not venue.submissions


@pytest.mark.parametrize("conflict", ["order", "fill", "position", "not_final",
                                      "unavailable", "wrong_time", "other_window",
                                      "other_source", "missing_count", "preview"])
def test_legacy_recovery_requires_exact_incident_and_no_conflicting_evidence(tmp_path, conflict):
    svc, venue, rec, _ = recovery_setup(tmp_path)
    if conflict == "order":
        venue.orders["foreign"] = {"ticker": rec["ticker"], "client_order_id": "foreign"}
    elif conflict == "fill":
        venue.fills = [{"ticker": rec["ticker"], "order_id": "foreign"}]
    elif conflict == "position":
        venue.held = D(-1)
    elif conflict == "not_final":
        venue.book["status"] = "closed"
    elif conflict == "unavailable":
        def fail(ticker): raise TimeoutError()
        venue.all_fills = fail
    elif conflict == "wrong_time":
        rec["buy"]["created_at"] += 1
    elif conflict == "other_window":
        rec["window_start_ms"] += 900000
    elif conflict == "other_source":
        rec["entry_signal_source"] = leader.SIGNAL_SOURCE
    elif conflict == "missing_count":
        venue.positions = lambda ticker: [{"ticker": ticker}]
    svc.run(venue, live=conflict != "preview")
    assert not rec.get("outcome") and not rec.get("submission_proof")
    assert leader.budget(svc.journal) is None and not venue.submissions


def test_real_order_identity_wins_over_legacy_incident_recovery(tmp_path):
    svc, venue, rec, _ = recovery_setup(tmp_path)
    venue.orders["real"] = {"ticker": rec["ticker"], "order_id": "real",
                            "client_order_id": rec["buy"]["client_id"],
                            "status": "executed", "fill_count_fp": "4"}
    # The receipt exists, but its fills are not visible yet: keep waiting.
    svc.run(venue, live=True)
    assert rec["buy"]["order_id"] == "real"
    assert not rec.get("outcome") and not rec.get("submission_proof")
    assert not venue.submissions
