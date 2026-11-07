"""Offline checks for the opt-in Crypto Leader minute-nine strategy."""
import copy
import pytest
from crypto_leader_minute9 import Journal, nearest_mean_coin, plan_minute_nine

START = 1793763000000  # exactly aligned 15-minute window
END = START + 900000

def sample():
    values = {"btc": .50, "eth": -.20, "sol": .30, "xrp": .11, "hype": -.15}
    return {
        "window_start_ms": START, "window_end_ms": END,
        "sample_ms": START + 540000,
        "timeframe_seconds": 60, "chart_type": "standard",
        "missing": 0, "values": values, "mean": sum(values.values()) / 5,
        "data_quality": {"held_count": 0, "prior_window_count": 0}
    }

def market(price=".59", coin="XRP"):
    from datetime import datetime, timezone
    return {
        "ticker": f"KXCRYPTOLEAD15M-26NOV031500-{coin}",
        "status": "active",
        "no_ask_dollars": price,
        "close_time": datetime.fromtimestamp(END / 1000, timezone.utc).isoformat(),
    }

def test_selected_no_coin_is_nearest_mean():
    assert nearest_mean_coin(sample()) == "XRP"
    assert plan_minute_nine(sample(), [market(".72")], (START + 550000) / 1000)["side"] == "NO"

@pytest.mark.parametrize("price", [".59", ".60", ".72", ".90", ".91"])
def test_inclusive_prices(price):
    p = plan_minute_nine(sample(), [market(price)], (START + 550000) / 1000)
    assert p and p["ask"] == price
    assert float(p["limit"]) >= float(price)

@pytest.mark.parametrize("price", [".00", ".01", ".58", ".5899", ".9101", ".92", ".99"])
def test_reject_outside_price_band(price):
    assert plan_minute_nine(sample(), [market(price)], (START + 550000) / 1000) is None

@pytest.mark.parametrize("offset", [0, 539999, 600000, 700000])
def test_only_minute_nine(offset):
    assert plan_minute_nine(sample(), [market()], (START + offset) / 1000) is None

def test_stale_or_future_signal_blocked():
    s = sample()
    s["sample_ms"] -= 120000
    assert plan_minute_nine(s, [market()], (START + 550000) / 1000) is None
    s = sample()
    s["sample_ms"] += 90000
    assert plan_minute_nine(s, [market()], (START + 550000) / 1000) is None

def test_missing_held_or_tied_mean_blocked():
    s = sample()
    s["missing"] = 1
    assert nearest_mean_coin(s) is None
    s = sample()
    s["data_quality"]["held_count"] = 1
    assert nearest_mean_coin(s) is None
    s = sample()
    s["values"] = {"btc": 1.0, "eth": 1.0, "sol": 0.0, "xrp": 0.0, "hype": 0.5}
    s["mean"] = .5
    assert nearest_mean_coin(s) is None

def test_wrong_coin_or_window_close_rejected():
    s = sample()
    assert plan_minute_nine(s, [market(coin="BTC")], (START + 550000) / 1000) is None
    m = market()
    m["close_time"] = "2026-11-01T01:00:00+00:00"
    assert plan_minute_nine(s, [m], (START + 550000) / 1000) is None

def test_ambiguous_duplicate_market_rejected():
    m = market()
    assert plan_minute_nine(sample(), [m, copy.deepcopy(m)], (START + 550000) / 1000) is None

def test_journal_persists_entry_intent(tmp_path):
    state = tmp_path / "journal.json"
    first = Journal(state)
    first.data["windows"][str(START)] = {"buy": {"client_id": "durable-id"}}
    first.save()
    assert Journal(state).data["windows"][str(START)]["buy"]["client_id"] == "durable-id"

def test_corrupt_journal_refused(tmp_path):
    state = tmp_path / "journal.json"
    state.write_text('{"windows": {}}')
    with pytest.raises(RuntimeError):
        Journal(state)
