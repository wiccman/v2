"""Offline exchange and integration tests; never connect to an account."""
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

import bot
from kalshi import KalshiAPIError
from take_profit import TakeProfitMonitor
from test_exit_orders import RecordingClient


class Exchange(RecordingClient):
    def __init__(self, quantity="2", bid="0.20", liquidity="100"):
        super().__init__()
        self.held, self.bid, self.liquidity = map(D, (quantity, bid, liquidity))
        self.remote = {}
        self.lose_ack = False
        self.failure = None
        self.read_failure = None
        self.position_read = threading.Event()
        self.posted = threading.Event()
        self.submissions = []

    def positions(self, ticker):
        self.position_read.set()
        if self.read_failure:
            raise self.read_failure
        return [{"ticker": ticker, "position_fp": str(self.held)}]

    def request(self, method, path, params=None, body=None, auth=False):
        assert method == "POST" and path == "/portfolio/events/orders"
        assert body["reduce_only"] is True
        assert body["time_in_force"] == "immediate_or_cancel"
        self.submissions.append(dict(body))
        if self.failure:
            raise self.failure
        expected_side = "ask" if self.held > 0 else "bid"
        target = D(body["price"]) if body["side"] == "ask" else 1 - D(body["price"])
        fill = min(abs(self.held), D(body["count"]), self.liquidity) if body["side"] == expected_side and self.bid >= target else D(0)
        self.held += -fill if self.held > 0 else fill
        oid = f"tp-{len(self.remote)}"
        self.remote[oid] = {"order_id": oid, "client_order_id": body["client_order_id"],
                            "status": "executed" if fill == D(body["count"]) else "canceled",
                            "fill_count_fp": str(fill)}
        self.posted.set()
        if self.lose_ack:
            raise TimeoutError("Acknowledgement lost after exchange execution")
        return {"order_id": oid, "fill_count": str(fill)}

    def order(self, order_id):
        return self.remote[order_id]

    def all_orders(self, ticker):
        return list(self.remote.values())

    def market(self, ticker):
        raise AssertionError("No quote/signal lookup is needed for exits")


def monitor(tmp_path, exchange, record=None):
    clock = [1000.0]
    entries = {"markets": {"T": record or {"close_timestamp": 1900}}}
    events = []
    svc = TakeProfitMonitor(exchange, lambda: entries, tmp_path / "tp.json",
                            clock=lambda: clock[0], emit=lambda event, **data: events.append((event, data)))
    return svc, entries, clock, events


@pytest.mark.parametrize("quantity,side,yes_price", [("2.5", "ask", "0.4500"), ("-2.5", "bid", "0.5500")])
def test_arms_and_submits_at_45_immediately_on_detected_fill_below_target(tmp_path, quantity, side, yes_price):
    exchange = Exchange(quantity=quantity, bid="0.20")
    svc, _, _, events = monitor(tmp_path, exchange)
    svc.run_once()
    body = exchange.submissions[0]
    assert body["side"] == side and body["price"] == yes_price
    assert body["count"] == "2.5"
    assert exchange.held == D(quantity)  # An unmarketable IOC cannot sell below 45.
    assert any(e == "TP_ARMED" for e, _ in events)
    assert not any(e == "TP_FILL" for e, _ in events)
    saved = json.loads(svc.path.read_text())["markets"]["T"]
    assert saved["pending"]["client_id"] == body["client_order_id"]


def test_no_exit_for_unfilled_entry_then_partial_fill_gets_exact_coverage(tmp_path):
    exchange = Exchange(quantity="0")
    svc, _, _, _ = monitor(tmp_path, exchange)
    svc.run_once()
    assert exchange.submissions == []
    exchange.held = D("0.37")
    svc.run_once()
    assert exchange.submissions[0]["count"] == "0.37"


@pytest.mark.parametrize("sign", [1, -1])
def test_partial_exit_then_restart_only_retries_remaining_inventory(tmp_path, sign):
    exchange = Exchange(quantity=str(sign * 2), bid="0.45", liquidity="0.75")
    svc, entries, clock, events = monitor(tmp_path, exchange)
    svc.run_once()
    assert exchange.held == D("1.25") * sign
    svc2 = TakeProfitMonitor(exchange, lambda: entries, svc.path, clock=lambda: clock[0],
                              emit=lambda event, **data: events.append((event, data)))
    exchange.liquidity = D(100)
    svc2.run_once()
    assert [b["count"] for b in exchange.submissions] == ["2", "1.25"]
    svc2.run_once()
    assert exchange.held == 0 and len(exchange.submissions) == 2
    assert sum(D(d["cumulative_quantity"]) for e, d in events if e == "TP_FILL") == D(2)


