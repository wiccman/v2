"""Offline tests for the separate 15-minute coin markets (not Coin Race)."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from single_coin_minute9 import Journal, DEFAULT_SERIES, plan_single_coin

START = 1793763000  # 15-minute-aligned contract window
CLOSE = START + 900

def market(yes=".72", no=".29", series="KXBTC15M", status="active"):
    return {
        "ticker": f"{series}-26NOV031500-00",
        "status": status,
        "yes_ask_dollars": yes, "no_ask_dollars": no,
        "close_time": datetime.fromtimestamp(CLOSE, timezone.utc).isoformat(),
    }

@pytest.mark.parametrize("series", DEFAULT_SERIES)
def test_each_coin_is_independent(series):
    p = plan_single_coin(market(series=series), START + 550, series)
    assert p and p["series"] == series and p["side"] == "YES"

def test_no_coin_race_or_basket_selection():
    assert "KXCRYPTOLEAD15M" not in DEFAULT_SERIES
    assert plan_single_coin(market(series="KXBTC15M"), START + 550, "KXETH15M") is None

@pytest.mark.parametrize("price", [".59", ".60", ".72", ".90", ".91"])
def test_yes_boundaries_are_inclusive(price):
    p = plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M")
    assert p and p["side"] == "YES" and D(p["ask"]) == D(price)

@pytest.mark.parametrize("price", [".01", ".58", ".5899", ".9101", ".92", ".99"])
def test_no_buy_outside_price_band(price):
    assert plan_single_coin(market(yes=price, no=".01"), START + 550, "KXBTC15M") is None

@pytest.mark.parametrize("price", [".59", ".72", ".91"])
def test_no_side_can_qualify_independently(price):
    p = plan_single_coin(market(yes=".01", no=price), START + 550, "KXBTC15M")
    assert p and p["side"] == "NO" and D(p["ask"]) == D(price)

@pytest.mark.parametrize("elapsed", [0, 539.9, 600, 610, 890])
def test_outside_minute_nine_rejected(elapsed):
    assert plan_single_coin(market(), START + elapsed, "KXBTC15M") is None

@pytest.mark.parametrize("elapsed", [540, 550, 599.9])
def test_inside_minute_nine_allowed(elapsed):
    assert plan_single_coin(market(), START + elapsed, "KXBTC15M")

def test_both_sides_in_range_skips_ambiguity():
    assert plan_single_coin(market(yes=".61", no=".60"), START + 550, "KXBTC15M") is None

def test_closed_market_skips():
    assert plan_single_coin(market(status="closed"), START + 550, "KXBTC15M") is None

def test_price_rounding_never_exceeds_91():
    p = plan_single_coin(market(yes=".9001", no=".01"), START + 550, "KXBTC15M")
    assert p["limit"] == ".91" or p["limit"] == "0.91"

def test_journal_persists_one_order_per_coin(tmp_path):
    state = tmp_path / "minute9.json"
    first = Journal(state)
    first.data["windows"]["KXBTC15M-xyz"] = {"buy": {"client_id": "unique-btc"}}
    first.data["windows"]["KXETH15M-xyz"] = {"buy": {"client_id": "unique-eth"}}
    first.save()
    assert len(Journal(state).data["windows"]) == 2

def test_corrupt_journal_refused(tmp_path):
    state = tmp_path / "minute9.json"
    state.write_text('{"windows": {}}')
    with pytest.raises(RuntimeError):
        Journal(state)
