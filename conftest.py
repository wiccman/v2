"""Explicit scenarios for retained entry helpers; never override live defaults."""
from decimal import Decimal as D
import pytest
import bot
import entry_policy


@pytest.fixture
def early_entry_scenario(monkeypatch):
    """Give legacy helper tests an explicit $10 early / $20 final allocation.

    The current dispatcher only runs the final route. These tests call retained
    helpers directly to exercise recovery, ownership and budget protections.
    Their small per-order ceiling keeps partial-fill scenarios deterministic.
    """
    monkeypatch.setenv("MARKET_BUDGET_DOLLARS", "30")
    monkeypatch.setattr(bot, "MARKET_BUDGET", D("30"))
    monkeypatch.setattr(entry_policy, "EARLIER_ORDER_BUDGET", D("2.80"))
    monkeypatch.setattr(bot, "DIRECTIONAL_ENTRY_POLICY", False)
    monkeypatch.setattr(bot, "ENTRY_START_DELAY", 0)
    monkeypatch.setattr(bot, "START", 0)
