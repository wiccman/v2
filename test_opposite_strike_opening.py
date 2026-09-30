"""Offline opening direction, phase handoff, and execution protection checks."""
import copy
from decimal import Decimal as D

import pytest

import bot
from test_combined_entry_rules import setup


@pytest.mark.parametrize("spot,side", [("100010", "NO"), ("99990", "YES")])
@pytest.mark.parametrize("elapsed", [0, 119.999])
@pytest.mark.parametrize("kind,price", [
    ("opening_bias", ".52"), ("opening_57", ".57"), ("regular", ".52"),
    ("dual", ".52"), ("historical", ".52"), ("spot", ".52"),
])
def test_every_opening_route_buys_opposite_strike_without_bias(monkeypatch, spot, side, elapsed, kind, price):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, price, side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D(spot))
    monkeypatch.setattr(fake, "markets", lambda **kw: pytest.fail("Opening must not need lookbacks"))
    record["entry_bias"] = {"prediction": "YES" if side == "NO" else "NO", "build": "old"}
    old_bias = copy.deepcopy(record["entry_bias"])
    result, _ = bot.funded_entry(record, state, "TEST", side, D(price), closed, kind)
    assert result["order_id"]
    intent = record["entry_intents"][-1]
    assert intent["side"] == side and intent["side_source"] == "opposite_strike"
    assert "bias_build" not in intent and record["entry_bias"] == old_bias
    assert intent["cancel_at"] == closed.timestamp() - 780
    same_as_strike = "YES" if side == "NO" else "NO"
    assert bot.funded_entry(record, state, "TEST", same_as_strike, D(".59"), closed, "regular") == ({}, 0)


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_full_cycle_and_restart_use_live_opposite_side(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 0, ".52", side)
    monkeypatch.setattr(fake, "markets", lambda **kw: pytest.fail("No opening lookbacks"))
    bot.cycle(state)
    assert fake.entries and {i["side"] for i in record["entry_intents"]} == {side}
    assert "entry_bias" not in record
    restored = copy.deepcopy(state)
    original_count = len(fake.entries)
    bot.cycle(restored)
    assert len(fake.entries) == original_count


@pytest.mark.parametrize("spot", ["100000", "NaN", "0", "-1", "Infinity", None])
def test_bad_or_equal_reference_does_not_spend_then_retries(monkeypatch, spot):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".52")
    def reference():
        if spot is None:
            raise TimeoutError("reference unavailable")
        return D(spot)
    monkeypatch.setattr(fake, "btc_reference_price", reference)
    bot.cycle(state)
    assert not fake.entries and not record["entry_intents"]
    assert not record["opening_bias_attempted"]
    monkeypatch.setattr(fake, "btc_reference_price", lambda: D("99990"))
    bot.cycle(state)
    assert fake.entries


def test_reference_crossing_before_post_blocks_stale_side(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".52")
    readings = iter([D("99990"), D("100010")])
    monkeypatch.setattr(fake, "btc_reference_price", lambda: next(readings))
    assert bot.funded_entry(record, state, "TEST", "YES", D(".52"), closed, "regular") == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


