"""Offline lifecycle checks for immediate-on-detection standing limit exits."""
import copy
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from kalshi import KalshiClient, KalshiAPIError
from manual_exit_runner import ManualExitService, Settings
from manual_limit_exits import ManualExitBlocked
from resting_manual_exits import RestingExitOnlyClient
from test_manual_limit_exits import Exchange, T, auto_buy


class RestingExchange(Exchange):
    place_manual_limit = RestingExitOnlyClient.place_manual_limit
    cancel_manual_exit = RestingExitOnlyClient.cancel_manual_exit
    _write = RestingExitOnlyClient._write

    def __init__(self):
        super().__init__()
        self.dry_run, self._approved_write = False, None
        self.hide_post, self.lose_cancel_ack, self.delay_cancel = False, False, False

    def request(self, method, path, params=None, body=None, auth=False):
        if method == "DELETE":
            if self.delay_cancel:
                self.cancelled.append(path.rsplit("/", 1)[-1])
                return {}  # No terminal proof: remote still appears resting.
            result = super().request(method, path, params=params, body=body, auth=auth)
            if self.lose_cancel_ack:
                raise TimeoutError("Cancellation response lost")
            return result
        if method != "POST":
            return super().request(method, path, params=params, body=body, auth=auth)
        assert path == "/portfolio/events/orders"
        assert body["time_in_force"] == "good_till_canceled" and body["reduce_only"] is False
        assert body["expiration_time"] == int(self.close)
        assert body["subaccount"] == 0 and body["cancel_order_on_pause"] is True
        assert body["post_only"] is False  # A target already crossed may execute.
        if self.fail_post:
            raise self.fail_post
        self.sent.append(copy.deepcopy(body))
        sign = 1 if body["side"] == "ask" else -1
        target = D(body["price"]) if sign == 1 else 1-D(body["price"])
        bid = self.bid if sign == 1 else self.no_bid
        oid = self.make_order(body["ticker"], sign, body["count"], target,
                              client_id=body["client_order_id"], action="sell")
        filled = min(D(body["count"]), self.liquidity) if bid >= target else D(0)
        if filled:
            self.fill(oid, filled, bid)
        if self.hide_post:
            self.hidden_orders.add(oid)
        if self.lose_ack:
            raise TimeoutError("Placement response lost")
        return {"order_id": oid, "client_order_id": body["client_order_id"],
                "fill_count": str(filled), "remaining_count": str(D(body["count"])-filled)}


def setup(tmp_path, *, dry_run=False):
    e = RestingExchange()
    path = tmp_path / "state.json"
    path.write_text('{"markets": {}}')
    path.with_name("state_take_profit.json").write_text('{"markets": {}}')
    config = replace(Settings.read({"STATE_PATH": str(path)}), dry_run=dry_run)
    events = []
    service = ManualExitService(e, config, clock=e.clock,
        emit=lambda event, **data: events.append((event, data)))
    return e, service, config, events


def exit_ids(e):
    return [oid for oid, row in e.remote.items() if row["client_order_id"].startswith("manual5c-")]


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("qty", ["1", "10", "2.75"])
def test_posts_before_target_price_and_exchange_can_fill_while_worker_stopped(tmp_path, sign, qty):
    e, s, _, _ = setup(tmp_path)
    e.buy(qty, ".42", sign)
    assert s.run_once() and len(e.sent) == 1
    sell = e.sent[0]
    assert sell["count"] == qty and sell["price"] == ("0.4700" if sign == 1 else "0.5300")
    oid = exit_ids(e)[0]
    assert e.remote[oid]["status"] == "resting" and e.held[T] == sign * D(qty)
    assert s.run_once() and len(e.sent) == 1  # Reserved, not submitted twice.
    e.fill(oid, qty, ".47")  # No worker call needed for an exchange fill.
    assert e.held[T] == 0
    assert s.run_once() and len(e.sent) == 1


def test_multiple_partial_fills_each_get_their_own_sell_in_the_same_pass(tmp_path):
    e, s, _, _ = setup(tmp_path)
    oid = e.make_order(count=10, price=".42")
    e.fill(oid, 4, ".40")
    e.fill(oid, 2, ".41")
    assert s.run_once()
    assert [(o["count"], o["price"]) for o in e.sent] == [("4", "0.4500"), ("2", "0.4600")]
    e.fill(oid, 4, ".42")
    assert s.run_once() and len(e.sent) == 3
    assert e.sent[-1]["price"] == "0.4700" and e.sent[-1]["count"] == "4"