def test_lost_ack_is_recovered_before_any_new_exit(tmp_path):
    exchange = Exchange(bid="0.45")
    exchange.lose_ack = True
    svc, entries, clock, _ = monitor(tmp_path, exchange)
    svc.run_once()
    assert not svc.healthy and exchange.held == 0
    saved = json.loads(svc.path.read_text())["markets"]["T"]["pending"]
    assert "order_id" not in saved
    restart = TakeProfitMonitor(exchange, lambda: entries, svc.path, clock=lambda: clock[0], emit=lambda *a, **k: None)
    restart.run_once()
    assert restart.healthy and len(exchange.submissions) == 1


def test_unresolved_ack_does_not_blindly_resubmit_and_blocks_new_buys(tmp_path, monkeypatch):
    exchange = Exchange()
    exchange.failure = TimeoutError("Request outcome unknown")
    svc, entries, clock, _ = monitor(tmp_path, exchange)
    svc.run_once()
    exchange.failure = None
    svc.run_once()
    assert len(exchange.submissions) == 1 and not svc.healthy
    monkeypatch.setattr(bot, "EXIT_MONITOR", svc)
    monkeypatch.setattr(bot, "write_log", lambda *a, **k: None)
    result, quantity = bot.funded_entry(entries["markets"]["T"], entries, "T", "YES", D("0.25"),
                                        datetime.fromtimestamp(1900, timezone.utc), "regular", now_timestamp=1000)
    assert result == {} and quantity == 0


def test_failed_durable_save_never_submits(tmp_path, monkeypatch):
    exchange = Exchange()
    svc, _, _, _ = monitor(tmp_path, exchange)
    monkeypatch.setattr(svc, "save", lambda: (_ for _ in ()).throw(OSError("Disk full")))
    svc.run_once()
    assert exchange.submissions == [] and not svc.healthy


def test_recovery_id_is_on_disk_before_request(tmp_path):
    exchange = Exchange()
    svc, _, _, _ = monitor(tmp_path, exchange)
    request = exchange.request
    def persisted_first(method, path, params=None, body=None, auth=False):
        pending = json.loads(svc.path.read_text())["markets"]["T"]["pending"]
        assert pending["client_id"] == body["client_order_id"]
        assert pending["quantity"] == body["count"]
        return request(method, path, params, body, auth)
    exchange.request = persisted_first
    svc.run_once()
    assert len(exchange.submissions) == 1


def test_old_price_entry_cancellation_does_not_reset_budget(monkeypatch):
    from test_five_minute_exits import cycle_setup
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    record["entry_intents"] = [{"client_id": "old", "order_id": "old-price", "side": "YES",
        "price": "0.46", "quantity": "2", "reserved_dollars": "0.98", "kind": "regular", "entry_closed": False}]
    fake.resting = [{"order_id": "old-price"}]
    bot.reconcile_entries(state)
    assert fake.cancelled == ["old-price"]
    assert record["entry_intents"][0]["entry_closed"]
    assert record["entry_intents"][0]["reserved_dollars"] == "0.98"


def test_fixed_entry_validation_rejects_old_46_cent_price(monkeypatch):
    from test_five_minute_exits import cycle_setup
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    with pytest.raises(ValueError, match="fixed entry limit"):
        bot.funded_entry(record, state, "TEST", "YES", D("0.46"), closed, "regular")
    assert fake.entries == []


@pytest.mark.parametrize("price", ["0.38", "0.39"])
def test_retired_38_and_39_cent_tiers_cannot_open_new_inventory(monkeypatch, price):
    from test_five_minute_exits import cycle_setup
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 180)
    with pytest.raises(ValueError, match="fixed entry limit"):
        bot.funded_entry(record, state, "TEST", "YES", D(price), closed, "regular")
    assert fake.entries == []


