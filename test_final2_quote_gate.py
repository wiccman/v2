"""Offline checks for the final-96c route without retired strike inputs."""
from decimal import Decimal as D
import pytest
import bot
from test_settlement_entry import setup


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("elapsed,expected", [(779,0),(780,1),(899,1),(900,0)])
def test_final_window_requires_quotes_not_btc_or_saved_bias(monkeypatch, side, elapsed, expected):
    e, record, state, clock, closed = setup(monkeypatch, elapsed, side)
    monkeypatch.setattr(bot, "DIRECTIONAL_ENTRY_POLICY", True)
    e.market("TEST").pop("floor_strike")
    def unavailable():
        raise AssertionError("Retired BTC input must not be read")
    monkeypatch.setattr(e, "btc_reference_price", unavailable)
    record.pop("trade_side", None)
    record["signal"] = {"prediction": "NO" if side == "YES" else "YES"}
    bot.settlement_entry(record, state, "TEST", closed)
    assert len(e.entries) == expected
    if expected:
        wire, quantity, price, kwargs = e.entries[0]
        assert quantity == 10
        assert price == (D(".96") if side == "YES" else D(".04"))
        assert kwargs["expiration_time"] == closed.timestamp()
        assert record["entry_intents"][0]["side_source"] == "settlement_quote"
        bot.settlement_entry(record, state, "TEST", closed)
        assert len(e.entries) == 1


@pytest.mark.parametrize("yes,no", [(".95",".06"),("1",".001"),("NaN",".96"),(".96",".97")])
def test_invalid_or_ambiguous_quotes_never_submit(monkeypatch, yes, no):
    e, record, state, clock, closed = setup(monkeypatch)
    e.market("TEST").update(yes_ask_dollars=yes,no_ask_dollars=no)
    bot.settlement_entry(record, state, "TEST", closed)
    assert not e.entries


@pytest.mark.parametrize("block", ["cash", "manual_opposite", "exit_unhealthy"])
def test_existing_account_safeguards_still_block(monkeypatch, block):
    e, record, state, clock, closed = setup(monkeypatch)
    if block == "cash":
        monkeypatch.setattr(e, "market_cash", lambda ticker: {"exchange_index":2,"cash_dollars":"1"})
    elif block == "manual_opposite":
        e.held = D("-2")
    else:
        bot.EXIT_MONITOR.healthy = False
    bot.settlement_entry(record, state, "TEST", closed)
    assert not e.entries