@pytest.mark.parametrize("slow_stage", ["market", "funding", "quote", "position"])
def test_slow_opening_attempt_cannot_submit_past_two_minutes(monkeypatch, slow_stage):
    fake, record, state, clock, closed = setup(monkeypatch, 119, ".52")
    original_market, original_position = fake.market, fake.positions
    calls = {"market": 0, "position": 0}
    def market(ticker):
        calls["market"] += 1
        if (slow_stage == "market" and calls["market"] == 1 or
                slow_stage == "quote" and calls["market"] == 2):
            clock[0] = closed.timestamp() - 780
        return original_market(ticker)
    def cash(ticker):
        if slow_stage == "funding":
            clock[0] = closed.timestamp() - 780
        return {"exchange_index": 2, "cash_dollars": "100"}
    def positions(ticker):
        calls["position"] += 1
        if slow_stage == "position" and calls["position"] == 2:
            clock[0] = closed.timestamp() - 780
        return original_position(ticker)
    monkeypatch.setattr(fake, "market", market)
    monkeypatch.setattr(fake, "market_cash", cash)
    monkeypatch.setattr(fake, "positions", positions)
    assert bot.funded_entry(record, state, "TEST", "YES", D(".52"), closed, "regular") == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_exact_minute_two_resumes_bias_even_when_strike_opposes_opening_rule(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 120, ".52", side)
    # The fixture's spot is on the bias side, so the opening rule would choose its opposite.
    result, _ = bot.funded_entry(record, state, "TEST", side, D(".52"), closed, "regular")
    assert result["order_id"] and record["entry_intents"][-1]["side_source"] == "boruto"


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_opening_55_uses_price_only_side_and_fixed_61_exit(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".55", side)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: pytest.fail("No BTC signal for price-only entry"))
    record["entry_bias"] = {"prediction": "NO" if side == "YES" else "YES", "build": "old"}
    result, quantity = bot.funded_entry(record, state, "TEST", side, D(".55"), closed, "opening_55")
    assert result["order_id"] and quantity == 5
    intent = record["entry_intents"][-1]
    assert intent["side"] == side and intent["side_source"] == "price_only"
    assert D(intent["exit_target"]) == D(".61") and intent["fixed_exit_target"] is True
    assert intent["cancel_at"] == closed.timestamp() - 780
    assert fake.entries[-1][3]["ioc"] is True


@pytest.mark.parametrize("elapsed,allowed", [(59.999, False), (60, True), (119.999, True), (120, False)])
def test_opening_55_has_first_two_minute_window(monkeypatch, elapsed, allowed):
    fake, record, state, clock, closed = setup(monkeypatch, elapsed, ".55")
    result, _ = bot.funded_entry(record, state, "TEST", "YES", D(".55"), closed, "opening_55")
    assert bool(result.get("order_id")) == allowed


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_cycle_prioritizes_unbiased_55_rule_over_overlapping_openers(monkeypatch):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".55", "YES")
    market = fake.market("TEST")
    market["yes_ask_dollars"] = ".55"
    market["no_ask_dollars"] = ".48"
    monkeypatch.setattr(fake, "market", lambda ticker: market)
    monkeypatch.setattr(fake, "btc_reference_price", lambda: pytest.fail("No BTC signal for price-only entry"))
    monkeypatch.setattr(bot, "DUAL_LIMIT_BUYS_ENABLED", False)
    monkeypatch.setattr(bot, "HISTORICAL_STRIKE_ENABLED", False)
    bot.cycle(state)
    assert len(fake.entries) == 1
    assert record["entry_intents"][-1]["kind"] == "opening_55"
    assert record["entry_intents"][-1]["side"] == "YES"


def test_opposing_inventory_still_blocks_opening(monkeypatch, side):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".52", side)
    fake.held = D(-3 if side == "YES" else 3)
    assert bot.funded_entry(record, state, "TEST", side, D(".52"), closed, "regular") == ({}, 0)
    assert not fake.entries and not record["entry_intents"]


@pytest.mark.parametrize("route", ["dual", "historical", "spot"])
def test_optional_routes_select_opposite_side_themselves(monkeypatch, route):
    fake, record, state, clock, closed = setup(monkeypatch, 60, ".45", "NO")
    if route == "dual":
        bot.place_dual_limit_buys(record, "TEST", closed, state=state)
    elif route == "historical":
        bot.place_historical_strike_entries(record, "TEST", D("100010"), closed, state=state)
    else:
        monkeypatch.setattr(fake, "btc_reference_price", lambda: D("100100"))
        monkeypatch.setattr(bot, "OPENING_BIAS_ENABLED", False)
        monkeypatch.setattr(bot, "MAX_BUYS", 0)
        bot.cycle(state)
    assert fake.entries and {i["side"] for i in record["entry_intents"]} == {"NO"}
    assert {i["kind"] for i in record["entry_intents"]} == {route}