def test_partial_exit_keeps_remaining_reservation_without_reposting(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    e.fill(exit_ids(e)[0], 3, ".47")
    assert s.run_once() and len(e.sent) == 1 and e.held[T] == 7
    assert not e.cancelled


def test_marketable_order_can_partially_fill_and_leave_remainder_resting(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.bid, e.liquidity = D(".50"), D(3)
    assert s.run_once() and len(e.sent) == 1
    assert e.held[T] == 7 and e.remote[exit_ids(e)[0]]["remaining_count_fp"] == "7"


@pytest.mark.parametrize("reduce", [3, 10, 15])
def test_manual_reduction_cancels_old_reservation_before_replacing_only_remaining_size(tmp_path, reduce):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    old_exit = exit_ids(e)[0]
    manual = e.make_order(count=reduce, price=".40", action="sell")
    e.fill(manual, reduce)
    assert s.run_once()
    assert e.cancelled == [old_exit] and manual not in e.cancelled
    assert len(e.sent) == (2 if reduce < 10 else 1)
    if reduce < 10:
        assert e.sent[-1]["count"] == "7" and e.sent[-1]["price"] == "0.4700"


def test_existing_user_sell_pauses_and_cancels_only_bot_owned_exit(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    user_sell = e.make_order(count=10, price=".50", action="sell")
    assert not s.run_once()
    assert e.cancelled == exit_ids(e) and user_sell not in e.cancelled
    assert len(e.sent) == 1


def test_user_cancel_is_respected_but_later_buy_fill_gets_its_own_order(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    e.cancel(exit_ids(e)[0], T)
    assert s.run_once() and len(e.sent) == 1
    e.buy(2, ".44")
    assert s.run_once() and len(e.sent) == 2
    assert e.sent[-1]["count"] == "2" and e.sent[-1]["price"] == "0.4900"


def test_restart_recovers_resting_order_without_duplicate(tmp_path):
    e, s, config, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    s = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert s.run_once() and len(e.sent) == 1 and not e.cancelled


@pytest.mark.parametrize("lost_ack", [True, False])
def test_hidden_order_and_lost_ack_cannot_create_duplicates_even_after_restart(tmp_path, lost_ack):
    e, s, config, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.lose_ack, e.hide_post = lost_ack, True
    assert not s.run_once() and len(e.sent) == 1
    s = ManualExitService(e, config, clock=e.clock, emit=lambda *a, **k: None)
    assert not s.run_once() and len(e.sent) == 1
    e.hidden_orders.clear()
    e.lose_ack = e.hide_post = False
    assert s.run_once() and len(e.sent) == 1 and not e.cancelled


def test_lost_cancel_ack_does_not_reuse_reservation_until_terminal_status(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    sale = e.make_order(count=3, action="sell")
    e.fill(sale, 3)
    e.lose_cancel_ack = True
    assert not s.run_once() and len(e.sent) == 1
    e.lose_cancel_ack = False
    e.clock_value += 6
    assert s.run_once() and len(e.sent) == 2
    assert e.sent[-1]["count"] == "7"


def test_unconfirmed_cancel_blocks_replacement(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    sale = e.make_order(count=3, action="sell")
    e.fill(sale, 3)
    e.delay_cancel = True
    assert not s.run_once() and len(e.sent) == 1
    assert not s.run_once() and len(e.sent) == 1
    e.delay_cancel = False
    e.clock_value += 6
    assert not s.run_once()  # Cancellation requested; still requires readback.
    assert s.run_once() and len(e.sent) == 2


def test_market_close_cancels_owned_order_and_waits_for_confirmation(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    e.clock_value = e.close + 1
    assert not s.run_once() and e.cancelled == exit_ids(e)
    assert not s.worker.ledger(T).get("window_closed")
    assert s.run_once() and s.worker.ledger(T)["window_closed"]
    assert e.held[T] == 10 and len(e.sent) == 1


def test_inconsistent_exit_history_cancels_resting_remainder_without_duplicate(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    fill = e.fill(exit_ids(e)[0], 3, ".47")
    e.hidden_fills.add(fill)
    assert not s.run_once() and e.cancelled == exit_ids(e) and len(e.sent) == 1
    e.hidden_fills.clear()
    assert s.run_once() and len(e.sent) == 2 and e.sent[-1]["count"] == "7"


def test_dry_run_logs_order_below_target_without_any_write(tmp_path):
    e, s, _, events = setup(tmp_path, dry_run=True)
    e.buy(10, ".42")
    assert s.run_once() and not e.sent and not e.cancelled
    assert any(event == "MANUAL_DRY_RUN_RESTING_ORDER" and D(data["target"]) == D(".47")
               for event, data in events)


@pytest.mark.parametrize("sign", [1, -1])
def test_automatic_inventory_and_its_journals_are_excluded(tmp_path, sign):
    e, s, config, _ = setup(tmp_path)
    state = {"markets": {}}
    auto_buy(e, state, sign=sign)
    config.entry_state.write_text(json.dumps(state))
    before = {p: p.read_bytes() for p in (config.entry_state, config.automatic_exit_state)}
    e.bid = e.no_bid = D(".99")
    assert s.run_once() and not e.sent
    e.bid = e.no_bid = D(".40")
    e.buy(3, ".42", sign)
    assert s.run_once() and len(e.sent) == 1 and e.sent[0]["count"] == "3"
    e.fill(exit_ids(e)[0], 3, ".47")
    assert s.run_once() and e.held[T] == sign * 10
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("entry,expected", [(".94", "0.9900"), (".95", None), (".96", None)])
def test_never_lowers_unreachable_target_to_make_order_fit(tmp_path, entry, expected):
    e, s, _, _ = setup(tmp_path)
    e.buy(1, entry)
    assert s.run_once()
    assert [o["price"] for o in e.sent] == ([expected] if expected else [])


def test_client_blocks_direct_writes_and_all_writes_in_dry_run(monkeypatch):
    calls = []
    def send(self, method, path, params=None, body=None, auth=False):
        calls.append((method, path, params, body))
        return {"order_id": "my-exit", "client_order_id": "manual5c-test"}
    monkeypatch.setattr(KalshiClient, "request", send)
    intent = {"quantity": "3", "target": ".47", "side": "NO", "client_id": "manual5c-test",
              "expires_at": 2000, "order_id": "my-exit"}
    for dry in [True, False]:
        client = RestingExitOnlyClient(dry_run=dry)
        for method, path in [("POST", "/portfolio/events/orders"), ("POST", "/portfolio/transfers"),
                             ("DELETE", "/portfolio/events/orders"), ("DELETE", "/portfolio/events/orders/user-order")]:
            with pytest.raises(RuntimeError):
                client.request(method, path, body={})
        if dry:
            with pytest.raises(RuntimeError):
                client.place_manual_limit(T, intent)
        else:
            client.place_manual_limit(T, intent)
    assert len(calls) == 1 and calls[0][3]["side"] == "bid" and calls[0][3]["price"] == "0.5300"
    assert client._approved_write is None
    monkeypatch.setattr(client, "order", lambda *a: {"ticker": T, "client_order_id": "user-owned"})
    with pytest.raises(ManualExitBlocked):
        client.cancel_manual_exit(T, intent)
    assert len(calls) == 1


def test_execution_modes_cannot_share_journal(tmp_path):
    e, _, config, _ = setup(tmp_path)
    with pytest.raises(ManualExitBlocked, match="separate journals"):
        ManualExitService(e, replace(config, execution_mode="monitored_reduce_only"), clock=e.clock)
    assert Settings.read({}).manual_state != Settings.read({"MANUAL_EXIT_MODE": "monitored_reduce_only"}).manual_state


def test_durable_intent_must_save_before_submission(tmp_path, monkeypatch):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    original = s.worker.save
    def failing_save():
        if s.worker.pending(T):
            raise OSError("disk unavailable")
        return original()
    monkeypatch.setattr(s.worker, "save", failing_save)
    assert not s.run_once() and not e.sent


def test_ownership_change_before_post_prevents_submission(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    original = s.worker.verify_snapshot
    def changed(ticker):
        sale = e.make_order(count=10, action="sell")
        e.fill(sale, 10)
        original(ticker)
    s.worker.verify_snapshot = changed
    assert not s.run_once() and not e.sent


def test_change_while_saving_intent_is_rechecked_before_post(tmp_path, monkeypatch):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    original = s.worker.save
    changed = False
    def save_and_change_position():
        nonlocal changed
        original()
        if s.worker.pending(T) and not changed:
            changed = True
            sale = e.make_order(count=10, action="sell")
            e.fill(sale, 10)
    monkeypatch.setattr(s.worker, "save", save_and_change_position)
    assert not s.run_once() and not e.sent and not s.worker.pending(T)


def test_journal_read_failure_attempts_only_owned_cancellations(tmp_path):
    e, s, config, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    config.entry_state.write_text("incomplete external write")
    with pytest.raises(ValueError):
        s.run_once()
    assert e.cancelled == exit_ids(e) and len(e.sent) == 1


@pytest.mark.parametrize("status", [400, 429, 500, 409])
def test_definite_rejection_retries_after_backoff_but_uncertain_post_stays_blocked(tmp_path, status):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    e.fail_post = KalshiAPIError(status, "simulated failure")
    assert not s.run_once() and not e.sent
    assert s.worker.pending(T) == (status in {500, 409})
    e.fail_post = None
    e.clock_value += 10
    if status in {500, 409}:
        assert not s.run_once() and not e.sent
    else:
        assert s.run_once() and len(e.sent) == 1


def test_other_series_and_pre_activation_fills_are_not_adopted(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.clock_value -= 10
    e.buy(10, ".42")
    e.clock_value += 10
    e.buy(10, ".42", ticker="KXOTHER-TEST")
    assert s.run_once() and not e.sent
    e.buy(2, ".43", ticker="KXETH15M-TEST")
    e.clock_value += 1
    assert s.run_once() and len(e.sent) == 1
    assert e.sent[0]["ticker"] == "KXETH15M-TEST" and e.sent[0]["price"] == "0.4800"


def test_cancellation_cannot_target_an_order_with_a_different_owner(tmp_path):
    e, s, _, _ = setup(tmp_path)
    e.buy(10, ".42")
    assert s.run_once()
    e.remote[exit_ids(e)[0]]["client_order_id"] = "user-replaced-order"
    assert not s.run_once() and not e.cancelled and len(e.sent) == 1
