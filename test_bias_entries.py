"""Offline integration coverage for Boruto early entries and live late entries."""
import copy
from decimal import Decimal as D

import pytest
import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("kind,price", [
    ("regular", ".52"),
    ("dual", ".52"), ("historical", ".52"), ("spot", ".52"),
])
def test_every_early_route_obeys_bias_when_live_strike_opposes(monkeypatch, side, kind, price):
    fake, record, state, clock, closed = setup(monkeypatch, 180, price, side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("99990" if side == "YES" else "100010"))
    result, _ = bot.funded_entry(record, state, "TEST", side, D(price), closed, kind)
    assert result["order_id"]
    assert record["entry_bias"]["prediction"] == side
    intent = record["entry_intents"][-1]
    assert intent["side"] == side and intent["side_source"] == "boruto"
    assert intent["bias_build"] == bot.SIGNAL_BUILD
    assert intent["exit_target"] == str(bot.ALL_ENTRY_EXIT_PAIRS[D(price)])


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_full_cycle_locks_bias_and_never_buys_opposite_side(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 180, ".59", side)
    record["signal"] = {"prediction": "NO" if side == "YES" else "YES", "build": "old"}
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("99900" if side == "YES" else "100100"))
    bot.cycle(state)
    assert fake.entries
    assert {i["side"] for i in record["entry_intents"]} == {side}
    assert record["entry_bias"]["prediction"] == side
    opposite = "NO" if side == "YES" else "YES"
    assert bot.funded_entry(record, state, "TEST", opposite, D(".59"), closed, "regular") == ({}, 0)


def test_saved_bias_survives_restart_without_repull_or_live_price_dependency(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 120, ".45")
    saved = []
    monkeypatch.setattr(bot, "save_state", lambda s: saved.append(copy.deepcopy(s)))
    signal = bot.ensure_entry_bias(record, state, "TEST", fake.market("TEST"), closed)
    state = copy.deepcopy(saved[-1]); record = state["markets"]["TEST"]
    def unavailable(*args, **kwargs):
        raise AssertionError("Locked early bias must not repull data or require spot")
    monkeypatch.setattr(fake, "markets", unavailable)
    monkeypatch.setattr(fake, "btc_reference_price", unavailable)
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".45"), closed, "regular")
    assert result["order_id"] and record["entry_bias"] == signal
    assert saved[-2]["markets"]["TEST"]["entry_bias"] == signal


@pytest.mark.parametrize("fault", ["missing", "unfinalized"])
def test_bad_history_blocks_early_entries_but_can_retry(monkeypatch, fault):
    fake, record, state, clock, closed = setup(monkeypatch, 180, ".52")
    history = fake.markets()
    broken = copy.deepcopy(history)
    if fault == "missing":
        broken.pop()
    else:
        broken[-1]["status"] = "closed"
    monkeypatch.setattr(fake, "markets", lambda **kw: broken)
    bot.cycle(state)
    assert not fake.entries and not record["entry_intents"] and "entry_bias" not in record
    assert not record["opening_bias_attempted"]
    monkeypatch.setattr(fake, "markets", lambda **kw: history)
    bot.cycle(state)
    assert fake.entries and record["entry_bias"]["prediction"] == "YES"


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("elapsed,price", [(360, ".75"), (480, ".70"), (660, ".85"), (720, ".96")])
def test_late_routes_ignore_conflicting_bias_and_missing_lookbacks(monkeypatch, side, elapsed, price):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, price, side)
    record["entry_bias"] = {"prediction": "NO" if side == "YES" else "YES"}
    monkeypatch.setattr(fake, "markets", lambda **kw: pytest.fail("Late route must not fetch lookbacks"))
    kind = bot.SETTLEMENT_KIND if price == ".96" else "regular" if price != ".85" else "late_bias"
    result, _ = bot.funded_entry(record, state, "TEST", side, D(price), closed, kind,
                                 cancel_at=closed.timestamp())
    assert result["order_id"]
    assert record["entry_intents"][-1]["side_source"] == "live_strike"


@pytest.mark.parametrize("field,value", [("ticker", "OTHER"), ("strike", "1"), ("build", "old"), ("prediction", "SKIP")])
def test_mismatched_saved_bias_cannot_create_an_order(monkeypatch, field, value):
    fake, record, state, clock, closed = setup(monkeypatch, 180, ".52")
    bot.ensure_entry_bias(record, state, "TEST", fake.market("TEST"), closed)
    record["entry_bias"][field] = value
    assert bot.funded_entry(record, state, "TEST", "YES", D(".52"), closed, "regular") == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


def test_failed_bias_save_is_not_reused_in_memory(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 180, ".52")
    monkeypatch.setattr(bot, "save_state", lambda s: (_ for _ in ()).throw(OSError("disk full")))
    assert bot.funded_entry(record, state, "TEST", "YES", D(".52"), closed, "regular") == ({}, 0)
    assert "entry_bias" not in record and not fake.entries and not record["entry_intents"]


def test_slow_history_cannot_extend_early_entry_window(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 359, ".59")
    history = fake.markets()
    def slow(**kwargs):
        clock[0] = closed.timestamp() - 540
        return history
    monkeypatch.setattr(fake, "markets", slow)
    assert bot.funded_entry(record, state, "TEST", "YES", D(".59"), closed, "regular") == ({}, 0)
    assert not fake.entries and not record["entry_intents"]



@pytest.fixture(autouse=True)
def historical_high_price_policy(monkeypatch):
    """Recreate pre-block trades for the recovery/side-selection scenarios.

    Production window enforcement is covered by test_blocked_buy_window and
    the combined/early/eight-minute entry suites.
    """
    monkeypatch.setattr(bot, 'BLOCKED_BUY_START', 900)
    monkeypatch.setattr(bot, 'BLOCKED_BUY_END', 900)
