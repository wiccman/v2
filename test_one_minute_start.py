"""Early entry routes open immediately; later price gates still apply."""
import os
import subprocess
import sys
from decimal import Decimal as D

import pytest

import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("kind", ["regular", "dual", "historical", "spot", "opening_bias", "opening_57", "late_bias", "settlement_97"])
def test_every_route_blocks_before_contract_open_even_with_future_caller_time(monkeypatch, side, kind):
    fake, record, state, clock, closed = setup(monkeypatch, -0.001, ".57", side)
    monkeypatch.setattr(fake, "market_cash", lambda ticker: pytest.fail("No funding read before start"))
    result = bot.funded_entry(record, state, "TEST", side, D(".57"), closed, kind,
                             now_timestamp=closed.timestamp() - 1)
    assert result == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


@pytest.mark.parametrize("elapsed,allowed", [(-1, False), (0, True), (59.999, True), (60, True), (61, True)])
def test_regular_gateway_opens_at_contract_start(monkeypatch, elapsed, allowed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ".45")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular")
    assert bool(result.get("order_id")) == allowed


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("kind", ["regular", "dual", "historical", "spot", "opening_bias", "opening_57"])
def test_all_early_routes_can_buy_at_contract_open(monkeypatch, side, kind):
    fake, record, state, clock, closed = setup(monkeypatch, 0, ".57", side)
    result, _ = bot.funded_entry(record, state, "TEST", side, D(".57"), closed, kind)
    assert result["order_id"] and fake.entries
    assert record["entry_intents"][-1]["side"] == side


def test_full_cycle_can_place_opening_order_immediately(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 0, ".52")
    bot.cycle(state)
    assert fake.entries
    assert any(i["kind"] == "opening_bias" for i in record["entry_intents"])


def test_old_environment_cannot_restore_one_minute_wait():
    result = subprocess.run([sys.executable, "-c", "import bot; assert bot.START == bot.ENTRY_START_DELAY == 0"],
                            env={**os.environ, "ENTRY_START_MINUTE": "1"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
