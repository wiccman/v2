"""Offline regression coverage: no credentials or live exchange requests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import bot
import kalshi
from kalshi import KalshiClient
from test_exit_orders import ExitClient, RecordingClient


class CycleClient(KalshiClient):
    def __init__(self):
        self.entries = []
        self.exits = []
        self.cancelled = []
        self.resting = []
        self.held = D("16")

    def market(self, ticker):
        return {"ticker": ticker, "floor_strike": "100000",
                "yes_ask_dollars": "0.46", "yes_bid_dollars": "0.39",
                "no_ask_dollars": "0.66", "no_bid_dollars": "0.65"}

    def market_cash(self, ticker):
        return {"exchange_index": 2, "cash_dollars": "100"}

    def markets(self, **kwargs):
        opened = datetime.fromtimestamp(1000000000, timezone.utc)
        return [dict(ticker=f"PAST-{offset}", status="finalized",
                     open_time=(opened - timedelta(minutes=15 * (offset + 1))).isoformat(),
                     close_time=(opened - timedelta(minutes=15 * offset)).isoformat(),
                     floor_strike="100000", expiration_value=str(value))
                for offset, value in zip((4, 3, 2, 1), (99980, 99985, 99990, 100010) if getattr(self, "bias_side", "YES") == "YES"
                                        else (100020, 100015, 100010, 99990))]

    def order(self, order_id, ticker=None):
        return {"order_id": order_id, "status": "resting"}

    def _order(self, ticker, side, quantity, price, **kwargs):
        collection = self.exits if kwargs.get("reduce_only") else self.entries
        collection.append((side, quantity, price, kwargs))
        return {"order_id": "order-" + str(len(self.entries) + len(self.exits))}

    def orders(self, ticker, status):
        return list(self.resting)

    def cancel(self, order_id, ticker):
        self.cancelled.append(order_id)
        self.resting = [o for o in self.resting if o["order_id"] != order_id]
        return {"order_id": order_id, "reduced_by": "1.00"}

    def positions(self, ticker):
        return [{"ticker": ticker, "position_fp": str(self.held)}]

    def fills(self, ticker):
        return [{"order_id": oid, "outcome_side": "YES", "count_fp": "8",
                 "yes_price_dollars": "0.32"} for oid in ("regular", "historical")]

    def btc_reference_price(self):
        return D("100010")


def cycle_setup(monkeypatch, elapsed):
    clock = [1000000000 + elapsed]
    started = datetime.fromtimestamp(1000000000, timezone.utc)
    closed = datetime.fromtimestamp(1000000900, timezone.utc)
    fake = CycleClient()
    record = {"entry_intents": [], "entry_budget_legacy": False, "entry_cancel_at": 1000000360, "buys": 0, "last_buy": 0, "orders": [],
              "signal": {"build": bot.SIGNAL_BUILD, "prediction": "YES", "base_confidence": "HIGH"},
              "predictions": [{"ask": "0.70"}] * 3,
              "historical_strikes": ["100000"],
              "historical_last_spot": "100050",
              "historical_strike_orders": [{"order_id": "historical", "side": "YES", "entry_closed": True}]}
    state = {"markets": {"TEST": record}}
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "EXIT_MONITOR", SimpleNamespace(healthy=True, wake=lambda: None,
                        settlement_ready=lambda ticker, side: False))
    # Preserve explicit legacy timing scenarios for migration regression tests.
    monkeypatch.setattr(bot, "START", 120)
    monkeypatch.setattr(bot, "END", 300)
    monkeypatch.setattr(bot, "CANCEL_AFTER", 360)
    monkeypatch.setattr(bot.time, "time", lambda: clock[0])
    monkeypatch.setattr(bot, "datetime", SimpleNamespace(now=lambda tz: datetime.fromtimestamp(clock[0], tz),
                                                       fromisoformat=datetime.fromisoformat))
    monkeypatch.setattr(bot, "active_market", lambda now: (fake.market("TEST"), started, closed))
    monkeypatch.setattr(bot, "save_state", lambda s: None)
    monkeypatch.setattr(bot, "write_log", lambda *a, **k: None)
    return fake, record, state, clock, closed


@pytest.mark.parametrize("elapsed", [300, 301, 360, 720, 899])
def test_all_new_buys_stop_at_five_minutes_and_exit_worker_is_sole_owner(monkeypatch, elapsed):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, elapsed)
    bot.cycle(state)
    assert fake.entries == []  # At 12:00 only an exact-97c settlement entry can run.
    assert fake.exits == []  # Independent worker owns exits; no single-target fallback.
    assert all(x[3].get("ioc") and x[3].get("reduce_only") for x in fake.exits)


def test_last_second_entries_expire_at_absolute_six_minute_cutoff(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 299)
    bot.cycle(state)
    assert len(fake.entries) == 2  # The $4 earlier allowance limits the batch.
    assert [q for _, q, _, _ in fake.entries] == [D(5), D(2)]
    assert sum(D(i["reserved_dollars"]) for i in record["entry_intents"]) <= D("4")
    assert all(x[3]["expiration_time"] == 1000000360 for x in fake.entries)


def test_restart_cancels_legacy_entries_even_without_saved_signal(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    record.update(orders=["regular-old", "spot-old"], dual_limit_orders=["dual-old"],
                  dual_limit_cancel_at=1000000500, signal=None,
                  historical_strike_orders=[{"order_id": "hist-old", "cancel_at": 1000000800}])
    fake.resting = [{"order_id": oid} for oid in ["regular-old", "spot-old", "dual-old", "hist-old"]]
    monkeypatch.setattr(fake, "markets", lambda **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    bot.cycle(state)
    assert set(fake.cancelled) == {"regular-old", "spot-old", "dual-old", "hist-old"}
    assert fake.entries == []


def test_slow_request_cannot_submit_an_entry_after_cutoff(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 299)
    original = fake.markets
    def slow_history(**kwargs):
        clock[0] = 1000000301
        return original(**kwargs)
    monkeypatch.setattr(fake, "markets", slow_history)
    bot.cycle(state)
    assert not fake.entries  # The legacy five-minute route deadline passed during the call.
    assert not any(i["kind"] == "historical" for i in record["entry_intents"])


def test_entry_wire_rejects_expired_order_locally(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(kalshi.time, "time", lambda: 300)
    assert fake.place_entry("T", "YES", D("1"), D("0.32"), 300) == {}
    assert fake.calls == []


def test_partial_ioc_retries_only_remaining_holdings_and_never_below_target(monkeypatch):
    fake = ExitClient()
    monkeypatch.setattr(bot, "client", fake)
    monkeypatch.setattr(bot, "write_log", lambda *a, **k: None)
    record = {}
    market = {"yes_ask_dollars": "0.46", "yes_bid_dollars": "0.39"}
    closed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    bot.manage_exit(record, "T", market, {}, closed)
    fake.held = D("0.75")
    market["yes_bid_dollars"] = "0.30"
    bot.manage_exit(record, "T", market, {}, closed)
    assert len(fake.actions) == 1
    market["yes_bid_dollars"] = "0.39"
    bot.manage_exit(record, "T", market, {}, closed)
    assert fake.actions[-1][2:4] == (D("0.75"), D("0.39"))
    fake.held = D("0")
    bot.manage_exit(record, "T", market, {}, closed)
    assert len(fake.actions) == 2


def test_old_rejection_backoff_does_not_block_fixed_exit(monkeypatch):
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 300)
    record.update(take_profit_rejected_side="YES", take_profit_rejected_quantity="8",
                  take_profit_rejected_target="0.29", take_profit_retry_after=clock[0] + 60)
    bot.cycle(state)
    assert fake.exits == []  # Retired backoff cannot trigger overlapping exits.
