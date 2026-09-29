"""Offline production-policy checks for immutable minute-three confirmation."""
import copy
from datetime import datetime
from decimal import Decimal as D
import pytest
import bot
from test_combined_entry_rules import setup


def configured(monkeypatch, elapsed=180, side="YES", spot=None):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ".45", side)
    monkeypatch.setattr(bot.datetime, "fromtimestamp", datetime.fromtimestamp, raising=False)
    monkeypatch.setattr(bot, "MINUTE3_POLICY", True)
    monkeypatch.setattr(bot, "DIRECTIONAL_ENTRY_POLICY", True)
    monkeypatch.setattr(bot, "ENTRY_START_DELAY", 180)
    monkeypatch.setattr(bot, "START", 180)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D(spot or ("100030" if side == "YES" else "99970")))
    def values(started, field, count, last_offset=0):
        assert field == "expiration_value" and count == 1 and last_offset == 2
        return [D("99900" if side == "YES" else "100100")]
    monkeypatch.setattr(bot, "completed_values", values)
    return fake, record, state, clock, closed


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_agreement_can_buy_and_survives_restart(monkeypatch, side):
    fake, record, state, clock, closed = configured(monkeypatch, side=side)
    result, _ = bot.funded_entry(record, state, "TEST", side, D(".45"), closed, "regular")
    assert result.get("order_id") and fake.entries
    saved = copy.deepcopy(record["minute3_confirmation"])
    assert saved["side"] == side and saved["status"] == "CONFIRMED"
    monkeypatch.setattr(bot, "completed_values", lambda *a, **k: pytest.fail("Do not resample"))
    restored = copy.deepcopy(record)
    assert bot.minute3_confirmation(restored, {"markets": {"TEST": restored}}, "TEST", fake.market("TEST"), closed) == saved


@pytest.mark.parametrize("spot,reason", [("99970", "MINUTE3_CONFLICT"), ("100010", "MINUTE3_NEUTRAL")])
def test_skip_cannot_recover_later_or_enter_settlement(monkeypatch, spot, reason):
    fake, record, state, clock, closed = configured(monkeypatch, spot=spot)
    assert bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular") == ({}, 0)
    assert record["minute3_confirmation"]["reason"] == reason
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("100100"))
    monkeypatch.setattr(bot.time, "time", lambda: closed.timestamp() - 100)
    bot.settlement_entry(record, state, "TEST", closed)
    assert not fake.entries and record["minute3_confirmation"]["reason"] == reason


@pytest.mark.parametrize("kind", ["regular", "dual", "historical", "spot", "opening_bias", "opening_57", "late_bias", "settlement_97"])
def test_every_route_waits_three_minutes(monkeypatch, kind):
    fake, record, state, clock, closed = configured(monkeypatch, 179.999)
    assert bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, kind) == ({}, 0)
    assert not fake.entries and "minute3_confirmation" not in record


@pytest.mark.parametrize("elapsed,status", [(179.999, "WAITING_CONFIRMATION"), (180, "CONFIRMED"), (209.999, "CONFIRMED"), (210, "DATA_UNAVAILABLE"), (800, "DATA_UNAVAILABLE")])
def test_exact_sampling_boundaries(monkeypatch, elapsed, status):
    fake, record, state, clock, closed = configured(monkeypatch, elapsed)
    assert bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed)["status"] == status


def test_slow_price_read_cannot_confirm_after_deadline(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch)
    def slow():
        monkeypatch.setattr(bot.time, "time", lambda: closed.timestamp() - 690)
        return D("100100")
    monkeypatch.setattr(fake, "btc_reference_price", slow)
    assert bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed)["status"] == "DATA_UNAVAILABLE"


def test_failure_is_unavailable_and_does_not_choose_later_sample(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch)
    monkeypatch.setattr(bot, "completed_values", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing")))
    decision = bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed)
    assert decision["status"] == "DATA_UNAVAILABLE"
    assert bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed) is decision


def test_save_failure_blocks_orders_and_retains_first_sample(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch)
    monkeypatch.setattr(bot, "save_state", lambda state: (_ for _ in ()).throw(OSError("disk")))
    assert bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular") == ({}, 0)
    assert not fake.entries and record["minute3_save_pending"]
    monkeypatch.setattr(bot, "save_state", lambda state: None)
    monkeypatch.setattr(bot, "completed_values", lambda *a, **k: pytest.fail("No second sample"))
    assert bot.confirmed_entry_side(record, state, "TEST", fake.market("TEST"), closed) == "YES"


def test_confirmed_side_cannot_flip_later(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch)
    bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("99900"))
    assert bot.funded_entry(record, state, "TEST", "NO", D(".45"), closed, "regular") == ({}, 0)
    assert bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular") == ({}, 0)
    assert not fake.entries


def test_full_cycle_confirms_before_entry(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch)
    bot.cycle(state)
    assert record["minute3_confirmation"]["status"] == "CONFIRMED"
    assert fake.entries


def test_neutral_emits_research_diagnostic_with_observation_time(monkeypatch):
    fake, record, state, clock, closed = configured(monkeypatch, spot="100010")
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, ticker="", **kw: events.append((event, kw)))
    decision = bot.minute3_confirmation(record, state, "TEST", fake.market("TEST"), closed)
    diagnostic = next(kw for event, kw in events if event == "ENTRY_SIDE")
    assert diagnostic["prediction"] == "AT_STRIKE"
    assert diagnostic["time_utc"] == decision["observed_at"]
