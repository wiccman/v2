from datetime import datetime, timedelta, timezone

import pytest

import bot


def test_retired_execution_mode_cannot_fall_back_to_live_trading(monkeypatch):
    monkeypatch.setattr(bot, "ENABLED", True)
    monkeypatch.setattr(bot, "EXECUTION_STRATEGY", "market_making")
    monkeypatch.setattr("sys.argv", ["bot.py"])
    monkeypatch.setattr(bot, "check", lambda: pytest.fail("Unexpected exchange access"))
    monkeypatch.setattr(bot, "cycle", lambda state: pytest.fail("Unexpected trading"))
    with pytest.raises(SystemExit, match="Market-making execution has been removed"):
        bot.main()


def test_disabled_bot_keeps_order_routing_off_with_old_mode(monkeypatch):
    monkeypatch.setattr(bot, "ENABLED", False)
    monkeypatch.setattr(bot, "EXECUTION_STRATEGY", "market_making")
    monkeypatch.setattr("sys.argv", ["bot.py"])
    checks = []
    monkeypatch.setattr(bot, "check", lambda: checks.append("read-only"))
    monkeypatch.setattr(bot, "cycle", lambda state: pytest.fail("Unexpected trading"))

    def stop_sleep(seconds):
        raise InterruptedError("test stops the disabled loop")

    monkeypatch.setattr(bot.time, "sleep", stop_sleep)
    with pytest.raises(InterruptedError):
        bot.main()
    assert checks == ["read-only"]


def test_legacy_mm_market_is_not_adopted_or_erased(monkeypatch):
    now = datetime.now(timezone.utc)
    state = {"markets": {}, "mm": {"markets": {"OLD_MM": {"orders": [{"filled": "1"}]}}}}
    monkeypatch.setattr(bot, "active_market", lambda when: (
        {"ticker": "OLD_MM"}, now - timedelta(minutes=2), now + timedelta(minutes=13)
    ))
    events = []
    monkeypatch.setattr(bot, "write_log", lambda event, *args, **kwargs: events.append(event))
    bot.cycle(state)
    assert events == ["STRATEGY_SWITCH_WAIT"]
    assert state["markets"] == {}
    assert state["mm"]["markets"]["OLD_MM"]["orders"] == [{"filled": "1"}]