@pytest.mark.parametrize("on_read", [False, True])
def test_rate_limit_respects_retry_after_for_reads_and_writes(tmp_path, on_read):
    exchange = Exchange()
    error = KalshiAPIError(429, "Rate limit", retry_after="30")
    setattr(exchange, "read_failure" if on_read else "failure", error)
    svc, _, clock, _ = monitor(tmp_path, exchange)
    svc.run_once()
    assert not svc.healthy
    exchange.read_failure = exchange.failure = None
    clock[0] += 29
    before = len(exchange.submissions)
    svc.run_once()
    assert len(exchange.submissions) == before and not svc.healthy
    clock[0] += 1
    svc.run_once()
    assert len(exchange.submissions) == before + 1 and svc.healthy


def test_legacy_resting_exit_must_be_confirmed_canceled_before_new_exit(tmp_path):
    exchange = Exchange()
    exchange.remote["legacy"] = {"order_id": "legacy", "status": "resting"}
    exchange.cancel = lambda *args: {"order_id": "legacy"}  # Incomplete acknowledgement.
    svc, _, _, _ = monitor(tmp_path, exchange, {"close_timestamp": 1900, "take_profit_order_id": "legacy"})
    svc.run_once()
    assert exchange.submissions == [] and not svc.healthy
    exchange.remote["legacy"]["status"] = "canceled"
    svc.run_once()
    assert len(exchange.submissions) == 1


def test_opposing_entry_or_manual_close_cannot_turn_exit_into_new_position(tmp_path):
    exchange = Exchange(bid="0.45")
    svc, _, _, _ = monitor(tmp_path, exchange)
    original = exchange.positions
    def racing_position(ticker):
        result = original(ticker)
        exchange.held = D("-1")  # Opposite entry fills after the REST snapshot.
        return result
    exchange.positions = racing_position
    svc.run_once()
    assert exchange.held == D("-1")  # Reduce-only ask must not increase NO exposure.
    exchange.positions = original
    svc.run_once()
    assert exchange.held == 0


def test_exits_continue_past_six_minutes_but_stop_at_market_close(tmp_path):
    exchange = Exchange(bid="0.44")
    svc, _, clock, _ = monitor(tmp_path, exchange)
    clock[0] = 1800  # Last 100 seconds; entry cancellation has already passed.
    svc.run_once()
    assert len(exchange.submissions) == 1
    clock[0] = 1900
    svc.run_once()
    assert len(exchange.submissions) == 1


def test_independent_thread_exits_while_entry_signal_call_is_blocked(tmp_path, monkeypatch):
    exchange = Exchange(bid="0.45")
    svc, entries, _, _ = monitor(tmp_path, exchange)
    entered = threading.Event()
    release = threading.Event()
    def blocked_discovery(now):
        entered.set()
        assert release.wait(2)
        raise RuntimeError("Signal lookup failed")
    monkeypatch.setattr(bot, "reconcile_entries", lambda state: None)
    monkeypatch.setattr(bot, "active_market", blocked_discovery)
    monkeypatch.setattr(bot, "EXIT_MONITOR", svc)
    errors = []
    def entry_cycle():
        try:
            bot.cycle(entries)
        except RuntimeError as error:
            errors.append(str(error))
    entry = threading.Thread(target=entry_cycle)
    entry.start()
    assert entered.wait(1)
    svc.start()
    try:
        assert exchange.posted.wait(1)
        assert exchange.held == 0 and entry.is_alive()
    finally:
        release.set()
        entry.join(timeout=2)
        svc.stop()
    assert errors == ["Signal lookup failed"]


def test_live_monitor_disables_old_overlapping_exit_paths(monkeypatch):
    from test_five_minute_exits import cycle_setup
    from types import SimpleNamespace
    fake, record, state, clock, closed = cycle_setup(monkeypatch, 360)
    monkeypatch.setattr(bot, "EXIT_MONITOR", SimpleNamespace(healthy=True))
    bot.cycle(state)
    assert fake.exits == []


def test_health_expires_when_exit_worker_stalls(tmp_path):
    svc, _, clock, _ = monitor(tmp_path, Exchange(quantity="0"))
    svc.run_once()
    assert svc.healthy
    clock[0] += 11
    assert not svc.healthy
