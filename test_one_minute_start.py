"""All entry routes wait for minute one without consuming their opportunities."""
import os
import subprocess
import sys
from decimal import Decimal as D

import pytest

import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("kind", ["regular", "dual", "historical", "spot", "opening_bias", "opening_57", "late_bias", "settlement_97"])
def test_every_route_blocks_before_one_minute_even_with_future_caller_time(monkeypatch, side, kind):
    fake, record, state, clock, closed = setup(monkeypatch, 59.999, ".57", side)
    monkeypatch.setattr(fake, "market_cash", lambda ticker: pytest.fail("No funding read before start"))
    result = bot.funded_entry(record, state, "TEST", side, D(".57"), closed, kind,
                             now_timestamp=closed.timestamp() - 1)
    assert result == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


@pytest.mark.parametrize("elapsed,allowed", [(-1, False), (0, False), (59.999, False), (60, True), (61, True)])
def test_regular_gateway_opens_at_exactly_sixty_seconds(monkeypatch, elapsed, allowed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ".45")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular")
    assert bool(result.get("order_id")) == allowed


def test_first_minute_cycles_preserve_opening_and_optional_attempts(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 0, ".52")
    monkeypatch.setattr(bot, "DUAL_LIMIT_BUYS_ENABLED", True)
    monkeypatch.setattr(bot, "HISTORICAL_STRIKE_ENABLED", True)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("100100"))
    for elapsed in (0, 30, 59.999):
        clock[0] = closed.timestamp() - 900 + elapsed
        bot.cycle(state)
        assert not fake.entries and not record["entry_intents"]
        assert not record["opening_bias_attempted"]
        assert not record["dual_limit_attempted"]
        assert not record["spot_entry_attempted"]
        assert not record["historical_triggered_strikes"]
    clock[0] = closed.timestamp() - 840
    bot.cycle(state)
    assert fake.entries
    assert any(i["kind"] == "opening_bias" for i in record["entry_intents"])


def test_old_environment_cannot_restore_immediate_entries():
    result = subprocess.run([sys.executable, "-c", "import bot; assert bot.START == bot.ENTRY_START_DELAY == 60"],
                            env={**os.environ, "ENTRY_START_MINUTE": "0"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
