"""Keep the archived strategy tests scoped to their original entry policy.

The production policy is directional; dedicated tests explicitly enable it.
"""
import pytest
import bot


@pytest.fixture(autouse=True)
def original_entry_policy_for_historical_tests(monkeypatch):
    monkeypatch.setattr(bot, "DIRECTIONAL_ENTRY_POLICY", False)
    monkeypatch.setattr(bot, "ENTRY_START_DELAY", 0)
    monkeypatch.setattr(bot, "START", 0)
