"""Offline regressions for the operator-authorized 11c -> 20c stop migration."""
import copy
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from manual_exit_runner import ManualExitService, Settings
from manual_limit_exits import ManualExitBlocked
from test_manual_limit_exits import T
from test_manual_stop_loss import setup


def migrate(exchange, config, events):
    config = replace(config, stop_loss_cents=D("20"))
    service = ManualExitService(exchange, config, clock=exchange.clock,
        emit=lambda event, **data: events.append((event, data)))
    return service, config


def test_environment_20c_setting_preserves_5c_take_profit():
    settings = Settings.read({"MANUAL_STOP_LOSS_CENTS": "20"})
    assert settings.stop_loss_cents == D("20")
    assert settings.increment == D(".05")


@pytest.mark.parametrize("sign", [1, -1])
def test_existing_tp_preserved_until_exact_twenty_cent_boundary(tmp_path, sign):
    e, old, config, events = setup(tmp_path)
    e.buy(10, ".60", sign)
    e.bid = e.no_bid = D(".59")
    assert old.run_once()
    before = copy.deepcopy(old.worker.state)
    old_orders = copy.deepcopy(e.sent)
    service, config = migrate(e, config, events)
    assert service.worker.state["markets"] == before["markets"]
    for key, value in before.items():
        if key != "stop_loss_increment":
            assert service.worker.state[key] == value
    assert D(service.worker.state["stop_loss_increment"]) == D(".20")
    assert e.sent == old_orders and not e.cancelled
    for bid in (".49", ".41", ".4001"):
        e.bid = e.no_bid = D(bid)
        assert service.run_once()
        assert e.held[T] == sign * 10 and e.sent == old_orders
        assert not e.cancelled
    e.bid = e.no_bid = D(".40")
    assert service.run_once()
    assert e.held[T] == 0 and len(e.cancelled) == 1
    stops = [o for o in e.sent if o["time_in_force"] == "immediate_or_cancel"]
    assert len(stops) == 1 and stops[0]["reduce_only"] is True
    assert stops[0]["price"] == ("0.4000" if sign == 1 else "0.6000")
    assert any(event == "MANUAL_STOP_POLICY_MIGRATED" for event, _ in events)


def test_migration_is_durable_and_idempotent(tmp_path):
    e, old, config, events = setup(tmp_path)
    service, config = migrate(e, config, events)
    saved = json.loads(config.manual_state.read_text())
    assert D(saved["stop_loss_increment"]) == D(".20")
    assert len(saved["stop_loss_policy_migrations"]) == 1
    restarted, _ = migrate(e, config, events)
    assert restarted.worker.state == saved
    assert sum(event == "MANUAL_STOP_POLICY_MIGRATED" for event, _ in events) == 1


def test_per_fill_entry_costs_use_new_offset(tmp_path):
    e, old, config, events = setup(tmp_path)
    service, _ = migrate(e, config, events)
    e.buy(10, ".60")
    e.buy(5, ".50")
    e.bid = D(".39")
    assert service.run_once()
    stops = [o for o in e.sent if o["time_in_force"] == "immediate_or_cancel"]
    assert len(stops) == 1 and stops[0]["count"] == "10"
    assert e.held[T] == 5
    assert any(o["time_in_force"] == "good_till_canceled" and o["price"] == "0.5500"
               for o in e.sent)


def test_already_armed_stop_retains_cancel_confirmation_and_latch(tmp_path):
    e, old, config, events = setup(tmp_path)
    e.buy(10, ".60")
    e.bid = D(".59")
    assert old.run_once()
    e.bid, e.delay_cancel = D(".48"), True
    assert not old.run_once()
    before = copy.deepcopy(old.worker.state["markets"])
    service, _ = migrate(e, config, events)
    assert service.worker.state["markets"] == before
    assert not service.run_once() and len(e.sent) == 1
    e.bid, e.delay_cancel = D(".55"), False
    e.clock_value += 6
    for _ in range(3):
        service.run_once()
    assert e.held[T] == 0
    assert len([o for o in e.sent if o["time_in_force"] == "immediate_or_cancel"]) == 1


def test_pending_stop_ack_is_preserved_without_duplicate_sale(tmp_path):
    e, old, config, events = setup(tmp_path)
    e.buy(10, ".60")
    e.bid, e.lose_ack = D(".48"), True
    assert not old.run_once() and old.worker.ledger(T).get("pending")
    before = copy.deepcopy(old.worker.state["markets"])
    service, _ = migrate(e, config, events)
    assert service.worker.state["markets"] == before
    e.lose_ack = False
    assert service.run_once()
    assert not service.worker.ledger(T).get("pending")
    assert e.held[T] == 0 and len(e.sent) == 1


@pytest.mark.parametrize("previous,new", [("0.12", "20"), ("0.10", "20"),
                                          ("0.20", "11"), ("0.11", "21")])
def test_unapproved_transitions_remain_blocked_without_state_changes(tmp_path, previous, new):
    e, old, config, events = setup(tmp_path)
    saved = json.loads(config.manual_state.read_text())
    saved["stop_loss_increment"] = previous
    config.manual_state.write_text(json.dumps(saved))
    before = config.manual_state.read_bytes()
    with pytest.raises(ManualExitBlocked, match="Stop loss policy"):
        ManualExitService(e, replace(config, stop_loss_cents=D(new)), clock=e.clock)
    assert config.manual_state.read_bytes() == before
    assert not e.sent and not e.cancelled


def test_invalid_migration_history_blocks_without_overwriting_state(tmp_path):
    e, old, config, events = setup(tmp_path)
    saved = json.loads(config.manual_state.read_text())
    saved["stop_loss_policy_migrations"] = {"invalid": True}
    config.manual_state.write_text(json.dumps(saved))
    before = config.manual_state.read_bytes()
    with pytest.raises(ManualExitBlocked, match="migration history"):
        migrate(e, config, events)
    assert config.manual_state.read_bytes() == before
