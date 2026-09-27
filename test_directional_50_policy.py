"""Production-only BTC distance rule, independent of archived entry policies."""
from decimal import Decimal as D

import pytest

import bot
from test_combined_entry_rules import setup
from test_settlement_entry import setup as settlement_setup


def enable(monkeypatch):
    monkeypatch.setattr(bot, "DIRECTIONAL_ENTRY_POLICY", True)
    monkeypatch.setattr(bot, "ENTRY_START_DELAY", 60)
    monkeypatch.setattr(bot, "START", 60)


@pytest.mark.parametrize("elapsed,spot,side,allowed", [
    (0, "100060", "YES", False), (59.999, "99940", "NO", False),
    (60, "100049.99", "YES", False), (60, "99950.01", "NO", False),
    (60, "100050", "YES", True), (60, "99950", "NO", True),
    (60, "100050", "NO", False), (60, "99950", "YES", False),
    (120, "100060", "YES", True), (120, "99940", "NO", True),
])
def test_entry_requires_minute_and_50_dollar_direction(monkeypatch, elapsed, spot, side, allowed):
    enable(monkeypatch)
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ".52", side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D(spot))
    result, _ = bot.funded_entry(record, state, "TEST", side, D(".52"), closed, "regular")
    assert bool(result.get("order_id")) == allowed


@pytest.mark.parametrize("side,spot", [("YES", "100050"), ("NO", "99950")])
@pytest.mark.parametrize("kind", ["opening_bias", "opening_57", "dual", "historical", "spot"])
def test_every_opening_route_uses_same_strike_gate(monkeypatch, side, spot, kind):
    enable(monkeypatch)
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".57", side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D(spot))
    result, _ = bot.funded_entry(record, state, "TEST", side, D(".57"), closed, kind)
    assert result.get("order_id")
    assert record["entry_intents"][-1]["side_source"] == "live_strike"


@pytest.mark.parametrize("side,spot,allowed", [
    ("YES", "100049.99", False), ("YES", "100050", True),
    ("NO", "99950.01", False), ("NO", "99950", True),
])
def test_settlement_route_uses_same_distance(monkeypatch, side, spot, allowed):
    enable(monkeypatch)
    fake, record, state, clock, closed = settlement_setup(monkeypatch, side=side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D(spot))
    bot.settlement_entry(record, state, "TEST", closed)
    assert bool(fake.entries) == allowed


def test_resting_settlement_buy_is_canceled_when_btc_reenters_band(monkeypatch):
    enable(monkeypatch)
    fake, record, state, clock, closed = settlement_setup(monkeypatch, side="YES")
    fake.market("TEST")["yes_ask_dollars"] = "0.98"  # 97c limit rests.
    spot = [D("100060")]
    monkeypatch.setattr(fake, "btc_reference_price", lambda: spot[0])
    bot.settlement_entry(record, state, "TEST", closed)
    assert len(fake.entries) == 1
    order_id = record["entry_intents"][-1]["order_id"]
    spot[0] = D("100049")
    bot.reconcile_entries(state)
    assert order_id in fake.cancelled
